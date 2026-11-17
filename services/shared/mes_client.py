"""Cliente da API REST do AVEVA MES, compartilhado pela tela de 7 dias (mes-api) e pela ETL.

Autenticação igual à do MCP-MES: OAuth2 grant_type=password no Identity Manager,
com o client id/secret no header Basic. O token fica em cache até perto de expirar.
Tudo vem de variáveis de ambiente; nada de credencial em arquivo.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, timezone

import logging

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)  # não logar cada URL de consulta


class MesError(RuntimeError):
    pass


class MesClient:
    def __init__(self) -> None:
        env = os.environ
        self.base = env["MES_BASE_URL"].rstrip("/")
        self.token_url = env["MES_TOKEN_URL"]
        self.username = env["MES_USERNAME"]
        self.password = env["MES_PASSWORD"]
        self.scope = env.get("MES_SCOPE", "system")
        client_id = env.get("MES_CLIENT_ID", "AIM_DefaultManagementClient")
        client_secret = env.get("MES_CLIENT_SECRET", "")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

        headers = {"Accept": "application/json"}
        if env.get("MES_HOST"):
            headers["Host"] = env["MES_HOST"]
        verify: bool | str = env.get("MES_CA_BUNDLE") or env.get("MES_VERIFY_TLS", "true").lower() == "true"
        self._http = httpx.Client(verify=verify, timeout=float(env.get("MES_TIMEOUT", "120")), headers=headers)

        self._lock = threading.Lock()
        self._token: str | None = None
        self._expira = 0.0

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expira:
                return self._token
            r = self._http.post(
                self.token_url,
                data={"grant_type": "password", "scope": self.scope,
                      "username": self.username, "password": self.password},
                headers={"Authorization": f"Basic {self._basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code != 200:
                raise MesError(f"Token do MES recusado ({r.status_code}): {r.text[:300]}")
            data = r.json()
            self._token = data["access_token"]
            self._expira = time.time() + int(data.get("expires_in", 3300)) - 60
            return self._token

    def get(self, path: str, params: dict | None = None):
        url = f"{self.base}/{path.lstrip('/')}"
        for tentativa in (1, 2):
            r = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._get_token(tentativa == 2)}"})
            if r.status_code == 401 and tentativa == 1:
                continue  # token expirou do lado do servidor: renova uma vez
            if r.status_code >= 400:
                raise MesError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise MesError(f"GET {path}: não autorizado mesmo com token novo")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _preencher(obj, inicio: str, fim: str):
    if isinstance(obj, str):
        return obj.replace("{inicio}", inicio).replace("{fim}", fim)
    if isinstance(obj, dict):
        return {k: _preencher(v, inicio, fim) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_preencher(v, inicio, fim) for v in obj]
    return obj


def _linhas(resposta) -> list[dict]:
    """Normaliza as formas comuns de resposta (lista, {'value': [...]}, {'data': [...]})."""
    if isinstance(resposta, list):
        return resposta
    if isinstance(resposta, dict):
        for chave in ("value", "data", "Data", "rows", "Rows", "result", "Result"):
            if isinstance(resposta.get(chave), list):
                return resposta[chave]
    raise MesError(f"Formato de resposta inesperado: {str(resposta)[:300]}")


# =============================================================================
# >>> AJUSTE AQUI <<<
# Esta é a ÚNICA função que sabe como buscar os registros no MES.
# A tela de 7 dias e a ETL usam ela, então os dois sempre leem a mesma coisa.
# Padrão: GET em MES_REGISTROS_PATH com MES_REGISTROS_PARAMS ({inicio}/{fim}).
# Se a sua consulta atual for outra (outro endpoint, paginação, SQL), troque
# o corpo desta função mantendo a assinatura: devolve lista de dicts.
# =============================================================================
def buscar_registros(cliente: MesClient, inicio: datetime, fim: datetime) -> list[dict]:
    path = os.environ.get("MES_REGISTROS_PATH")
    if not path:
        raise MesError("Defina MES_REGISTROS_PATH (e MES_REGISTROS_PARAMS) com a consulta real do MES.")
    params = json.loads(os.environ.get("MES_REGISTROS_PARAMS") or "{}")
    params = _preencher(params, _iso(inicio), _iso(fim))
    return _linhas(cliente.get(path, params))
