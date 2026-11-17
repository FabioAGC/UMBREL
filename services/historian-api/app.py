"""Painéis do Historian servidos de um cache.

Para cada painel de paineis.yaml, uma thread consulta o Historian (REST OData,
NTLM com a conta de serviço) a cada `intervalo_s` e guarda o último valor de cada
tag, mais um histórico curto para o mini-gráfico. Os navegadores leem o cache:
o Historian recebe a mesma carga com 1 ou com 100 telas abertas.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from requests_ntlm import HttpNtlmAuth

log = logging.getLogger("historian-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = os.environ.get("HISTORIAN_BASE_URL", "http://brpuwwhist1:32569/Historian/v2/ProcessValues")
TIMEOUT = float(os.environ.get("HISTORIAN_TIMEOUT", "15"))
CONFIG = Path(os.environ.get("PAINEIS_CONFIG", Path(__file__).with_name("paineis.yaml")))
HIST_REFRESH_S = 60  # o histórico do mini-gráfico é recalculado a cada minuto

app = FastAPI(title="historian-api")
_paineis: dict[str, dict] = {}
_lock = threading.Lock()


def _fmt(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sessao() -> requests.Session:
    dominio = os.environ.get("HISTORIAN_DOMAIN", "GENMILLS")
    usuario = os.environ["HISTORIAN_USER"]
    usuario = usuario if "\\" in usuario else f"{dominio}\\{usuario}"
    s = requests.Session()
    s.auth = HttpNtlmAuth(usuario, os.environ["HISTORIAN_PASSWORD"])
    s.headers.update({"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json"})
    return s


def _consultar(s: requests.Session, tag: dict, ini: datetime, fim: datetime, resolucao_ms: int) -> list[dict]:
    body = {"FQN": [tag["fqn"]], "StartDateTime": _fmt(ini), "EndDateTime": _fmt(fim),
            "Resolution": str(resolucao_ms)}
    if tag.get("expressao"):
        body["Expression"] = tag["expressao"]
    r = s.post(BASE_URL, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("value", [])


def _loop(painel: dict) -> None:
    pid, intervalo = painel["id"], float(painel.get("intervalo_s", 5))
    janela = timedelta(minutes=float(painel.get("janela_min", 60)))
    res_hist = int(painel.get("resolucao_hist_ms", 60_000))
    sessao, ultimo_hist = None, 0.0
    while True:
        t0 = time.monotonic()
        agora = datetime.now(timezone.utc)
        fazer_hist = t0 - ultimo_hist >= HIST_REFRESH_S
        erros = []
        try:
            sessao = sessao or _sessao()
            for tag in painel["tags"]:
                estado = _paineis[pid]["tags"][tag["fqn"]]
                try:
                    vals = _consultar(sessao, tag, agora - timedelta(seconds=intervalo * 3), agora, int(intervalo * 1000))
                    vals = [v for v in vals if v.get("Value") is not None]
                    if vals:
                        u = vals[-1]
                        with _lock:
                            estado.update(valor=u["Value"], ts=u.get("DateTime"), qualidade=u.get("OpcQuality"),
                                          unidade=tag.get("unidade") or u.get("Unit"), erro=None)
                    if fazer_hist:
                        h = _consultar(sessao, tag, agora - janela, agora, res_hist)
                        with _lock:
                            estado["historico"] = [[v.get("DateTime"), v["Value"]] for v in h if v.get("Value") is not None]
                except requests.RequestException as e:
                    erros.append(f"{tag['fqn']}: {e}")
                    with _lock:
                        estado["erro"] = str(e)[:200]
            if fazer_hist:
                ultimo_hist = t0
        except Exception as e:
            erros.append(f"{type(e).__name__}: {e}")
            sessao = None  # recria a sessão NTLM na próxima volta
        with _lock:
            _paineis[pid]["atualizado_em"] = agora.isoformat()
            _paineis[pid]["erro"] = "; ".join(erros)[:500] or None
        if erros:
            log.warning("Painel %s: %s", pid, erros[0])
        time.sleep(max(0.5, intervalo - (time.monotonic() - t0)))


@app.on_event("startup")
def _start() -> None:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for p in cfg["paineis"]:
        _paineis[p["id"]] = {
            "id": p["id"], "titulo": p.get("titulo", p["id"]), "intervalo_s": p.get("intervalo_s", 5),
            "atualizado_em": None, "erro": None,
            "tags": {t["fqn"]: {"fqn": t["fqn"], "nome": t.get("nome", t["fqn"]), "unidade": t.get("unidade"),
                                "min": t.get("min"), "max": t.get("max"), "valor": None, "ts": None,
                                "qualidade": None, "erro": None, "historico": []}
                     for t in p["tags"]},
        }
        threading.Thread(target=_loop, args=(p,), daemon=True, name=f"painel-{p['id']}").start()
    log.info("Painéis carregados: %s", ", ".join(_paineis))


def _snapshot(pid: str) -> dict:
    with _lock:
        p = _paineis[pid]
        return {**p, "tags": [dict(t, historico=list(t["historico"])) for t in p["tags"].values()]}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/paineis")
def listar():
    with _lock:
        return [{"id": p["id"], "titulo": p["titulo"], "intervalo_s": p["intervalo_s"]} for p in _paineis.values()]


@app.get("/paineis/{pid}")
def painel(pid: str):
    if pid not in _paineis:
        raise HTTPException(404, "Painel não encontrado")
    return JSONResponse(_snapshot(pid), headers={"Cache-Control": "no-store"})
