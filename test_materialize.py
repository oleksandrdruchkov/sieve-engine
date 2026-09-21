"""
Прогін truth_seeker_materialize на truth_seeker_examples.json + сконструйовані сценарії.
Запуск:  python3 test_materialize.py      (код виходу 1, якщо є провали)

Валідатор нижче реалізує лише підмножину JSON Schema, яку використовують схеми
(type, enum, const, pattern, min/max, required, properties, additionalProperties:false,
allOf, if/then, $ref). У своєму середовищі краще перевірити ще й стандартним
`jsonschema` / `ajv`.
"""
import copy
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import truth_seeker_materialize as tm  # noqa: E402

SCHEMA = json.load(open(os.path.join(HERE, "truth_seeker_schemas.json"), encoding="utf-8"))
EX = json.load(open(os.path.join(HERE, "truth_seeker_examples.json"), encoding="utf-8"))


# ------------------------------------------------------------ міні-валідатор
def resolve(r):
    node = SCHEMA
    for part in r[2:].split("/"):
        node = node[part]
    return node


def check(inst, sch, path="$"):
    if "$ref" in sch:
        return check(inst, resolve(sch["$ref"]), path)
    errs, t = [], sch.get("type")
    if t:
        ok = {"string": isinstance(inst, str),
              "integer": isinstance(inst, int) and not isinstance(inst, bool),
              "number": isinstance(inst, (int, float)) and not isinstance(inst, bool),
              "boolean": isinstance(inst, bool), "object": isinstance(inst, dict),
              "array": isinstance(inst, list), "null": inst is None}[t]
        if not ok:
            return [f"{path}: очікувався {t}"]
    if "enum" in sch and inst not in sch["enum"]:
        errs.append(f"{path}: {inst!r} не з enum")
    if "const" in sch and inst != sch["const"]:
        errs.append(f"{path}: очікувалась константа {sch['const']!r}")
    if isinstance(inst, str):
        if "pattern" in sch and not re.search(sch["pattern"], inst):
            errs.append(f"{path}: не збігається з pattern")
        if len(inst) < sch.get("minLength", 0):
            errs.append(f"{path}: закоротка")
        if sch.get("format") == "date-time":
            try:
                datetime.fromisoformat(inst.replace("Z", "+00:00"))
            except ValueError:
                errs.append(f"{path}: не date-time")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        for k, bad in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b),
                       ("exclusiveMinimum", lambda a, b: a <= b),
                       ("exclusiveMaximum", lambda a, b: a >= b)):
            if k in sch and bad(inst, sch[k]):
                errs.append(f"{path}: порушено {k}")
    if isinstance(inst, list):
        if len(inst) < sch.get("minItems", 0):
            errs.append(f"{path}: замало елементів")
        if "items" in sch:
            for i, x in enumerate(inst):
                errs += check(x, sch["items"], f"{path}[{i}]")
    if isinstance(inst, dict):
        for r in sch.get("required", []):
            if r not in inst:
                errs.append(f"{path}: відсутнє поле '{r}'")
        props = sch.get("properties", {})
        for k, v in inst.items():
            if k in props:
                errs += check(v, props[k], f"{path}.{k}")
            elif sch.get("additionalProperties") is False:
                errs.append(f"{path}: зайве поле '{k}'")
    for sub in sch.get("allOf", []):
        errs += check(inst, sub, path)
    if "if" in sch and not check(inst, sch["if"], path):
        errs += check(inst, sch["then"], path)
    return errs


def ref(n): return {"$ref": f"#/$defs/{n}"}


# ------------------------------------------------------------ каркас тестів
FAILS = []


