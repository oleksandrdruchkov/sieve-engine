"""
Тести Goal Fidelity Sieve.  Запуск:  python3 test_fidelity.py  (код виходу 1 при провалах)
Валідатор нижче реалізує підмножину JSON Schema, яку використовують схеми; у своєму
середовищі додатково прогоніть їх стандартним `jsonschema` / `ajv`.
"""
import copy
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import truth_seeker_fidelity as fid  # noqa: E402
import truth_seeker_materialize as tm  # noqa: E402

SCHEMA = json.load(open(os.path.join(HERE, "truth_seeker_schemas.json"), encoding="utf-8"))
FX = json.load(open(os.path.join(HERE, "truth_seeker_fidelity_examples.json"), encoding="utf-8"))


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


FAILS = []


def expect(name, cond, detail=""):
    print(("   ✓ " if cond else "   ✗ ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def near(a, b, tol=1e-6): return abs(a - b) <= tol


REQS, G0, GN, CHAIN = FX["requirements"], FX["graph_0"], FX["graph_n"], FX["ledger"]
MUTS = [r for r in CHAIN if r["type"] == "MUTATION"]
R = {r["id"]: r for r in REQS}


def variant(g, replace=None, drop=()):
    out = {k: dict(v) for k, v in g.items() if k not in drop}
    for old, new in (replace or {}).items():
        out.pop(old, None)
        out[new["node_id"]] = new
    return out


def item(res, rid): return next(i for i in res.requirements if i["requirement_id"] == rid)
def rules(res): return [(x["rule"], x.get("requirement_id")) for x in res.gate["reasons"]]
def concs(res, rid): return [c for c in res.concessions if c["requirement_id"] == rid]


# ------------------------------------------------------------------ T0
print("T0. Вхідні дані відповідають схемам і хеш-правилу")
expect("вимоги валідні за Requirement", all(not check(r, ref("Requirement")) for r in REQS))
nodes = list(G0.values()) + list(GN.values())
expect("вузли валідні за Node", all(not check(n, ref("Node")) for n in nodes),
       "; ".join(e for n in nodes for e in check(n, ref("Node"))))
expect("node_hash збігається з правилом", all(n["node_hash"] == fid.node_hash(n) for n in nodes))
expect("ланцюг Ledger цілий", not tm.verify_chain(CHAIN))
errs = [e for r in CHAIN for e in check(r, ref("LedgerRecord"))]
expect("усі записи (GOAL_FREEZE, MUTATION, CONCESSION, FIDELITY) валідні", not errs, "; ".join(errs[:2]))
expect("contract_hash у GOAL_FREEZE збігається",
       CHAIN[0]["payload"]["contract_hash"] == fid.contract_hash(REQS))

# ------------------------------------------------------------------ T1
print("\nT1. Основний сценарій: масштабованість 10 000 → 1 000 rps, звужене шифрування")
res = fid.fidelity(REQS, G0, GN, MUTS)
expect("gate = FAIL, причини: MUST_WEAKENED для R1 і R2",
       res.gate["result"] == "FAIL" and rules(res) == [("MUST_WEAKENED", "R1"), ("MUST_WEAKENED", "R2")],
       str(rules(res)))
r1 = item(res, "R1")
expect("R1: WEAKENED, покриття 0.10 = 1000/10000 (найгірший вузол, не середнє)",
       r1["status"] == "WEAKENED" and near(r1["coverage"], 0.1), str(r1["coverage"]))
expect("R1 на старті виконувалась (status_at_start = PRESERVED)", r1["status_at_start"] == "PRESERVED")
c = concs(res, "R1")[0]
expect("поступка R1: RELAXED_THRESHOLD 12000 → 1000, magnitude 0.916667",
       c["kind"] == "RELAXED_THRESHOLD" and c["detail"]["from"] == 12000
       and c["detail"]["to"] == 1000 and near(c["detail"]["magnitude"], 0.916667))
expect("поступка R1 приписана rec-0001 / attack-101, ΔP = +0.11",
       c["caused_by"] == {"mutation_record_ids": ["rec-0001"], "attack_ids": ["attack-101"]}
       and near(c["delta_p"], 0.11))
kinds2 = sorted(x["kind"] for x in concs(res, "R2"))
expect("R2: NARROWED_SCOPE + ADDED_CONDITION, coverage 0.5",
       kinds2 == ["ADDED_CONDITION", "NARROWED_SCOPE"] and near(item(res, "R2")["coverage"], 0.5))
expect("ΔP мутації rec-0003 ділиться порівну між двома поступками (по 0.04)",
       all(near(x["delta_p"], 0.04) for x in concs(res, "R2")))
expect("R3 (SHOULD): DROPPED_REQUIREMENT, не блокує gate",
       item(res, "R3")["status"] == "DROPPED" and concs(res, "R3")[0]["kind"] == "DROPPED_REQUIREMENT"
       and not concs(res, "R3")[0]["needs_user_decision"])
c4 = concs(res, "R4")[0]
expect("R4: DELAYED_HORIZON 80→85 без впливу на вимогу (effect NONE, рішення не потрібне)",
       c4["kind"] == "DELAYED_HORIZON" and c4["effect_on_requirement"] == "NONE"
       and not c4["needs_user_decision"] and item(res, "R4")["status"] == "PRESERVED")
expect("Alignment Score = (2·0 + 1·1)/3 = 0.333333 (MUST у бал не входять)", near(res.alignment_score, 1 / 3))
expect("must_summary: 0 збережено, 2 послаблено", res.must_summary == {"total": 2, "preserved": 0, "weakened": 2, "dropped": 0})
total = sum(x["delta_p"] for x in res.concessions)
expect("сума ΔP поступок = P_end − P_start = 0.26",
       near(total, res.p_trajectory["end"] - res.p_trajectory["start"], 1e-6), f"{total:.3f}")
expect("node_diff: 3 додано, 4 видалено; n-goal не «змінився» (requires поза хешем)",
       res.node_diff == {"added": ["n-api2", "n-enc2", "n-plan2"],
                         "removed": ["n-api", "n-enc", "n-mreg", "n-plan"], "changed": []})
expect("усі 4 вимоги зачеплені → у черзі судді", res.judge_queue == ["R1", "R2", "R3", "R4"])
rec_f = [r for r in CHAIN if r["type"] == "FIDELITY"][0]["payload"]
expect("payload збігається із записаним у Ledger (відтворюваність)", res.fidelity_payload == rec_f)
rec_c = [r["payload"] for r in CHAIN if r["type"] == "CONCESSION"]
expect("payload-и поступок збігаються із записаними", res.concessions == rec_c)
text = fid.render_tradeoffs(res, REQS)
expect("CLI-блок містить Alignment Score, Gate і рядок про 12000 → 1000",
       "Alignment Score: 0.33" in text and "Gate: FAIL" in text and "12000 → 1000" in text)

# ------------------------------------------------------------------ T2
print("\nT2. Згода користувача (consent)")
need = {c["concession_id"] for c in res.concessions if c["needs_user_decision"]}
r_all = fid.fidelity(REQS, G0, GN, MUTS, approved=need)
expect("затверджено всі поступки MUST → PASS_WITH_CONSENT",
       r_all.gate["result"] == "PASS_WITH_CONSENT" and {x for x, _ in rules(r_all)} == {"CONSENT_USED"})
only_r1 = {c["concession_id"] for c in concs(res, "R1")}
r_part = fid.fidelity(REQS, G0, GN, MUTS, approved=only_r1)
expect("затверджено лише R1 → FAIL через R2", r_part.gate["result"] == "FAIL"
       and ("MUST_WEAKENED", "R2") in rules(r_part) and ("CONSENT_USED", "R1") in rules(r_part))
expect("невідомий id у approved ігнорується", fid.fidelity(REQS, G0, GN, MUTS, approved={"conc-0000000000000000"})
       .fidelity_payload["approved_concession_ids"] == [])

# ------------------------------------------------------------------ T3
print("\nT3. Нульова зміна та нешкідливий патч")
r0 = fid.fidelity(REQS, G0, G0, [])
expect("граф без змін → PASS, бал 1.0, поступок і черги судді немає",
       r0.gate["result"] == "PASS" and r0.alignment_score == 1.0 and not r0.concessions and not r0.judge_queue)
benign = variant(G0, replace={"n-plan": GN["n-plan2"]})
rb = fid.fidelity(REQS, G0, benign, [])
expect("патч 80→85 днів (ліміт 90) → PASS; поступка лише як інформація",
       rb.gate["result"] == "PASS" and [c["effect_on_requirement"] for c in rb.concessions] == ["NONE"]
       and rb.judge_queue == ["R4"])

# ------------------------------------------------------------------ T4
print("\nT4. Суддя: питання лише для зачеплених вимог; лише погіршує; цитата обов'язкова")
rephr = fid.seal({**G0["n-api"], "statement": "API-шар легко масштабується (переформульовано)"})
gj = variant(G0, replace={"n-api": rephr})
rj = fid.fidelity(REQS, G0, gj, [])
expect("метрики ті самі, текст інший → R1 PRESERVED, але gate = NEEDS_REVIEW (JUDGE_PENDING)",
       item(rj, "R1")["status"] == "PRESERVED" and rj.gate["result"] == "NEEDS_REVIEW"
       and rules(rj) == [("JUDGE_PENDING", "R1")] and rj.judge_queue == ["R1"])
JV = {"R1": {"status": "WEAKENED", "quote": "«легко» замість 12 000 rps"}}
rj2 = fid.fidelity(REQS, G0, gj, [], judge_verdicts=JV)
jc = concs(rj2, "R1")[0]
expect("суддя: WEAKENED з цитатою → JUDGE_FINDING і FAIL",
       jc["kind"] == "JUDGE_FINDING" and rj2.gate["result"] == "FAIL" and item(rj2, "R1")["coverage"] <= 0.5)
rj3 = fid.fidelity(REQS, G0, gj, [], judge_verdicts=JV, approved={jc["concession_id"]})
expect("знахідку судді користувач може затвердити → PASS_WITH_CONSENT", rj3.gate["result"] == "PASS_WITH_CONSENT")
JV2 = {"R1": {"status": "WEAKENED", "quote": "інше формулювання знахідки"}}
rj3b = fid.fidelity(REQS, G0, gj, [], judge_verdicts=JV2, approved={jc["concession_id"]})
expect("згода прив'язана до конкретної знахідки: інша цитата = інша поступка → знову FAIL",
       rj3b.gate["result"] == "FAIL")
rj4 = fid.fidelity(REQS, G0, gj, [], judge_verdicts={"R1": {"status": "PRESERVED", "quote": ""}})
expect("суддя: PRESERVED → PASS, черга порожня", rj4.gate["result"] == "PASS" and not rj4.judge_queue)
try:
    fid.fidelity(REQS, G0, gj, [], judge_verdicts={"R1": {"status": "DROPPED"}})
    expect("вердикт судді без цитати відхиляється", False)
except ValueError:
    expect("вердикт судді без цитати відхиляється (ValueError)", True)
ru = fid.fidelity(REQS, G0, GN, MUTS, judge_verdicts={"R1": {"status": "PRESERVED", "quote": "виглядає нормально"}})
expect("суддя НЕ може покращити детермінований WEAKENED (applied=False), gate лишається FAIL",
       item(ru, "R1")["status"] == "WEAKENED" and item(ru, "R1")["judge"]["applied"] is False
       and ru.gate["result"] == "FAIL")

# ------------------------------------------------------------------ T5
print("\nT5. Прогалини, непідтверджені вимоги, поріг Alignment")
low = fid.seal({**G0["n-api"], "quantities": [{"metric": "throughput", "comparator": ">=", "value": 8000, "unit": "rps"}]})
g_gap = variant(G0, replace={"n-api": low})
rg = fid.fidelity(REQS, g_gap, g_gap, [])
expect("MUST не виконувався вже на старті → FAIL MUST_UNMET, поступок немає",
       rg.gate["result"] == "FAIL" and rules(rg) == [("MUST_UNMET", "R1")] and not rg.concessions)
rg2 = fid.fidelity(REQS, g_gap, g_gap, [], approved={"conc-0000000000000000"})
expect("згода на поступки не лікує прогалину плану", rg2.gate["result"] == "FAIL")
reqs_u = [dict({k: v for k, v in r.items() if k != "source_quote"}, source="INFERRED",
               confirmed_by_user=False) if r["id"] == "R2" else r for r in REQS]
ru2 = fid.fidelity(reqs_u, G0, G0, [])
expect("непідтверджена MUST-інтерпретація → NEEDS_REVIEW (UNCONFIRMED_MUST)",
       ru2.gate["result"] == "NEEDS_REVIEW" and rules(ru2) == [("UNCONFIRMED_MUST", "R2")])
g_no3 = variant(G0, drop=("n-mreg",))
rs = fid.fidelity(REQS, G0, g_no3, [])
expect("відкинуто лише SHOULD: gate PASS, бал 1/3", rs.gate["result"] == "PASS" and near(rs.alignment_score, 1 / 3))
rs2 = fid.fidelity(REQS, G0, g_no3, [], min_alignment=0.5)
expect("min_alignment=0.5 → NEEDS_REVIEW (LOW_ALIGNMENT)",
       rs2.gate["result"] == "NEEDS_REVIEW" and rules(rs2) == [("LOW_ALIGNMENT", None)])

# ------------------------------------------------------------------ T6
print("\nT6. Хеш вузла, цілісність, детермінізм")
n = G0["n-enc"]
same = [
    {**n, "scope": ["in_transit", "at_rest"]},
    {**n, "statement": "  Дані   шифруються у спокої та під час передавання "},
    {**n, "q": 0.1, "n_eff": 3, "requires": ["x"]},
    {**G0["n-plan"], "horizon_days": 80.0},
]
expect("хеш не залежить від порядку тегів, пробілів, q/n_eff/requires, 80 vs 80.0",
       fid.node_hash(same[0]) == fid.node_hash(same[1]) == fid.node_hash(same[2]) == n["node_hash"]
       and fid.node_hash(same[3]) == G0["n-plan"]["node_hash"])
diff = [{**n, "statement": n["statement"] + "!"}, {**n, "scope": ["at_rest"]},
        {**n, "serves": ["R2", "R9"]}, {**n, "conditions": ["x"]},
        {**G0["n-api"], "quantities": [{"metric": "throughput", "comparator": ">=", "value": 11999, "unit": "rps"}]}]
expect("хеш змінюється при зміні змісту (текст, обсяг, serves, умови, поріг)",
       all(fid.node_hash(x) != x.get("node_hash", "") for x in diff)
       and len({fid.node_hash(x) for x in diff}) == len(diff))
bad = variant(G0)
bad["n-enc"] = {**bad["n-enc"], "statement": "підмінено"}
try:
    fid.fidelity(REQS, bad, GN, MUTS)
    expect("підміна вузла без перерахунку hash відхиляється", False)
except ValueError:
    expect("підміна вузла без перерахунку hash відхиляється (ValueError)", True)
shuffled = fid.fidelity(list(reversed(REQS)), dict(reversed(list(G0.items()))),
                        dict(reversed(list(GN.items()))), list(reversed(MUTS)))
expect("результат не залежить від порядку вимог, вузлів і мутацій", shuffled.fidelity_payload == res.fidelity_payload)

# ------------------------------------------------------------------ T7
print("\nT7. Схеми відхиляють неприпустиме")
rec = copy.deepcopy([r for r in CHAIN if r["type"] == "FIDELITY"][0])
rec["payload"]["gate"]["result"] = "PASS"
expect("FIDELITY: PASS при послабленій MUST-вимозі відхиляється",
       any("weakened" in e for e in check(rec, ref("LedgerRecord"))))
rec = copy.deepcopy([r for r in CHAIN if r["type"] == "FIDELITY"][0])
rec["payload"]["gate"]["reasons"] = []
expect("FIDELITY: FAIL без причин відхиляється", bool(check(rec, ref("LedgerRecord"))))
bad_req = {k: v for k, v in REQS[1].items() if k != "source_quote"}
expect("Requirement: USER_STATED без source_quote відхиляється", bool(check(bad_req, ref("Requirement"))))
bad_node = copy.deepcopy(G0["n-api"])
bad_node["quantities"][0]["comparator"] = "≈"
expect("Node: невідомий компаратор відхиляється", bool(check(bad_node, ref("Node"))))
rec = copy.deepcopy([r for r in CHAIN if r["type"] == "CONCESSION"][0])
rec["payload"]["concession_id"] = "x1"
expect("CONCESSION: некоректний concession_id відхиляється", bool(check(rec, ref("LedgerRecord"))))

print("\n=== CLI-БЛОК КОНТРАКТУ (основний сценарій) ===")
print(text)
print(f"\nПідсумок: {'усе пройдено' if not FAILS else 'ПРОВАЛИ: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
