"""Backend Noul sobre um LLM de chat compatível com OpenAI (Groq por padrão).

Um LLM gera texto, não probabilidade. Dois modos para obter P(sim):
- "logprobs": força a resposta em 1 token (SIM/NAO) e lê a probabilidade dos dois tokens.
  É o mais próximo do Noul do Jev e o mais calibrável.
- "verbal": o modelo devolve {"resposta": "SIM"|"NAO", "confianca": 0-100}. Funciona em qualquer
  provedor, mas confiança declarada costuma ser mal calibrada: rode `laudo-check calibrar`.
"auto" tenta logprobs e cai para verbal se o provedor recusar.
"""
from __future__ import annotations

import json
import math
import os
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import httpx

from .jev import JevError, post_with_retry

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"

SYSTEM = ("Você é auditor de qualidade de materiais de embalagem. Avalie SOMENTE com base no DOCUMENTO "
          "fornecido. Se a informação necessária não estiver no documento, a resposta é NAO.")
P_MIN, P_MAX = 0.001, 0.999


def _fold(tok: str) -> str:
    return unicodedata.normalize("NFKD", tok).encode("ascii", "ignore").decode().strip().upper()


def _side(tok: str) -> str | None:
    t = _fold(tok)
    if t in {"SIM", "SI", "S", "YES", "Y"}:
        return "yes"
    if t in {"NAO", "NA", "N", "NO"}:
        return "no"
    return None


def p_from_logprobs(top: list[dict]) -> float:
    """P(sim) normalizada entre as alternativas SIM/NAO presentes no top-k do 1º token."""
    mass = {"yes": 0.0, "no": 0.0}
    for t in top:
        side = _side(t.get("token", ""))
        if side:
            mass[side] += math.exp(t["logprob"])
    total = mass["yes"] + mass["no"]
    if total == 0:
        raise JevError(f"Nenhum token SIM/NAO no top-k: {[t.get('token') for t in top]}")
    return min(P_MAX, max(P_MIN, mass["yes"] / total))


def p_from_verbal(content: str) -> float:
    try:
        d = json.loads(content)
        side = _side(str(d["resposta"]))
        conf = float(d["confianca"]) / 100
    except (ValueError, KeyError, TypeError) as e:
        raise JevError(f"Resposta verbal inválida: {content[:200]!r}") from e
    if side is None or not 0 <= conf <= 1:
        raise JevError(f"Resposta verbal inválida: {content[:200]!r}")
    p = conf if side == "yes" else 1 - conf
    return min(P_MAX, max(P_MIN, p))


class ChatNoulBackend:
    def __init__(self, api_key: str | None = None, model: str | None = None, url: str = GROQ_URL,
                 mode: str = "auto", timeout: float = 60.0, max_workers: int = 4, max_retries: int = 5,
                 transport: httpx.BaseTransport | None = None, provider: str = "groq"):
        self.provider = provider
        api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("Defina GROQ_API_KEY (ou use --backend mock).")
        if mode not in ("auto", "logprobs", "verbal"):
            raise ValueError(f"modo inválido: {mode}")
        self.base_model = model or os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)
        self.url, self.mode, self.max_workers, self.max_retries = url, mode, max_workers, max_retries
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {api_key}"},
                                  limits=httpx.Limits(max_connections=max_workers))

    @property
    def model(self) -> str:
        # O modo entra na identidade: muda o cache e fica registrado na auditoria.
        return f"{self.provider}/{self.base_model}/{'verbal' if self.mode == 'verbal' else 'logprobs'}"

    def _messages(self, state: str, q: str, verbal: bool) -> list[dict]:
        fmt = ('Responda em JSON: {"resposta": "SIM" ou "NAO", "confianca": inteiro de 0 a 100}.'
               if verbal else "Responda com uma única palavra: SIM ou NAO.")
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"DOCUMENTO:\n<<<\n{state}\n>>>\n\nPERGUNTA: {q}\n\n{fmt}"}]

    def _ask_one(self, state: str, q: str) -> float:
        if self.mode != "verbal":
            body = {"model": self.base_model, "messages": self._messages(state, q, False),
                    "temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": 10}
            try:
                data = post_with_retry(self._http, self.url, body, self.max_retries)
                top = data["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
                return p_from_logprobs(top)
            except (JevError, KeyError, TypeError, IndexError) as e:
                if self.mode == "logprobs" or "indisponível" in str(e):
                    raise JevError(f"logprobs falhou: {e}") from e
                with self._lock:
                    self.mode = "verbal"  # provedor não devolve logprobs: degrada uma vez, para todos
        body = {"model": self.base_model, "messages": self._messages(state, q, True), "temperature": 0,
                "max_tokens": 60, "response_format": {"type": "json_object"}}
        data = post_with_retry(self._http, self.url, body, self.max_retries)
        return p_from_verbal(data["choices"][0]["message"]["content"])

    def ask_noul_batch(self, state: str, instructions: list[str]) -> list[float]:
        if not instructions:
            return []
        if self.mode == "auto":  # 1ª pergunta sozinha decide o modo antes de abrir o paralelismo
            first = [self._ask_one(state, instructions[0])]
            rest = instructions[1:]
        else:
            first, rest = [], instructions
        with ThreadPoolExecutor(self.max_workers) as ex:
            return first + list(ex.map(lambda q: self._ask_one(state, q), rest))