def expect(name, cond, detail=""):
    print(("   ✓ " if cond else "   ✗ ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def near(a, b, tol=2e-3): return abs(a - b) <= tol


ORCH = {"id": "orchestrator", "role": "ORCHESTRATOR", "kind": "SYSTEM"}
JUDGE = {"id": "judge-1", "role": "JUDGE", "kind": "LLM", "name": "model-b", "version": "2.0"}
TOOL = {"id": "verifier-9", "role": "VERIFIER", "kind": "TOOL", "name": "calc", "version": "1.0"}

LEDGER = EX["ledger"]
V13 = tm.node_states(EX["graph_v13"]["nodes"])
GOAL = EX["graph_v13"]["goal"]
NOW = "2026-09-21T10:00:00Z"


def run(records, cutoff, nodes=V13, as_of=NOW, gv=13):
    mat = tm.materialize(records, cutoff, nodes, as_of=as_of)
    ev = tm.evaluate(mat, nodes, GOAL, graph_version=gv, as_of=as_of)
    return mat, ev


def commit(records, ev, gv=13):
    """Як зробив би оркестратор: дописати нові VERDICT і AGGREGATE. Повертає (ланцюг, нові)."""
    out, new = list(records), []
    for vp in ev.verdict_payloads:
        new.append(tm.append_record(out, created_at=NOW, type="VERDICT", epoch=4,
                                    graph_version=gv, apply_status="APPLIED", executor=ORCH,
                                    task_id="sys-agg-4", payload=vp))
    new.append(tm.append_record(out, created_at=NOW, type="AGGREGATE", epoch=4,
                                graph_version=gv, apply_status="APPLIED", executor=ORCH,
                                task_id="sys-agg-4", payload=ev.aggregate_payload))
    return out, new


def schema_ok(recs):
    return all(not check(r, ref("LedgerRecord")) for r in recs)


def routing_rec(records, aid, target, decision, damage, p, el, tclass="DIRECT", **extra):
    return tm.append_record(
        records, created_at=NOW, type="ROUTING", epoch=4, graph_version=13,
        apply_status="APPLIED", executor=JUDGE,
        payload={"attack_id": aid, "decision": decision, "reason_code": "TEST",
                 "scope": "IN_SCOPE", "target_class": tclass, "target": target,
                 "damage": damage, "materiality": {"p": p, "impact": 5.0, "el": el},
                 "judges": ["judge-1"], **extra})


def node_ref(nid, nodes=EX["graph_v13"]["nodes"]):
    return {"node_id": nid, "node_hash": nodes[nid]["node_hash"]}


print("Перевірка вхідних даних")
expect("ланцюг Ledger цілий", not tm.verify_chain(LEDGER))
expect("усі записи прикладів валідні за схемою v1.1",
       all(not check(r, ref("LedgerRecord")) for r in LEDGER))

# ------------------------------------------------------------ S1
print("\nS1. Повний Ledger (seq<=6), граф v13: ідемпотентність і STALE")
mat, ev = run(LEDGER, 6)
v = ev.verdicts["attack-X"]
expect("attack-X: REFUTED, posterior ≈ 0.0123", v[0] == "REFUTED" and near(v[1], 0.0123, 5e-4),
       f"{v[0]} {v[1]:.4f}")
expect("P(успіх) = 0.581 (REFUTED знімає атаку)", near(ev.p_point, 0.5814), f"{ev.p_point:.4f}")
expect("нових VERDICT немає (збігається з rec-0003)", ev.verdict_payloads == [])
expect("STALE-доказ rec-0006 виключено",
       ("rec-0006", "STATUS_STALE") in mat.excluded_evidence)
expect("регенерації не потрібні", ev.regen_triggers == [])
s1_agg = ev.aggregate_payload

# ------------------------------------------------------------ S2
print("\nS2. Знімок Ledger: cutoff_seq=1 (лише LLM-доказ) відтворює сценарій 2")
mat, ev = run(LEDGER, 1)
v = ev.verdicts["attack-X"]
expect("UNRESOLVED, p_a ≈ 0.111", v[0] == "UNRESOLVED" and near(v[1], 0.1111, 5e-4),
       f"{v[0]} {v[1]:.4f}")
expect("P(успіх) ≈ 0.517", near(ev.p_point, 0.5168), f"{ev.p_point:.4f}")
expect("вердикт ще не записаний, тому випускається новий", len(ev.verdict_payloads) == 1)
expect("важіль атаки X ≈ +0.065", ev.leverage_attacks and near(ev.leverage_attacks[0].gain, 0.0646, 2e-3),
       f"{ev.leverage_attacks[0]}")
chain, new = commit(LEDGER, ev)
expect("нові записи проходять схему й ланцюг", schema_ok(new) and not tm.verify_chain(chain))

# ------------------------------------------------------------ S3
print("\nS3. Повторний ROUTING (prior 0.25) НЕ стирає докази")
recs = copy.deepcopy(LEDGER)
routing_rec(recs, "attack-X", node_ref("node-B"), "EXECUTE_VERIFICATION", 1.0, 0.25, 1.1,
            supersedes="rec-0000")
mat, ev = run(recs, len(recs) - 1)
expect("докази збережені (rec-0001, rec-0002)",
       mat.evidence_ids["attack-X"] == ["rec-0001", "rec-0002"], str(mat.evidence_ids))
expect("prior оновлено з останнього ROUTING",
       near(mat.attacks["attack-X"].prior, 0.25, 1e-9))
expect("новий VERDICT, бо змінився prior", len(ev.verdict_payloads) == 1
       and near(ev.verdict_payloads[0]["posterior"], 0.0164, 5e-4),
       str(ev.verdict_payloads[0]["posterior"]))

# ------------------------------------------------------------ S4
print("\nS4. RISK_REGISTER не зникає і не впливає на P")
recs = copy.deepcopy(LEDGER)
routing_rec(recs, "attack-V", node_ref("node-C"), "RISK_REGISTER", 0.5, 0.04, 1.2,
            tclass="ADJACENT")
mat, ev = run(recs, len(recs) - 1)
expect("attack-V у реєстрі ризиків", [x["attack_id"] for x in mat.risk_register] == ["attack-V"])
expect("residual_el_sum = 1.2", near(ev.aggregate_payload["residual_el_sum"], 1.2, 1e-9))
expect("P(успіх) не змінилось", near(ev.p_point, 0.5814))

# ------------------------------------------------------------ S5
print("\nS5. CONFIRMED-атака: P = 0 і запускається регенерація")
recs = copy.deepcopy(LEDGER)
routing_rec(recs, "attack-W", node_ref("node-A"), "EXECUTE_VERIFICATION", 1.0, 0.3, 2.7)
nxt = f"rec-{len(recs):04d}"
tm.append_record(
    recs, created_at=NOW, type="EVIDENCE", epoch=4, graph_version=13, apply_status="APPLIED",
    executor=TOOL, task_id="task-0050", idempotency_key=tm.sha("VERIFY|node-A|calc|1.0"),
    payload={"attack_id": "attack-W", "target": node_ref("node-A"), "prediction_id": "pred-w",
             "cluster_id": "calc", "tier": "T0",
             "provenance": {"source_type": "COMPUTATION", "retrieved_at": NOW,
                            "content_hash": tm.sha("calc-out"), "code_hash": tm.sha("calc-src"),
                            "screened_for_injection": True},
             "observation": {"summary": "Обчислення показало, що обмеження порушується."},
             "direction": "SUPPORTS_ATTACK",
             "lr_raw": {"low": 60, "mid": 100, "high": 100},
             "lr_effective": {"low": 60, "mid": 100, "high": 100},
             "cap_applied": False, "tie_break_key": nxt})
mat, ev = run(recs, len(recs) - 1)
v = ev.verdicts["attack-W"]
expect("attack-W: CONFIRMED (T0, заземлено)", v[0] == "CONFIRMED", f"{v[0]} {v[1]:.4f}")
expect("P(успіх) = 0", ev.p_point == 0.0 and ev.p_median == 0.0)
expect("чутливості вузлів порожні при P=0 (не інформативні)", ev.leverage_nodes == [])
expect("важіль атаки W = P після усунення ≈ 0.581",
       ev.leverage_attacks[0].id == "attack-W" and near(ev.leverage_attacks[0].gain, 0.5814),
       str(ev.leverage_attacks[0]))
expect("рівно один тригер регенерації на node-A",
       len(ev.regen_triggers) == 1 and ev.regen_triggers[0]["target"]["node_id"] == "node-A"
       and ev.regen_triggers[0]["cause_attack_ids"] == ["attack-W"])
expect("expected_p_after ≈ 0.581", near(ev.regen_triggers[0]["expected_p_after"], 0.5814))
task = tm.make_regeneration_task(ev.regen_triggers[0], epoch=4, graph_version=13,
                                 graph_hash_=ev.graph_hash, p_point=ev.p_point, now=NOW,
                                 deadline="2026-09-21T10:30:00Z")
terr = check(task, ref("Task"))
expect("REGENERATE-задача валідна за схемою Task", not terr, "; ".join(terr))
expect("task_id стабільний і не залежить від рядка для друку",
       task["task_id"].startswith("task-regen-") and task["task_id"] != "task-regen-q-4")
task2 = tm.make_regeneration_task(ev.regen_triggers[0], epoch=5, graph_version=13,
                                  graph_hash_=ev.graph_hash, p_point=ev.p_point, now=NOW,
                                  deadline="2026-09-21T10:30:00Z")
expect("той самий idempotency_key в наступній епосі (single-flight)",
       task["idempotency_key"] == task2["idempotency_key"])
chain, new = commit(recs, ev)
expect("VERDICT+AGGREGATE проходять схему й ланцюг", schema_ok(new) and not tm.verify_chain(chain),
       "; ".join(e for r in new for e in check(r, ref("LedgerRecord"))))
expect("AGGREGATE містить тригер регенерації",
       new[-1]["payload"]["regeneration_triggers"][0]["target"]["node_id"] == "node-A")
s5_task = task

# ------------------------------------------------------------ S6
print("\nS6. Вузол B пропатчено (інший hash): атака X потребує повторної L7")
patched = dict(V13)
patched["node-B"] = tm.NodeState(V13["node-B"].q, tm.sha("node-B-v2"), V13["node-B"].requires)
mat, ev = run(LEDGER, 6, nodes=patched)
expect("attack-X у needs_reroute", mat.needs_reroute == ["attack-X"])
expect("її докази виключено (ATTACK_NEEDS_REROUTE)",
       ("rec-0001", "ATTACK_NEEDS_REROUTE") in mat.excluded_evidence
       and ("rec-0002", "ATTACK_NEEDS_REROUTE") in mat.excluded_evidence)
expect("атака не впливає на P", near(ev.p_point, 0.5814))

# ------------------------------------------------------------ S7
print("\nS7. TTL: після 2026-12-21 первинне джерело застаріло")
mat, ev = run(LEDGER, 6, as_of="2027-01-01T00:00:00Z")
expect("rec-0002 виключено (EXPIRED)", ("rec-0002", "EXPIRED") in mat.excluded_evidence)
expect("attack-X знову UNRESOLVED, P ≈ 0.517",
       ev.verdicts["attack-X"][0] == "UNRESOLVED" and near(ev.p_point, 0.5168))

# ------------------------------------------------------------ S8
print("\nS8. Детермінізм: порядок записів у списку не впливає на результат")
_, ev_rev = run(list(reversed(LEDGER)), 6)
expect("AGGREGATE-payload ідентичний після перестановки", ev_rev.aggregate_payload == s1_agg)
_, ev_again = run(LEDGER, 6)
expect("повторний виклик дає той самий результат",
       ev_again.aggregate_payload == s1_agg)

# ------------------------------------------------------------ схема
print("\nS9. Схема v1.1 відхиляє ROUTING без target/damage")
bad = copy.deepcopy(LEDGER[0])
del bad["payload"]["target"]
del bad["payload"]["damage"]
errs = check(bad, ref("LedgerRecord"))
expect("EXECUTE_VERIFICATION без target і damage відхиляється",
       any("target" in e for e in errs) and any("damage" in e for e in errs), errs[0] if errs else "")
bad = copy.deepcopy(LEDGER[0])
del bad["payload"]["materiality"]
expect("EXECUTE_VERIFICATION без materiality відхиляється",
       bool(check(bad, ref("LedgerRecord"))))
try:
    tm.materialize([{**LEDGER[0], "payload": {k: v for k, v in LEDGER[0]["payload"].items()
                                              if k != "damage"}}], 0, V13)
    expect("materialize кидає ValueError на неповний ROUTING", False)
except ValueError:
    expect("materialize кидає ValueError на неповний ROUTING (fail-loud)", True)

print("\n=== РЕГЕНЕРАЦІЙНА ЗАДАЧА зі сценарію S5 ===")
print(json.dumps(s5_task, ensure_ascii=False, indent=2))
print(f"\nПідсумок: {'усе пройдено' if not FAILS else 'ПРОВАЛИ: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
