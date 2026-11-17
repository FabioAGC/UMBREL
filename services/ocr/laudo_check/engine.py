from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .jev import JevError, NoulBackend
from .kb import Doc, KnowledgeBase, Passage
from .ocr import Page

PASS, FAIL, REVIEW = "conforme", "irregular", "revisao_humana"

# Mudar o texto de uma pergunta muda o comportamento: suba QUESTION_VERSION (entra na auditoria).
QUESTION_VERSION = "2"
Q_IS_REQUIREMENT = "REQUISITO? O trecho a seguir de um guideline estabelece uma exigência, limite ou critério verificável sobre um laudo?"
Q_COMPLIES = "CONFORME? O laudo atende ao seguinte requisito do guideline?"
Q_VIOLATES = "VIOLA? O laudo descumpre ou deixa de demonstrar o seguinte requisito do guideline?"


@dataclass
class Config:
    approve: float = 0.80          # P(atende) >= approve -> conforme
    reject: float = 0.30           # P(atende) <= reject  -> irregular; no meio -> revisão humana
    is_requirement: float = 0.60   # corte da pergunta "é requisito?" (só sem curadoria)
    double_check: bool = True      # pergunta também a forma negada e cruza as respostas
    max_disagreement: float = 0.35  # |P(atende) - (1 - P(viola))| acima disso -> revisão humana
    min_ocr_conf: float = 60.0     # confiança média do Tesseract abaixo disso -> revisão humana
    max_laudo_chars: int = 60_000  # acima disso não arrisca truncar: revisão humana
    workers: int = 8

    def __post_init__(self):
        if not 0 <= self.reject < self.approve <= 1:
            raise ValueError("limiares exigem 0 <= reject < approve <= 1")

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if not path or not Path(path).exists():
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Chaves desconhecidas em {path}: {sorted(unknown)}")
        return cls(**data)


@dataclass
class Requirement:
    ref: str
    text: str
    severity: str = "major"
    approve: float | None = None  # sobrescreve Config.approve para este requisito
    reject: float | None = None
    source: str = "auto"          # auto (pergunta ao Jev) | curado (YAML revisado por pessoa)


@dataclass
class RuleResult:
    req: Requirement
    p_yes: float | None
    p_violates: float | None
    status: str
    reason: str = ""

    @property
    def p(self) -> float | None:
        """Probabilidade combinada de atender (média das duas polaridades quando há verificação dupla)."""
        if self.p_yes is None:
            return None
        return self.p_yes if self.p_violates is None else (self.p_yes + 1 - self.p_violates) / 2


@dataclass
class Report:
    guidelines: list[str]
    material_candidates: list[tuple[str, float]]
    results: list[RuleResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # qualquer aviso impede APROVADO
    meta: dict = field(default_factory=dict)

    @property
    def issues(self) -> list[RuleResult]:
        return [r for r in self.results if r.status != PASS]

    @property
    def verdict(self) -> str:
        # Evidência ruim (OCR fraco, laudo truncável, Jev fora) invalida também a reprovação automática.
        if self.warnings or not self.results:
            return "REVISAO_HUMANA"
        if any(r.status == FAIL for r in self.results):
            return "REPROVADO"
        if any(r.status == REVIEW for r in self.results):
            return "REVISAO_HUMANA"
        return "APROVADO"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "guidelines": self.guidelines,
            "warnings": self.warnings,
            "material_candidates": [{"doc": d, "score": round(s, 2)} for d, s in self.material_candidates],
            "results": [{
                "ref": r.req.ref, "requirement": r.req.text, "severity": r.req.severity,
                "source": r.req.source, "status": r.status, "reason": r.reason,
                "p": _r(r.p), "p_yes": _r(r.p_yes), "p_violates": _r(r.p_violates),
            } for r in self.results],
            "meta": self.meta,
        }


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


