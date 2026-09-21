"""
Шукач істини: Goal Fidelity Sieve (Сито здорового глузду).

    fidelity(requirements, graph_0, graph_N, mutations, *, approved=(), judge_verdicts=None,
             min_alignment=None) -> FidelityResult

Що робить детерміновано (без LLM):
  * порівнює, чи ВИМОГИ користувача (заморожений GoalContract) досі підтримані графом N,
    рахуючи найгірший випадок по вузлах, що їм служать (`serves`);
  * знаходить поступки (concessions) відносно графу 0: послаблений поріг, звужений обсяг,
    додана умова, відкладений горизонт, відкинута вимога/критерій;
  * приписує поступку мутаціям Ledger і розподіляє ΔP з delta_confidence;
  * пропускає план через воротця: MUST-вимога, що не збережена, дає FAIL;
    середній бал (Alignment Score) рахується лише по SHOULD/COULD.

Роль LLM-судді: лише класифікація зачеплених вимог; суддя може ПОГІРШИТИ статус
(і мусить навести цитату), але не може його покращити.

Правило node_hash (вмістовий відбиток): sha256 від канонічного JSON з полів
{node_id, kind, statement, quantities, scope, conditions, horizon_days, serves}.
Пробіли/Unicode нормалізуються (NFC), множини сортуються, 80 і 80.0 однакові.
НЕ входять: q, n_eff, requires (зміна впевненості чи зв'язків не інвалідує доказів
про твердження; структура графу має окремий structure_hash).
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Optional

STATUS_ORDER = {"PRESERVED": 0, "WEAKENED": 1, "DROPPED": 2}
PRIORITY_ORDER = {"MUST": 0, "SHOULD": 1, "COULD": 2}
DEFAULT_WEIGHT = {"SHOULD": 2.0, "COULD": 1.0}
CONDITION_PENALTY = 0.5
_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b, "==": lambda a, b: a == b}


# ------------------------------------------------------------------ утиліти
def canon(o) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def r6(x) -> float:
    return round(float(x), 6)


def _num(x):
    x = float(x)
    return int(x) if x.is_integer() else x


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).split())


# ------------------------------------------------------------------ вузол і хеші
def node_content(n: dict) -> dict:
    qs = sorted(({"metric": _norm(q["metric"]), "comparator": q["comparator"],
                  "unit": _norm(q["unit"]), "value": _num(q["value"])}
                 for q in n.get("quantities", [])),
                key=lambda q: (q["metric"], q["comparator"], q["unit"], q["value"]))
    h = n.get("horizon_days")
    return {"node_id": n["node_id"], "kind": n["kind"], "statement": _norm(n["statement"]),
            "quantities": qs, "scope": sorted({_norm(x) for x in n.get("scope", [])}),
            "conditions": sorted({_norm(x) for x in n.get("conditions", [])}),
            "horizon_days": None if h is None else _num(h),
            "serves": sorted(n.get("serves", []))}


def node_hash(n: dict) -> str:
    return sha(canon(node_content(n)))


def seal(n: dict) -> dict:
    """Повертає копію вузла з обчисленим node_hash."""
    out = dict(n)
    out["node_hash"] = node_hash(out)
    return out


def structure_hash(graph: dict) -> str:
    return sha(canon({k: [node_hash(v), sorted(v.get("requires", []))]
                      for k, v in sorted(graph.items())}))


def contract_hash(requirements) -> str:
    return sha(canon(sorted(requirements, key=lambda r: r["id"])))


def _check_hashes(graph: dict, label: str) -> None:
    for nid, n in graph.items():
        if n["node_id"] != nid:
            raise ValueError(f"{label}: ключ {nid} != node_id {n['node_id']}")
        if n.get("node_hash") != node_hash(n):
            raise ValueError(f"{label}: node_hash вузла {nid} не збігається з вмістом")


# ------------------------------------------------------------------ оцінка вимоги на графі
def _agg(vals, c):
    if c["comparator"] in (">=", ">"):
        return min(vals)                       # найгірший випадок: найнижча гарантія
    if c["comparator"] in ("<=", "<"):
        return max(vals)
    return max(vals, key=lambda v: abs(v - c["value"]))


def _met(c, v) -> bool:
    return _OPS[c["comparator"]](v, _num(c["value"]))


def _worse(c, f, s) -> bool:
    if c["comparator"] in (">=", ">"):
        return f < s
    if c["comparator"] in ("<=", "<"):
        return f > s
    return abs(f - c["value"]) > abs(s - c["value"])


def _ratio(c, v) -> float:
    t = _num(c["value"])
    if c["comparator"] in (">=", ">"):
        r = v / t if t > 0 else 0.0
    elif c["comparator"] in ("<=", "<"):
        r = t / v if v > 0 else 1.0
    else:
        r = 1 - abs(v - t) / abs(t) if t else 0.0
    return max(0.0, min(r, 0.99))              # невиконаний поріг ніколи не дає 1.0


def _collect(req: dict, graph: dict) -> dict:
    serving = sorted(i for i, n in graph.items() if req["id"] in n["serves"])
    nodes = [graph[i] for i in serving]
    crit = []
    for c in req.get("criteria", []):
        vals, seen = [], False
        for n in nodes:
            for q in n.get("quantities", []):
                if q["metric"] != c["metric"]:
                    continue
                seen = True
                if q["comparator"] == c["comparator"] and q["unit"] == c["unit"]:
                    vals.append(_num(q["value"]))
        crit.append({"value": _agg(vals, c) if vals else None,
                     "issue": None if vals else ("MISMATCH" if seen else "UNSUPPORTED")})
    hz = [_num(n["horizon_days"]) for n in nodes if n.get("horizon_days") is not None]
    return {"serving": serving, "hashes": frozenset(node_hash(n) for n in nodes),
            "criteria": crit,
            "scope": {_norm(x) for n in nodes for x in n.get("scope", [])},
            "conditions": {_norm(x) for n in nodes for x in n.get("conditions", [])},
            "horizon": max(hz) if hz else None}


def _evaluate(req: dict, col: dict, start: Optional[dict] = None):
    """(статус, покриття) за детермінованими перевірками. Покриття = найслабша ланка."""
    if not col["serving"]:
        return "DROPPED", 0.0
    comps = []
    for c, a in zip(req.get("criteria", []), col["criteria"]):
        v = a["value"]
        comps.append(0.0 if v is None else (1.0 if _met(c, v) else _ratio(c, v)))
    if req.get("scope"):
        need = {_norm(x) for x in req["scope"]}
        comps.append(len(need & col["scope"]) / len(need))
    if req.get("max_horizon_days") is not None and col["horizon"] is not None:
        h, m = col["horizon"], req["max_horizon_days"]
        comps.append(1.0 if h <= m else (m / h if h > 0 else 1.0))
    if start is not None and start["serving"] and (col["conditions"] - start["conditions"]):
        comps.append(CONDITION_PENALTY)
    if not comps:
        return "PRESERVED", 1.0
    cov = min(comps)
    if cov == 0.0:
        return "DROPPED", 0.0
    return ("PRESERVED", 1.0) if cov >= 1.0 else ("WEAKENED", cov)


# ------------------------------------------------------------------ поступки
def _cid(req_id, kind, key) -> str:
    return "conc-" + sha(canon({"r": req_id, "k": kind, "d": key}))[:16]


def _base(req, c0, cN) -> dict:
    return {"requirement_id": req["id"], "priority": req["priority"],
            "nodes_before": c0["serving"], "nodes_after": cN["serving"]}


def _concessions(req: dict, c0: dict, cN: dict) -> list:
    if not c0["serving"]:
        return []                                  # на старті не було покриття: це прогалина, не поступка
    out, rid, base = [], req["id"], _base(req, c0, cN)

    def add(kind, effect, detail, key):
        out.append({**base, "concession_id": _cid(rid, kind, key), "kind": kind,
                    "effect_on_requirement": effect, "detail": detail})

    if not cN["serving"]:
        add("DROPPED_REQUIREMENT", "DROPPED", {"from": c0["serving"], "to": []}, ["all"])
        return out
    for c, a0, aN in zip(req.get("criteria", []), c0["criteria"], cN["criteria"]):
        s, f = a0["value"], aN["value"]
        if s is None:
            continue
        d = {"metric": c["metric"], "comparator": c["comparator"], "unit": c["unit"]}
        key = [c["metric"], c["comparator"], c["unit"], s, f]
        if f is None:
            add("DROPPED_CRITERION", "DROPPED", {**d, "from": s, "to": None}, key)
        elif _worse(c, f, s):
            det = {**d, "from": s, "to": f}
            if s:
                det["magnitude"] = r6(abs(s - f) / abs(s))
            add("RELAXED_THRESHOLD", "NONE" if _met(c, f) else "WEAKENED", det, key)
    removed = sorted(c0["scope"] - cN["scope"])
    if removed:
        need = {_norm(x) for x in req.get("scope", [])}
        eff = "WEAKENED" if need and (need - cN["scope"]) else "NONE"
        add("NARROWED_SCOPE", eff, {"from": sorted(c0["scope"]), "to": sorted(cN["scope"]),
                                    "removed_tags": removed}, removed)
    if c0["horizon"] is not None and cN["horizon"] is not None and cN["horizon"] > c0["horizon"]:
        m = req.get("max_horizon_days")
        eff = "WEAKENED" if m is not None and cN["horizon"] > m else "NONE"
        add("DELAYED_HORIZON", eff, {"metric": "horizon_days", "unit": "days",
                                     "from": c0["horizon"], "to": cN["horizon"]},
            [c0["horizon"], cN["horizon"]])
    added = sorted(cN["conditions"] - c0["conditions"])
    if added:
        add("ADDED_CONDITION", "WEAKENED", {"from": sorted(c0["conditions"]),
                                            "to": sorted(cN["conditions"]),
                                            "added_conditions": added}, added)
    return out


# ------------------------------------------------------------------ результат
@dataclass
class FidelityResult:
    gate: dict
    alignment_score: float
    must_summary: dict
    requirements: list
    concessions: list            # payload-и CONCESSION (з delta_p)
    node_diff: dict
    judge_queue: list
    p_trajectory: Optional[dict]
    fidelity_payload: dict


def fidelity(requirements, graph_0: dict, graph_N: dict, mutations=(), *, approved=(),
             judge_verdicts: Optional[dict] = None,
             min_alignment: Optional[float] = None) -> FidelityResult:
    _check_hashes(graph_0, "graph_0")
    _check_hashes(graph_N, "graph_N")
    reqs = sorted(requirements, key=lambda r: r["id"])
    if len({r["id"] for r in reqs}) != len(reqs):
        raise ValueError("дублікати id вимог")
    approved = set(approved)
    judge_verdicts = judge_verdicts or {}
    muts = sorted((m for m in mutations
                   if m["type"] == "MUTATION" and m.get("apply_status", "APPLIED") == "APPLIED"),
                  key=lambda m: m["seq"])

    items, concessions, judge_queue, cols = [], [], [], {}
    for r in reqs:
        c0, cN = _collect(r, graph_0), _collect(r, graph_N)
        cols[r["id"]] = (c0, cN)
        st0, _ = _evaluate(r, c0)
        st, cov = _evaluate(r, cN, start=c0)
        concs = _concessions(r, c0, cN)
        touched = c0["hashes"] != cN["hashes"]
        item = {"requirement_id": r["id"], "priority": r["priority"], "status": st,
                "status_at_start": st0, "coverage": r6(cov), "touched": touched,
                "judge_pending": False, "serving_nodes": cN["serving"]}
        jv = judge_verdicts.get(r["id"])
        if jv:
            js = jv["status"]
            if js != "PRESERVED" and not jv.get("quote"):
                raise ValueError(f"суддя не навів цитату для {r['id']}: вердикт без доказу")
            if STATUS_ORDER[js] > STATUS_ORDER[st]:            # суддя може лише погіршити
                concs.append({**_base(r, c0, cN),
                              "concession_id": _cid(r["id"], "JUDGE_FINDING",
                                                    [js, sha(jv["quote"])[:12]]),
                              "kind": "JUDGE_FINDING", "effect_on_requirement": js,
                              "detail": {"from": st, "to": js, "quote": jv["quote"],
                                         "deterministic_status": st}})
                st, cov = js, (min(cov, 0.5) if js == "WEAKENED" else 0.0)
                item.update(status=st, coverage=r6(cov))
                item["judge"] = {"status": js, "quote": jv["quote"], "applied": True}
            else:
                item["judge"] = {"status": js, "quote": jv.get("quote", ""), "applied": False}
        elif touched:
            item["judge_pending"] = True
            judge_queue.append(r["id"])
        items.append(item)
        concessions += concs

    # --- атрибуція мутаціям і розподіл ΔP
    def ids(m, key):
        return {x["node_id"] for x in m["payload"].get(key, [])}

    matched: dict = {}
    for c in concessions:
        c0, cN = cols[c["requirement_id"]]
        matched[c["concession_id"]] = [
            m for m in muts if (ids(m, "nodes_removed") & set(c0["serving"]))
            or (ids(m, "nodes_added") & set(cN["serving"]))]
    load = {}
    for ms in matched.values():
        for m in ms:
            load[m["record_id"]] = load.get(m["record_id"], 0) + 1
    for c in concessions:
        ms = matched[c["concession_id"]]
        c["caused_by"] = {
            "mutation_record_ids": sorted(m["record_id"] for m in ms),
            "attack_ids": sorted({m["payload"]["cause_attack_id"] for m in ms
                                  if m["payload"].get("cause_attack_id")})}
        deltas = [(m["payload"]["delta_confidence"]["after"]
                   - m["payload"]["delta_confidence"]["before"]) / load[m["record_id"]]
                  for m in ms if m["payload"].get("delta_confidence")]
        if deltas:
            c["delta_p"] = r6(sum(deltas))
        c["needs_user_decision"] = (c["priority"] == "MUST"
                                    and c["effect_on_requirement"] != "NONE")
    concessions.sort(key=lambda c: (PRIORITY_ORDER[c["priority"]], c["requirement_id"],
                                    c["kind"], c["concession_id"]))

    # --- воротця
    by_id = {r["id"]: r for r in reqs}
    reasons, consent = [], False
    for it in items:
        r = by_id[it["requirement_id"]]
        if r["priority"] != "MUST":
            continue
        if r["source"] != "USER_STATED" and not r["confirmed_by_user"]:
            reasons.append({"rule": "UNCONFIRMED_MUST", "verdict": "NEEDS_REVIEW",
                            "requirement_id": r["id"],
                            "detail": "інтерпретацію вимоги користувач не підтвердив"})
        if it["status"] == "PRESERVED":
            if it["judge_pending"]:
                reasons.append({"rule": "JUDGE_PENDING", "verdict": "NEEDS_REVIEW",
                                "requirement_id": r["id"],
                                "detail": "вимога зачеплена патчами, вердикту судді немає"})
            continue
        cons = [c for c in concessions
                if c["requirement_id"] == r["id"] and c["effect_on_requirement"] != "NONE"]
        if it["status_at_start"] != "PRESERVED":
            reasons.append({"rule": "MUST_UNMET", "verdict": "FAIL", "requirement_id": r["id"],
                            "detail": "вимога не виконувалась уже в початковому плані"})
        elif cons and all(c["concession_id"] in approved for c in cons):
            consent = True
            reasons.append({"rule": "CONSENT_USED", "verdict": "PASS_WITH_CONSENT",
                            "requirement_id": r["id"],
                            "detail": "поступки затверджено користувачем"})
        else:
            reasons.append({"rule": "MUST_DROPPED" if it["status"] == "DROPPED"
                            else "MUST_WEAKENED", "verdict": "FAIL",
                            "requirement_id": r["id"], "detail": "MUST-вимога не збережена"})

    soft = [it for it in items if it["priority"] != "MUST"]
    w = {it["requirement_id"]: by_id[it["requirement_id"]].get(
        "weight", DEFAULT_WEIGHT[it["priority"]]) for it in soft}
    score = (sum(w[i["requirement_id"]] * i["coverage"] for i in soft) / sum(w.values())
             if soft else 1.0)
    if min_alignment is not None and score < min_alignment:
        reasons.append({"rule": "LOW_ALIGNMENT", "verdict": "NEEDS_REVIEW",
                        "detail": f"Alignment Score {score:.2f} < {min_alignment}"})

    verdicts = {x["verdict"] for x in reasons}
    result = ("FAIL" if "FAIL" in verdicts else "NEEDS_REVIEW" if "NEEDS_REVIEW" in verdicts
              else "PASS_WITH_CONSENT" if consent else "PASS")
    gate = {"result": result,
            "reasons": [{k: v for k, v in x.items() if k != "verdict"} for x in reasons]}

    musts = [it for it in items if it["priority"] == "MUST"]
    must_summary = {"total": len(musts),
                    "preserved": sum(i["status"] == "PRESERVED" for i in musts),
                    "weakened": sum(i["status"] == "WEAKENED" for i in musts),
                    "dropped": sum(i["status"] == "DROPPED" for i in musts)}

    a, b = set(graph_0), set(graph_N)
    node_diff = {"added": sorted(b - a), "removed": sorted(a - b),
                 "changed": sorted(i for i in a & b
                                   if node_hash(graph_0[i]) != node_hash(graph_N[i]))}
    with_delta = [m for m in muts if m["payload"].get("delta_confidence")]
    traj = ({"start": with_delta[0]["payload"]["delta_confidence"]["before"],
             "end": with_delta[-1]["payload"]["delta_confidence"]["after"]}
            if with_delta else None)

    concession_payloads = [{k: v for k, v in c.items()} for c in concessions]
    payload = {
        "contract_hash": contract_hash(reqs), "graph_0_hash": structure_hash(graph_0),
        "graph_n_hash": structure_hash(graph_N), "gate": gate,
        "alignment_score": r6(score), "must_summary": must_summary,
        "requirements": items,
        "concession_ids": [c["concession_id"] for c in concession_payloads],
        "approved_concession_ids": sorted(approved & {c["concession_id"]
                                                      for c in concession_payloads}),
        "node_diff": node_diff, "judge_queue": sorted(judge_queue),
        "mutation_record_ids": [m["record_id"] for m in muts]}
    if traj:
        payload["p_trajectory"] = traj
    return FidelityResult(gate, r6(score), must_summary, items, concession_payloads,
                          node_diff, sorted(judge_queue), traj, payload)


# ------------------------------------------------------------------ CLI-блок
def _describe(c: dict) -> str:
    d, k = c["detail"], c["kind"]
    if k == "RELAXED_THRESHOLD":
        return f'{d["metric"]} {d["comparator"]}: {d["from"]} → {d["to"]} {d["unit"]}'
    if k == "DROPPED_CRITERION":
        return f'{d["metric"]}: {d["from"]} {d["unit"]} → критерій більше не підтримано'
    if k == "NARROWED_SCOPE":
        return "обсяг звужено: без " + ", ".join(d["removed_tags"])
    if k == "DELAYED_HORIZON":
        return f'строк: {d["from"]} → {d["to"]} днів'
    if k == "ADDED_CONDITION":
        return "нова умова: " + "; ".join(d["added_conditions"])
    if k == "DROPPED_REQUIREMENT":
        return "вимогу відкинуто повністю"
    return "висновок судді: " + d.get("quote", "")


def render_tradeoffs(res: FidelityResult, requirements) -> str:
    text = {r["id"]: r["text"] for r in requirements}
    L = [f'Alignment Score: {res.alignment_score:.2f}    Gate: {res.gate["result"]}']
    ms = res.must_summary
    L.append(f'MUST: {ms["preserved"]}/{ms["total"]} збережено, '
             f'{ms["weakened"]} послаблено, {ms["dropped"]} відкинуто')
    for it in res.requirements:
        L.append(f'  {it["priority"]:<6} {it["requirement_id"]:<3} {it["status"]:<9}'
                 f' {it["coverage"]:.2f}  {text[it["requirement_id"]]}')
    p = res.p_trajectory
    L.append("Компроміси" + (f' (P {p["start"]:.2f} → {p["end"]:.2f})' if p else "") + ":")
    if not res.concessions:
        L.append("  (немає)")
    for c in res.concessions:
        dp = f' {c["delta_p"]:+.3f}' if "delta_p" in c else "      "
        flag = "  ← потрібне рішення" if c["needs_user_decision"] else (
            "  (запас, впливу на вимогу немає)" if c["effect_on_requirement"] == "NONE" else "")
        L.append(f'  {c["requirement_id"]} [{c["priority"]}]{dp}  {_describe(c)}{flag}')
    for x in res.gate["reasons"]:
        L.append(f'  ! {x["rule"]}' + (f' ({x["requirement_id"]})' if "requirement_id" in x else ""))
    return "\n".join(L)
