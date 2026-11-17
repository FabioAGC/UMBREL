from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .calibration import collect, recommend, report_text
from .engine import AmbiguousMaterial, Config, StaleCuration, evaluate, export_curation, render
from .jev import make_backend
from .kb import CACHE_DIRNAME, KnowledgeBase
from .ocr import extract

COMMANDS = {"validar", "requisitos", "calibrar"}


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-k", "--kb", default="kb", help="pasta com os guidelines/documentos da fábrica")
    p.add_argument("-c", "--config", help="YAML de limiares (padrão: <kb>/config.yaml)")
    p.add_argument("--backend", choices=["jev", "groq", "mock"], default="jev")
    p.add_argument("--no-cache", action="store_true", help="não usar o cache de respostas do Jev")
    p.add_argument("--lang", default="por", help="idioma do Tesseract")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "validar")  # compatível com `laudo-check laudo.pdf`

    ap = argparse.ArgumentParser(prog="laudo-check")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validar", help="valida um laudo")
    v.add_argument("laudo", help="PDF, imagem ou .txt do laudo")
    v.add_argument("-m", "--material", help="trecho do nome do guideline (evita a detecção automática)")
    v.add_argument("--json", action="store_true")
    _common(v)

    r = sub.add_parser("requisitos", help="gera <guideline>.requisitos.yaml para revisão humana")
    r.add_argument("documentos", nargs="*", help="trecho do nome; vazio = todos")
    _common(r)

    c = sub.add_parser("calibrar", help="mede o erro em laudos rotulados e sugere limiares")
    c.add_argument("dataset", help="JSONL com laudo, material e rotulos")
    c.add_argument("--max-falso-aceite", type=float, default=0.02)
    c.add_argument("--max-falso-rejeite", type=float, default=0.10)
    _common(c)

    a = ap.parse_args(argv)
    kb = KnowledgeBase(a.kb, a.lang)
    cfg = Config.load(a.config or Path(a.kb) / "config.yaml")
    backend = make_backend(a.backend, None if a.no_cache else Path(a.kb) / CACHE_DIRNAME / "jev.sqlite")

    try:
        if a.cmd == "requisitos":
            for d in kb.docs:
                if not a.documentos or any(s.lower() in d.name.lower() for s in a.documentos):
                    print(f"gerado: {export_curation(kb, d, backend, cfg)}")
            return 0
        if a.cmd == "calibrar":
            pairs, problems, inc = collect(a.dataset, kb, backend, cfg, a.lang)
            rec = recommend(pairs, a.max_falso_aceite, a.max_falso_rejeite)
            print(report_text(pairs, problems, inc, rec, cfg))
            return 0
        report = evaluate(extract(a.laudo, a.lang), kb, backend, a.material, cfg)
    except AmbiguousMaterial as e:
        print(f"{e}\nUse --material para escolher o guideline.", file=sys.stderr)
        return 3
    except StaleCuration as e:
        print(str(e), file=sys.stderr)
        return 4
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if a.json else render(report))
    return {"APROVADO": 0, "REVISAO_HUMANA": 2}.get(report.verdict, 1)


if __name__ == "__main__":
    sys.exit(main())
