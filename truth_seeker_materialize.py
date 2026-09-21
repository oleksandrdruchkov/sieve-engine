"""
Шукач істини: матеріалізація L9 зі Ledger (чисті функції, без I/O і без годинника).

    materialize(records, cutoff_seq, current_nodes, as_of=None) -> Materialized
    evaluate(mat, current_nodes, goal, graph_version=..., as_of=...) -> Evaluation
    make_regeneration_task(trigger, ...) -> dict (Task v1.1)

Що виправлено відносно першої версії aggregate_l9_phase:
  1. target атаки береться з ROUTING.target (id вузла), а не з target_class;
  2. два проходи: спершу останній ROUTING на атаку, потім докази
     (повторний ROUTING більше не стирає докази, порядок записів не важливий);
  3. не-EXECUTE рішення (MITIGATE / RISK_REGISTER / ESCALATE_HUMAN) потрапляють
     у реєстр ризиків, а не зникають;
  4. докази фільтруються за поточним node_hash, TTL, статусом і ключем ідемпотентності;
  5. регенерація запускається від ВЕРДИКТІВ (CONFIRMED у замиканні цілі),
     а не від чутливостей; чутливості повертаються структурно;
  6. знімок: читаються лише записи з seq <= cutoff_seq;
  7. VERDICT/AGGREGATE-payload відповідають схемам v1.1;
     дублікати вердиктів відсікаються за (attack_id, evidence_set_hash, prior).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import truth_seeker_l9 as l9
import math

SCHEMA_VERSION = "1.1"
ACTIVE = "EXECUTE_VERIFICATION"
RISK_DECISIONS = {"MITIGATE", "RISK_REGISTER", "ESCALATE_HUMAN"}


# ----------------------------------------------------------------- утиліти
def canon(o) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def r6(x: float) -> float:
    return round(float(x), 6)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def evidence_set_hash(ids) -> str:
    return sha("\n".join(sorted(ids)))


# ----------------------------------------------------------------- ланцюг Ledger
def record_hash(prev_hash: str, rec: dict) -> str:
    return sha(prev_hash + canon({k: v for k, v in rec.items() if k != "record_hash"}))


def verify_chain(records) -> list[str]:
    errs, prev = [], "0" * 64
    for r in sorted(records, key=lambda x: x["seq"]):
        if r["prev_hash"] != prev:
            errs.append(f"{r['record_id']}: розрив ланцюга")
        if r["record_hash"] != record_hash(prev, r):
            errs.append(f"{r['record_id']}: хибний record_hash")
        prev = r["record_hash"]
    return errs


def append_record(records: list, **rec) -> dict:
    """Дописує запис у кінець списку (seq неперервний, від 0)."""
    prev = records[-1]["record_hash"] if records else "0" * 64
    seq = len(records)
    rec.update(schema_version=SCHEMA_VERSION, seq=seq, record_id=f"rec-{seq:04d}",
               prev_hash=prev)
    rec["record_hash"] = record_hash(prev, rec)
    records.append(rec)
    return rec


# ----------------------------------------------------------------- граф
@dataclass(frozen=True)
class NodeState:
    q: float
    node_hash: str
    requires: tuple = ()
    n_eff: float = 10.0


def node_states(d: dict) -> dict:
    """JSON {id: {q, requires, node_hash}} -> {id: NodeState}."""
    return {k: NodeState(v["q"], v["node_hash"], tuple(v.get("requires", ())),
                         v.get("n_eff", 10.0)) for k, v in d.items()}


def graph_hash(nodes: dict) -> str:
    return sha(canon({k: [v.q, list(v.requires), v.node_hash]
                      for k, v in sorted(nodes.items())}))


def _to_l9(nodes: dict) -> dict:
    return {k: l9.Node(v.q, list(v.requires), v.n_eff) for k, v in nodes.items()}


# ----------------------------------------------------------------- materialize
@dataclass
class Materialized:
    cutoff_seq: int
    attacks: dict = field(default_factory=dict)          # id -> l9.Attack
    evidence_ids: dict = field(default_factory=dict)     # id -> [record_id] (той самий порядок)
    risk_register: list = field(default_factory=list)
    needs_reroute: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    excluded_evidence: list = field(default_factory=list)  # (record_id, reason)
    existing_verdicts: set = field(default_factory=set)    # (attack_id, set_hash, prior)


def materialize(records, cutoff_seq: int, current_nodes: dict,
                as_of: Optional[str] = None) -> Materialized:
    """Чиста функція: (записи, знімок Ledger, знімок графу, час) -> стан для L9."""
    recs = sorted((r for r in records if r["seq"] <= cutoff_seq), key=lambda r: r["seq"])
    if len({r["seq"] for r in recs}) != len(recs):
        raise ValueError("Ledger містить дублікати seq")
    m = Materialized(cutoff_seq=cutoff_seq)

    # Прохід 1: останній APPLIED-ROUTING на кожну атаку
    routing = {}
    for r in recs:
        if r["type"] == "ROUTING" and r["apply_status"] == "APPLIED":
            routing[r["payload"]["attack_id"]] = r

    for aid, r in sorted(routing.items()):
        p, dec = r["payload"], r["payload"]["decision"]
        if dec == "REJECT_LOGGED":
            m.rejected.append(aid)
            continue
        if dec in RISK_DECISIONS:
            mat = p.get("materiality", {})
            item = {"attack_id": aid, "decision": dec, "target_class": p["target_class"]}
            if "p" in mat:
                item["p"] = mat["p"]
            if "el" in mat:
                item["el"] = mat["el"]
            m.risk_register.append(item)
            continue
        if "target" not in p or "damage" not in p or "p" not in p.get("materiality", {}):
            raise ValueError(f"{r['record_id']}: ROUTING без target/damage/materiality.p")
        node = current_nodes.get(p["target"]["node_id"])
        if node is None or node.node_hash != p["target"]["node_hash"]:
            m.needs_reroute.append(aid)      # ціль зникла або переписана: потрібна нова L7
            continue
        m.attacks[aid] = l9.Attack(aid, p["target"]["node_id"], p["materiality"]["p"],
                                   p["damage"])
        m.evidence_ids[aid] = []

    # Прохід 2: докази
    seen_keys, cand = set(), []
    for r in recs:
        if r["type"] == "VERDICT" and r["apply_status"] == "APPLIED":
            pl = r["payload"]
            m.existing_verdicts.add((pl["attack_id"], pl["evidence_set_hash"],
                                     r6(pl["prior"])))
        if r["type"] != "EVIDENCE":
            continue
        rid, pl = r["record_id"], r["payload"]
        aid = pl["attack_id"]
        if r["apply_status"] != "APPLIED":
            m.excluded_evidence.append((rid, f"STATUS_{r['apply_status']}"))
        elif aid in m.needs_reroute:
            m.excluded_evidence.append((rid, "ATTACK_NEEDS_REROUTE"))
        elif aid not in m.attacks:
            m.excluded_evidence.append((rid, "NO_ACTIVE_ATTACK"))
        elif pl["target"]["node_hash"] != current_nodes[m.attacks[aid].target].node_hash:
            m.excluded_evidence.append((rid, "TARGET_HASH_MISMATCH"))
        elif as_of and pl.get("valid_until") and _ts(pl["valid_until"]) <= _ts(as_of):
            m.excluded_evidence.append((rid, "EXPIRED"))
        elif r.get("idempotency_key") and r["idempotency_key"] in seen_keys:
            m.excluded_evidence.append((rid, "DUPLICATE_KEY"))
        else:
            if r.get("idempotency_key"):
                seen_keys.add(r["idempotency_key"])
            cand.append(r)

    # Детермінований порядок доказів (тай-брейк у кластері не залежить від порядку в Ledger)
    for r in sorted(cand, key=lambda r: (r["payload"]["tie_break_key"], r["record_id"])):
        pl, e = r["payload"], r["payload"]["lr_effective"]
        m.attacks[pl["attack_id"]].evidence.append(
            l9.Evidence(pl["cluster_id"], pl["tier"], e["mid"], e["low"], e["high"]))
        m.evidence_ids[pl["attack_id"]].append(r["record_id"])
    return m


# ----------------------------------------------------------------- evaluate
@dataclass(frozen=True)
class Leverage:
    kind: str      # "ATTACK" | "NODE"
    id: str
    gain: float
    basis: str


@dataclass
class Evaluation:
    verdicts: dict
    p_point: float
    p_median: float
    p_p10: float
    p_p90: float
    leverage_attacks: list
    leverage_nodes: list
    regen_triggers: list
    verdict_payloads: list       # лише НОВІ (без дублікатів)
    aggregate_payload: dict
    graph_hash: str


def _cluster_choices(attack: l9.Attack, ids: list) -> list:
    """Той самий вибір, що й у l9.posterior: найсильніший доказ кластера, перший при рівності."""
    best = {}
    for e, rid in zip(attack.evidence, ids):
        ln = math.log(l9.cap(e.lr, e.tier))
        cur = best.get(e.cluster)
        if cur is None or abs(ln) > abs(cur[1]):
            best[e.cluster] = (rid, ln)
    return [{"cluster_id": c, "chosen_evidence_id": rid, "ln_lr": r6(ln)}
            for c, (rid, ln) in sorted(best.items())]


def evaluate(mat: Materialized, current_nodes: dict, goal: str, *, graph_version: int,
             as_of: Optional[str] = None, mc_n: int = 20000, seed: int = 7) -> Evaluation:
    if goal not in current_nodes:
        raise ValueError(f"мета {goal} відсутня в графі")
    nodes = _to_l9(current_nodes)
    req = l9.closure(nodes, goal)
    attacks = list(mat.attacks.values())
    verdicts = {a.id: l9.verdict(a) for a in attacks}

    def P(atks, vd=verdicts):
        return l9.p_success(nodes, goal, atks, vd)

    p_point = P(attacks)
    med, lo, hi = l9.monte_carlo(nodes, goal, attacks, verdicts, n=mc_n, seed=seed)

    # --- вердикти-payload (лише нові)
    verdict_payloads = []
    for a in attacks:
        v, post = verdicts[a.id]
        ids = mat.evidence_ids[a.id]
        set_hash = evidence_set_hash(ids)
        if (a.id, set_hash, r6(a.prior)) in mat.existing_verdicts:
            continue
        grounded = any(e.tier != "T4" for e in a.evidence)
        clusters = _cluster_choices(a, ids)
        rules = []
        if len(a.evidence) > len(clusters):
            rules.append("CLUSTER_MAX")
        if not grounded and (post >= l9.CONFIRM_AT or post <= l9.REFUTE_AT):
            rules.append("T4_CANNOT_CLOSE")
        vd_prior = dict(verdicts)
        vd_prior[a.id] = ("UNRESOLVED", a.prior)
        if a.target not in req:
            mult = 1.0
        else:
            mult = {"CONFIRMED": 0.0, "REFUTED": 1.0}.get(v, 1 - post * a.damage)
        verdict_payloads.append({
            "attack_id": a.id, "prior": r6(a.prior), "posterior": r6(post),
            "verdict": v,
            "thresholds": {"confirm_at": l9.CONFIRM_AT, "refute_at": l9.REFUTE_AT},
            "evidence_set_hash": set_hash, "evidence_ids": sorted(ids),
            "clusters": clusters, "grounded": grounded, "rules_applied": rules,
            "l9_effect": {"multiplier": r6(mult),
                          "p_success_before": r6(P(attacks, vd_prior)),
                          "p_success_after": r6(p_point)}})

    # --- важелі (структурно; для планувальника верифікацій, а не для регенерації)
    lev_attacks = []
    for a in attacks:
        if a.target in req and verdicts[a.id][0] != "REFUTED":
            rest = [x for x in attacks if x.id != a.id]
            lev_attacks.append(Leverage("ATTACK", a.id, r6(P(rest) - p_point),
                                        "P, якщо атаку знято"))
    lev_attacks.sort(key=lambda x: (-x.gain, x.id))
    lev_nodes = []
    if p_point > 0:
        lev_nodes = sorted((Leverage("NODE", n, r6(0.05 * p_point / current_nodes[n].q),
                                     "ΔP при +0.05 до q") for n in req),
                           key=lambda x: (-x.gain, x.id))

    # --- тригери регенерації: від вердиктів CONFIRMED у замиканні цілі
    by_target: dict = {}
    for a in attacks:
        if a.target in req and verdicts[a.id][0] == "CONFIRMED":
            by_target.setdefault(a.target, []).append(a.id)
    confirmed_all = {x for ids in by_target.values() for x in ids}
    p_after_repairs = P([x for x in attacks if x.id not in confirmed_all])
    triggers = []
    for tid, aids in sorted(by_target.items()):
        aids.sort()
        h = current_nodes[tid].node_hash
        triggers.append({
            "target": {"node_id": tid, "node_hash": h},
            "cause_attack_ids": aids,
            "expected_p_after": r6(p_after_repairs),   # якщо ВСІ CONFIRMED буде усунено
            "idempotency_key": sha(canon({"kind": "REGENERATE", "target_hash": h,
                                          "attack_id": aids[0]}))})

    # --- AGGREGATE-payload
    agg = {
        "goal_id": goal, "graph_version": graph_version,
        "ledger_cutoff_seq": mat.cutoff_seq,
        "mc": {"samples": mc_n, "seed": seed},
        "p_success": {"point": r6(p_point), "median": r6(med), "p10": r6(lo),
                      "p90": r6(hi)},
        "attack_summary": [{"attack_id": a.id, "verdict": verdicts[a.id][0],
                            "posterior": r6(verdicts[a.id][1]),
                            "evidence_set_hash": evidence_set_hash(mat.evidence_ids[a.id])}
                           for a in sorted(attacks, key=lambda x: x.id)],
        "risk_register": mat.risk_register,
        "residual_el_sum": r6(sum(x.get("el", 0.0) for x in mat.risk_register)),
        "needs_reroute": sorted(mat.needs_reroute),
        "off_path_attacks": sorted(a.id for a in attacks if a.target not in req),
        "excluded_evidence": [{"record_id": rid, "reason": why}
                              for rid, why in mat.excluded_evidence],
        "regeneration_triggers": triggers}
    if as_of:
        agg["as_of"] = as_of
    return Evaluation(verdicts, p_point, med, lo, hi, lev_attacks, lev_nodes, triggers,
                      verdict_payloads, agg, graph_hash(current_nodes))


def make_regeneration_task(trigger: dict, *, epoch: int, graph_version: int,
                           graph_hash_: str, p_point: float, now: str, deadline: str,
                           reserved_usd: float = 0.5) -> dict:
    """Task v1.1 для PATCHER. task_id стабільний (від ключа), а не від рядка для друку."""
    key = trigger["idempotency_key"]
    return {
        "schema_version": SCHEMA_VERSION, "task_id": f"task-regen-{key[:10]}",
        "idempotency_key": key, "kind": "REGENERATE", "status": "QUEUED", "epoch": epoch,
        "snapshot": {"graph_version": graph_version, "graph_hash": graph_hash_},
        "target": trigger["target"], "attack_id": trigger["cause_attack_ids"][0],
        "priority": {"score": r6(trigger["expected_p_after"] - p_point),
                     "enqueued_at": now},
        "budget": {"reserved": {"usd": reserved_usd}, "deadline": deadline,
                   "max_retries": 2},
        "attempt": 1,
        "isolation": {"visible_context": "SNAPSHOT_ONLY", "forbid_peer_outputs": True}}