class AmbiguousMaterial(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("Não foi possível decidir o material: " +
                         ", ".join(f"{d} ({s:.1f})" for d, s in candidates))


class StaleCuration(Exception):
    pass


def pick_guideline(kb: KnowledgeBase, laudo: str, material: str | None = None,
                   margin: float = 0.15) -> tuple[Doc, list[tuple[str, float]]]:
    ranked = kb.rank_docs(laudo)
    cands = [(d.name, s) for d, s in ranked]
    if material:
        hits = [d for d, _ in ranked if material.lower() in d.name.lower()]
        if len(hits) != 1:
            raise ValueError(f"--material '{material}' casou com {len(hits)} documentos: {[d.name for d in hits]}")
        return hits[0], cands
    if not ranked or ranked[0][1] <= 0:
        raise AmbiguousMaterial(cands)
    if len(ranked) > 1 and ranked[1][1] >= ranked[0][1] * (1 - margin):
        raise AmbiguousMaterial(cands[:3])
    return ranked[0][0], cands


# --- requisitos: curados (YAML) ou extraídos automaticamente ---

def auto_requirements(doc: Doc, backend: NoulBackend, cfg: Config) -> list[tuple[Passage, float]]:
    """P("é requisito?") por passagem. Não depende do laudo: com CachedBackend sai de graça
    a partir do 2º laudo do mesmo material."""
    with ThreadPoolExecutor(cfg.workers) as ex:
        ps = list(ex.map(lambda p: backend.ask_noul_batch(p.text, [Q_IS_REQUIREMENT])[0], doc.passages))
    return list(zip(doc.passages, ps))


def load_curated(kb: KnowledgeBase, doc: Doc) -> list[Requirement] | None:
    path = kb.curation_path(doc)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("sha256") != doc.sha256:
        raise StaleCuration(f"{path.name} foi revisado para outra versão de {doc.name}. "
                            f"Regere com 'laudo-check requisitos' e revise de novo.")
    return [Requirement(ref=r["ref"], text=r["texto"], severity=r.get("severidade", "major"),
                        approve=r.get("approve"), reject=r.get("reject"), source="curado")
            for r in data["requisitos"] if r.get("ativo", True)]


def export_curation(kb: KnowledgeBase, doc: Doc, backend: NoulBackend, cfg: Config) -> Path:
    """Gera o YAML para um engenheiro de qualidade revisar: liga/desliga, reescreve e dá severidade."""
    items = [{"ref": p.ref, "texto": p.text, "p_requisito": round(pr, 3),
              "ativo": pr >= cfg.is_requirement, "severidade": "major"}
             for p, pr in auto_requirements(doc, backend, cfg)]
    path = kb.curation_path(doc)
    path.write_text(
        "# Revise: 'ativo' liga/desliga, 'texto' pode ser reescrito para ficar inequívoco,\n"
        "# 'severidade' (blocker|major|minor) e 'approve'/'reject' opcionais por requisito.\n"
        "# O sha256 amarra esta revisão à versão exata do documento.\n"
        + yaml.safe_dump({"documento": doc.name, "sha256": doc.sha256, "requisitos": items},
                         allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8")
    return path


def requirements_for(kb: KnowledgeBase, doc: Doc, backend: NoulBackend, cfg: Config) -> list[Requirement]:
    curated = load_curated(kb, doc)
    if curated is not None:
        return curated
    return [Requirement(p.ref, p.text) for p, pr in auto_requirements(doc, backend, cfg)
            if pr >= cfg.is_requirement]


# --- avaliação ---

def classify(p: float, req: Requirement, cfg: Config) -> str:
    approve = req.approve if req.approve is not None else cfg.approve
    reject = req.reject if req.reject is not None else cfg.reject
    return PASS if p >= approve else FAIL if p <= reject else REVIEW


def evaluate(laudo: str | list[Page], kb: KnowledgeBase, backend: NoulBackend, material: str | None = None,
             cfg: Config | None = None) -> Report:
    cfg = cfg or Config()
    pages = [Page(laudo, None)] if isinstance(laudo, str) else laudo
    text = "\n\n".join(p.text for p in pages)

    guideline, cands = pick_guideline(kb, text, material)
    docs = [guideline] + kb.general_docs()
    report = Report([d.name for d in docs], cands[:3], meta={
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "laudo_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "documentos": {d.name: d.sha256 for d in docs},
        "modelo": backend.model, "question_version": QUESTION_VERSION, "config": asdict(cfg),
    })

    low = [i for i, p in enumerate(pages, 1) if p.ocr_conf is not None and p.ocr_conf < cfg.min_ocr_conf]
    if low:
        report.warnings.append(f"OCR com baixa confiança nas páginas {low} do laudo (< {cfg.min_ocr_conf:.0f}).")
    doubts = [f"p.{i} {d}" for i, p in enumerate(pages, 1) for d in p.doubts]
    if doubts:
        report.warnings.append("OCR com leituras divergentes em valores numéricos: " + "; ".join(doubts))
    if len(text) > cfg.max_laudo_chars:
        report.warnings.append(f"Laudo com {len(text)} caracteres excede max_laudo_chars={cfg.max_laudo_chars}.")
    if not text.strip():
        report.warnings.append("Laudo sem texto extraído.")

    reqs = [r for d in docs for r in requirements_for(kb, d, backend, cfg)]
    if not reqs:
        report.warnings.append("Nenhum requisito identificado nos guidelines.")
        return report

    # Todas as perguntas compartilham o mesmo state (o laudo): vão em lote.
    qs = [f"{Q_COMPLIES}\n«{r.text}»" for r in reqs]
    if cfg.double_check:
        qs += [f"{Q_VIOLATES}\n«{r.text}»" for r in reqs]
    try:
        ps = backend.ask_noul_batch(text, qs)
    except JevError as e:
        report.warnings.append(f"Falha no Jev: {e}")
        report.results = [RuleResult(r, None, None, REVIEW, "sem resposta do Jev") for r in reqs]
        return report

    n = len(reqs)
    for i, r in enumerate(reqs):
        res = RuleResult(r, ps[i], ps[n + i] if cfg.double_check else None, REVIEW)
        if res.p_violates is not None and abs(res.p_yes - (1 - res.p_violates)) > cfg.max_disagreement:
            res.reason = "respostas inconsistentes entre 'atende?' e 'viola?'"
        else:
            res.status = classify(res.p, r, cfg)
        report.results.append(res)
    report.meta["modelo"] = backend.model  # relido: no modo auto o backend pode ter caído para verbal
    return report


def render(report: Report) -> str:
    icon = {PASS: "OK  ", FAIL: "ERRO", REVIEW: "REV "}
    lines = [f"Veredito: {report.verdict}", "Guidelines usados: " + ", ".join(report.guidelines)]
    lines += [f"AVISO: {w}" for w in report.warnings]
    lines.append("")
    for r in report.results:
        p = "  -  " if r.p is None else f"{r.p:.2f}"
        lines.append(f"[{icon[r.status]}] {r.req.ref}  P(atende)={p}  ({r.req.severity}, {r.req.source})")
    if report.issues:
        lines += ["", "Irregularidades / pontos a revisar:"]
        for r in report.issues:
            kind = "NÃO CONFORME" if r.status == FAIL else "REVISAR"
            why = f" [{r.reason}]" if r.reason else ""
            lines.append(f" - [{kind}] {r.req.ref}: {r.req.text}{why}")
    elif report.verdict == "APROVADO":
        lines += ["", "Nenhuma irregularidade: pode prosseguir."]
    return "\n".join(lines)
