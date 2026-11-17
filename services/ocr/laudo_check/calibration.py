"""Calibração dos limiares a partir de laudos rotulados por quem já decide hoje (qualidade).

Conjunto (JSONL, uma linha por laudo):
  {"laudo": "laudos/123.pdf", "material": "aco_a36", "rotulos": {"aco_a36.pdf p.1#3": "irregular", ...}}

O erro caro é aprovar um requisito descumprido (falso aceite). Por isso `approve` é escolhido pelo
limite SUPERIOR de confiança (Wilson, 95% unilateral) da taxa de falso aceite, e não pela taxa observada:
com poucos exemplos a ferramenta se recusa a afrouxar o limiar.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

from .engine import Config, evaluate
from .jev import NoulBackend
from .kb import KnowledgeBase
from .ocr import extract

Z95 = 1.645  # unilateral


def wilson_upper(errors: int, n: int, z: float = Z95) -> float:
    if n == 0:
        return 1.0
    ph = errors / n
    den = 1 + z * z / n
    centre = ph + z * z / (2 * n)
    margin = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / den)


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def reliability(pairs: list[tuple[float, int]], bins: int = 10) -> tuple[list[dict], float]:
    """Tabela de confiabilidade e ECE: em cada faixa de P, quantos realmente estavam conformes."""
    rows, ece = [], 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not sel:
            continue
        mp = sum(p for p, _ in sel) / len(sel)
        fy = sum(y for _, y in sel) / len(sel)
        ece += len(sel) / len(pairs) * abs(mp - fy)
        rows.append({"faixa": f"{lo:.1f}-{hi:.1f}", "n": len(sel), "p_medio": round(mp, 3),
                     "frac_conforme": round(fy, 3)})
    return rows, ece


@dataclass
class Recommendation:
    approve: float | None  # None = dados insuficientes para a meta de falso aceite
    reject: float | None
    automation: float      # fração decidida sem revisão humana com os limiares sugeridos
    n: int
    n_conforme: int


def recommend(pairs: list[tuple[float, int]], max_false_accept: float = 0.02,
              max_false_reject: float = 0.10) -> Recommendation:
    approve = next((t / 100 for t in range(50, 100)
                    if (sel := [y for p, y in pairs if p >= t / 100])
                    and wilson_upper(sel.count(0), len(sel)) <= max_false_accept), None)
    reject = next((t / 100 for t in range(49, 0, -1)
                   if (sel := [y for p, y in pairs if p <= t / 100])
                   and wilson_upper(sel.count(1), len(sel)) <= max_false_reject), None)
    a = approve if approve is not None else 1.01
    r = reject if reject is not None else -0.01
    if r >= a:  # faixas se cruzariam: não há decisão automática segura
        approve, reject, a, r = None, None, 1.01, -0.01
    auto = sum(1 for p, _ in pairs if p >= a or p <= r) / len(pairs) if pairs else 0.0
    return Recommendation(approve, reject, auto, len(pairs), sum(y for _, y in pairs))


def collect(dataset: str | Path, kb: KnowledgeBase, backend: NoulBackend, cfg: Config,
            lang: str = "por") -> tuple[list[tuple[float, int]], list[str], int]:
    """Roda o pipeline nos laudos rotulados e devolve (P combinada, rótulo 1=conforme)."""
    pairs, problems, inconsistent = [], [], 0
    base = Path(dataset).parent
    for n, line in enumerate(Path(dataset).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        rep = evaluate(extract(base / row["laudo"], lang), kb, backend, row.get("material"), cfg)
        by_ref = {r.req.ref: r for r in rep.results}
        for ref, label in row["rotulos"].items():
            if label not in ("conforme", "irregular"):
                problems.append(f"linha {n}: rótulo inválido '{label}' em {ref}")
            elif ref not in by_ref or by_ref[ref].p is None:
                problems.append(f"linha {n}: requisito {ref} não avaliado (inativo, inexistente ou sem resposta)")
            else:
                r = by_ref[ref]
                inconsistent += r.reason.startswith("respostas inconsistentes")
                pairs.append((r.p, int(label == "conforme")))
    return pairs, problems, inconsistent


def report_text(pairs, problems, inconsistent, rec: Recommendation, cfg: Config) -> str:
    if not pairs:
        return "Nenhum par (P, rótulo) coletado.\n" + "\n".join(problems)
    rows, ece = reliability(pairs)
    out = [f"Pares avaliados: {rec.n} ({rec.n_conforme} conformes, {rec.n - rec.n_conforme} irregulares)",
           f"Brier: {brier(pairs):.4f}   ECE: {ece:.4f}   inconsistentes (atende x viola): {inconsistent}",
           "", "Faixa     n    P médio  frac. conforme"]
    out += [f"{r['faixa']}  {r['n']:>4}  {r['p_medio']:>7.3f}  {r['frac_conforme']:>7.3f}" for r in rows]
    out.append("")
    if rec.approve is None:
        out.append("approve: DADOS INSUFICIENTES para a meta de falso aceite; mantenha o atual e rotule mais laudos.")
    if rec.reject is None:
        out.append("reject: DADOS INSUFICIENTES para a meta de falso rejeite.")
    new = replace(cfg, approve=rec.approve if rec.approve is not None else cfg.approve,
                  reject=rec.reject if rec.reject is not None else cfg.reject)
    if new.reject >= new.approve:
        new = cfg
    out += [f"Automação estimada com os limiares sugeridos: {rec.automation:.0%}", "",
            "Sugestão para kb/config.yaml:", f"approve: {new.approve}", f"reject: {new.reject}"]
    if problems:
        out += ["", "Problemas no conjunto:"] + [f" - {p}" for p in problems]
    return "\n".join(out)
