from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .engine import AmbiguousMaterial, Config, StaleCuration, evaluate
from .jev import make_backend
from .kb import CACHE_DIRNAME, KnowledgeBase
from .ocr import extract

app = FastAPI(title="laudo-check")
KB_PATH = Path(os.environ.get("KB_PATH", "kb"))
BACKEND = os.environ.get("LAUDO_BACKEND", "jev")

# Carregados uma vez e reaproveitados: índice, config e cliente HTTP/cache do Jev.
_lock = threading.Lock()
_state: dict = {}


def _get():
    with _lock:
        if not _state:
            _state["kb"] = KnowledgeBase(KB_PATH)
            _state["cfg"] = Config.load(KB_PATH / "config.yaml")
            _state["backend"] = make_backend(BACKEND, KB_PATH / CACHE_DIRNAME / "jev.sqlite")
        return _state["kb"], _state["cfg"], _state["backend"]


@app.post("/kb/reload")
def reload_kb():
    with _lock:
        _state.clear()
    kb, _, _ = _get()
    return {"docs": [d.name for d in kb.docs]}


@app.post("/validar")
def validar(file: UploadFile = File(...), material: str | None = Form(None)):
    # Endpoint síncrono: o FastAPI o roda em threadpool, sem travar o event loop durante OCR/Jev.
    kb, cfg, backend = _get()
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / Path(file.filename or "laudo.pdf").name
            p.write_bytes(file.file.read())
            pages = extract(p)
        return evaluate(pages, kb, backend, material, cfg).to_dict()
    except AmbiguousMaterial as e:
        raise HTTPException(409, {"error": str(e), "candidates": e.candidates})
    except StaleCuration as e:
        raise HTTPException(409, {"error": str(e)})
    except ValueError as e:
        raise HTTPException(422, {"error": str(e)})
