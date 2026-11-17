"""Tela de 7 dias do MES: lê os registros dos últimos 7 dias e serve de um cache.

O MES é consultado em segundo plano a cada MES_REFRESH_MIN minutos.
Os navegadores leem o cache, então 1 tela ou 50 telas custam o mesmo para o MES.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from mes_client import MesClient, buscar_registros

log = logging.getLogger("mes-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DIAS = int(os.environ.get("MES_DIAS", "7"))
REFRESH_MIN = float(os.environ.get("MES_REFRESH_MIN", "10"))

app = FastAPI(title="mes-api")
_cache: dict = {"linhas": [], "atualizado_em": None, "erro": None, "inicio": None, "fim": None}
_lock = threading.Lock()


def _atualizar(cliente: MesClient) -> None:
    fim = datetime.now(timezone.utc)
    inicio = fim - timedelta(days=DIAS)
    t0 = time.monotonic()
    linhas = buscar_registros(cliente, inicio, fim)
    with _lock:
        _cache.update(linhas=linhas, atualizado_em=fim.isoformat(), erro=None,
                      inicio=inicio.isoformat(), fim=fim.isoformat())
    log.info("MES: %d registros em %.1fs", len(linhas), time.monotonic() - t0)


def _loop() -> None:
    cliente = None
    while True:
        try:
            cliente = cliente or MesClient()
            _atualizar(cliente)
        except Exception as e:  # o erro vai para a tela; o último dado bom continua servido
            log.exception("Falha ao atualizar do MES")
            with _lock:
                _cache["erro"] = f"{type(e).__name__}: {e}"
        time.sleep(REFRESH_MIN * 60)


@app.on_event("startup")
def _start() -> None:
    threading.Thread(target=_loop, daemon=True, name="mes-refresh").start()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/registros")
def registros():
    with _lock:
        dados = dict(_cache, total=len(_cache["linhas"]), refresh_min=REFRESH_MIN, dias=DIAS)
    return JSONResponse(dados, headers={"Cache-Control": "no-store"})


@app.get("/registros.csv")
def registros_csv():
    with _lock:
        linhas = list(_cache["linhas"])
    colunas = sorted({k for linha in linhas for k in linha})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=colunas, delimiter=";", extrasaction="ignore")
    w.writeheader()
    w.writerows(linhas)
    nome = f"mes_ultimos_{DIAS}_dias.csv"
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename={nome}"})
