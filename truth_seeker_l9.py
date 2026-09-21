"""
Шукач істини: референсний код
  V3-V5 (L7): оновлення ймовірності атаки p_a з кластерами доказів -> вердикт
  L9        : P(успіх) по замиканню PRESUPPOSES + вердикти + Monte Carlo інтервал
Залежності: лише стандартна бібліотека (Python 3.9+).

Спрощення (свідомі):
  * лише noisy-AND по PRESUPPOSES (без OR-альтернатив і SUPPORTS);
  * вердикти фіксуються за "mid"-значеннями LR, а Monte Carlo семплює
    невизначеність лише для UNRESOLVED-атак і невизначеність q вузлів;
  * атаки поза замиканням цілі не впливають на P (їх місце: реєстр ризиків).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

# --------------------------------------------------------------------------
# 1. Докази -> p_a
# --------------------------------------------------------------------------

# Стартові межі LR за рівнем заземлення (калібрувати на реальних даних!)
LR_CAP = {"T0": 100.0, "T1": 20.0, "T1p": 5.0, "T2": 10.0, "T3": 8.0, "T4": 2.0}
CONFIRM_AT, REFUTE_AT = 0.95, 0.05


def lr_from_test(sens: float, spec: float, positive: bool) -> float:
    """LR тесту з виміряних чутливості/специфічності."""
    return sens / (1 - spec) if positive else (1 - sens) / spec


def odds(p: float) -> float:
    return p / (1 - p)


def prob(o: float) -> float:
    return o / (1 + o)


def cap(lr: float, tier: str) -> float:
    c = LR_CAP[tier]
    return min(max(lr, 1 / c), c)


@dataclass
class Evidence:
    cluster: str            # незалежність: одне походження = один кластер
    tier: str               # T0 | T1 | T1p | T2 | T3 | T4
    lr: float               # LR на користь атаки (>1) або проти (<1)
    lr_low: Optional[float] = None
    lr_high: Optional[float] = None

    def bounds(self):
        return (self.lr_low if self.lr_low is not None else self.lr,
                self.lr,
                self.lr_high if self.lr_high is not None else self.lr)


@dataclass
class Attack:
    id: str
    target: str
    prior: float
    damage: float = 1.0     # частка шкоди для цілі, якщо атака справжня
    evidence: list[Evidence] = field(default_factory=list)


def posterior(attack: Attack, use_clusters: bool = True,
              sampler: Optional[Callable[[Evidence], float]] = None) -> float:
    """odds(a|E) = odds(a) * prod(LR по кластерах). У кластері беремо
    найсильніший доказ (max |ln LR|), а не добуток."""
    sampler = sampler or (lambda e: e.lr)
    items = [(e.cluster, math.log(cap(sampler(e), e.tier)))
             for e in attack.evidence]
    if use_clusters:
        best: dict[str, float] = {}
        for c, l in items:
            if c not in best or abs(l) > abs(best[c]):
                best[c] = l
        total = sum(best.values())
    else:
        total = sum(l for _, l in items)
    p0 = min(max(attack.prior, 1e-6), 1 - 1e-6)
    return prob(odds(p0) * math.exp(total))


def verdict(attack: Attack, use_clusters: bool = True) -> tuple[str, float]:
    p = posterior(attack, use_clusters)
    # Правило T4: лише LLM-докази не можуть закрити чи підтвердити атаку
    grounded = any(e.tier != "T4" for e in attack.evidence)
    if grounded and p >= CONFIRM_AT:
        return "CONFIRMED", p
    if grounded and p <= REFUTE_AT:
        return "REFUTED", p
    return "UNRESOLVED", p


# --------------------------------------------------------------------------
# 2. L9: P(успіх)
# --------------------------------------------------------------------------

@dataclass
class Node:
    q: float                                   # P(вузол | усі передумови істинні)
    requires: list[str] = field(default_factory=list)
    n_eff: float = 10.0                        # "ефективний обсяг вибірки" для Beta


def closure(nodes: dict[str, Node], goal: str) -> set[str]:
    seen, stack = set(), [goal]
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(nodes[n].requires)
    return seen


def p_success(nodes, goal, attacks, verdicts, q_override=None, p_override=None):
    req = closure(nodes, goal)
    P = 1.0
    for n in req:                              # кожен необхідний вузол рівно раз
        P *= (q_override or {}).get(n, nodes[n].q)
    for a in attacks:
        if a.target not in req:
            continue                           # поза замиканням -> реєстр ризиків
        v, p = verdicts[a.id]
        if v == "CONFIRMED":
            return 0.0                         # -> тригер L11
        if v == "UNRESOLVED":
            p = (p_override or {}).get(a.id, p)
            P *= 1 - p * a.damage
        # REFUTED: атака знімається, множника немає
    return P


def monte_carlo(nodes, goal, attacks, verdicts, n=20000, seed=7):
    rnd = random.Random(seed)
    req = closure(nodes, goal)

    def sampler(e: Evidence) -> float:         # log-трикутний розподіл LR
        lo, mid, hi = e.bounds()
        return math.exp(rnd.triangular(math.log(lo), math.log(hi), math.log(mid)))

    out = []
    for _ in range(n):
        q = {}
        for i in req:
            m = min(max(nodes[i].q, 0.001), 0.999)
            k = nodes[i].n_eff
            q[i] = rnd.betavariate(m * k, (1 - m) * k)
        pa = {a.id: posterior(a, sampler=sampler)
              for a in attacks if verdicts[a.id][0] == "UNRESOLVED"}
        out.append(p_success(nodes, goal, attacks, verdicts, q, pa))
    out.sort()
    return out[n // 2], out[int(0.1 * n)], out[int(0.9 * n)]


def sensitivities(nodes, goal, attacks, verdicts):
    """Де найбільший важіль: для вузлів dP/dq = P/q, для атак виграш P,
    якби атаку було знято."""
    P = p_success(nodes, goal, attacks, verdicts)
    req = closure(nodes, goal)
    rows = [(f"вузол {i}: +0.05 до q", 0.05 * P / nodes[i].q) for i in req]
    for a in attacks:
        v, p = verdicts[a.id]
        if a.target in req and v == "UNRESOLVED":
            rows.append((f"атака {a.id}: якби знято", P / (1 - p * a.damage) - P))
    return sorted(rows, key=lambda r: -r[1])


# --------------------------------------------------------------------------
# 3. Демо: G потребує A і B; A потребує C (приклад із розбору L9)
# --------------------------------------------------------------------------

def run(title, nodes, goal, attacks, use_clusters=True):
    verdicts = {a.id: verdict(a, use_clusters) for a in attacks}
    P = p_success(nodes, goal, attacks, verdicts)
    med, lo, hi = monte_carlo(nodes, goal, attacks, verdicts)
    print(f"\n### {title}")
    for a in attacks:
        v, p = verdicts[a.id]
        print(f"  атака {a.id}->{a.target}: prior={a.prior:.2f}  "
              f"p_a={p:.3f}  вердикт={v}")
    print(f"  P(успіх) точкова = {P:.3f}   MC медіана = {med:.3f}  "
          f"[p10..p90 = {lo:.3f} .. {hi:.3f}]")
    return verdicts


if __name__ == "__main__":
    nodes = {
        "C": Node(0.90),
        "A": Node(0.85, ["C"]),
        "B": Node(0.80),
        "G": Node(0.95, ["A", "B"]),
    }

    llm1 = Evidence("llm", "T4", 0.5, 0.35, 0.7)
    llm2 = Evidence("llm", "T4", 0.5, 0.35, 0.7)     # той самий кластер
    llm3 = Evidence("llm", "T4", 0.5, 0.35, 0.7)

    def X(*ev):
        return Attack("X", "B", prior=0.20, evidence=list(ev))

    run("0. Без атак (коректний добуток по замиканню)", nodes, "G", [])
    run("1. Атака на B, доказів немає", nodes, "G", [X()])
    run("2. + одна LLM-думка (T4)", nodes, "G", [X(llm1)])

    print("\n### 3. Три LLM-думки в одному кластері: кластери vs наївний добуток")
    a3 = X(llm1, llm2, llm3)
    print(f"  p_a з кластерами = {posterior(a3, True):.3f}   "
          f"наївно (вважаємо незалежними) = {posterior(a3, False):.3f}")
    run("3. Три LLM-думки, кластеризовано", nodes, "G", [a3])

    run("4. + первинне джерело суперечить механізму (T1, LR=0.1)", nodes, "G",
        [X(llm1, Evidence("gov_report", "T1", 0.10, 0.05, 0.20))])
    run("5. Сильний доказ ЗА атаку, але без вердикту (T1, LR=20)", nodes, "G",
        [X(Evidence("audit", "T1", 20, 10, 20))])
    run("6. Обчислення підтверджує атаку (T0, LR=100)", nodes, "G",
        [X(Evidence("calc", "T0", 100, 60, 100))])

    print("\n### Чутливості (стан із кроку 2: атака UNRESOLVED)")
    atk = [X(llm1)]
    vd = {a.id: verdict(a) for a in atk}
    for name, gain in sensitivities(nodes, "G", atk, vd):
        print(f"  {name:<28} ΔP = +{gain:.3f}")
