"""laudo-check servido no mini PC: a API original (/validar, /kb/reload) + gestão da base.

As guidelines (PDFs internos) NÃO vão para o git nem para a imagem: sobem pela tela
/admin/ (rotas /kb/*, protegidas por login no proxy) e ficam no volume /data/kb.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi import File, HTTPException, UploadFile

from laudo_check.api import KB_PATH, app, reload_kb
from laudo_check.kb import CACHE_DIRNAME, GENERAL_DIR

PERMITIDOS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt", ".yaml", ".yml"}


def _seguro(nome: str) -> str:
    """Só o nome do arquivo, sem caminho; mantém a subpasta 'geral/' se vier explícita."""
    geral = nome.startswith(f"{GENERAL_DIR}/")
    base = re.sub(r"[^\w.\- ]", "_", Path(nome).name).strip()
    if not base or base.startswith("."):
        raise HTTPException(422, f"Nome inválido: {nome!r}")
    if Path(base).suffix.lower() not in PERMITIDOS:
        raise HTTPException(422, f"Extensão não permitida: {base}")
    return f"{GENERAL_DIR}/{base}" if geral else base


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/kb/files")
def listar():
    arquivos = []
    for p in sorted(KB_PATH.rglob("*")):
        rel = p.relative_to(KB_PATH)
        if p.is_file() and CACHE_DIRNAME not in rel.parts:
            arquivos.append({"nome": str(rel), "bytes": p.stat().st_size})
    return arquivos


@app.post("/kb/upload")
def enviar(arquivos: list[UploadFile] = File(...), geral: bool = False):
    salvos = []
    for a in arquivos:
        nome = _seguro(f"{GENERAL_DIR}/{a.filename}" if geral else (a.filename or ""))
        destino = KB_PATH / nome
        destino.parent.mkdir(parents=True, exist_ok=True)
        with destino.open("wb") as f:
            shutil.copyfileobj(a.file, f)
        salvos.append(nome)
    return {"salvos": salvos, **reload_kb()}


@app.delete("/kb/files/{nome:path}")
def apagar(nome: str):
    nome = _seguro(nome)
    if nome == "config.yaml":
        raise HTTPException(409, "config.yaml tem os limiares calibrados: substitua por upload, não apague")
    alvo = (KB_PATH / nome).resolve()
    if KB_PATH.resolve() not in alvo.parents or not alvo.is_file():
        raise HTTPException(404, "Arquivo não encontrado")
    alvo.unlink()
    return reload_kb()
