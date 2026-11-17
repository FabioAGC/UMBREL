"""Backends Noul (sim/não -> P(sim) em [0,1]) e cliente do Jev (TypeSafe).

ATENÇÃO: o formato do payload/resposta foi montado a partir de resumos públicos
(POST https://api.typesafe.ai/v1/systemone, Bearer TYPESAFE_API_KEY) e NÃO foi validado
contra a doc oficial nem contra a API real. Se algo divergir, ajuste apenas
`JevClient._build_payload` e `JevClient._parse`; o resto do projeto só usa `ask_noul_batch`.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import httpx

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class JevError(RuntimeError):
    pass


def post_with_retry(http: httpx.Client, url: str, payload: dict, max_retries: int) -> dict:
    """POST com backoff exponencial + jitter em 408/429/5xx/timeout, respeitando Retry-After.
    4xx definitivo (auth, payload inválido) falha na hora: repetir não ajuda."""
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            r = http.post(url, json=payload)
            if r.status_code not in RETRY_STATUS:
                r.raise_for_status()
                return r.json()
            last = JevError(f"HTTP {r.status_code}")
            wait = float(r.headers.get("Retry-After", 0) or 0)
        except httpx.TransportError as e:  # timeout, conexão recusada/resetada
            last, wait = e, 0.0
        except httpx.HTTPStatusError as e:
            raise JevError(f"Backend recusou a requisição: {e.response.status_code} {e.response.text[:300]}") from e
        if attempt < max_retries:
            time.sleep(min(60.0, max(wait, 0.5 * 2 ** attempt)) + random.uniform(0, 0.25))
    raise JevError(f"Backend indisponível após {max_retries + 1} tentativas: {last}")


class NoulBackend(Protocol):
    model: str

    def ask_noul_batch(self, state: str, instructions: list[str]) -> list[float]:
        """Várias perguntas sobre o MESMO state; devolve P(sim) na mesma ordem."""
        ...


class JevClient:
    def __init__(self, api_key: str | None = None, url: str = DEFAULT_URL, model: str = "jev-latest",
                 timeout: float = 60.0, batch_size: int = 16, max_workers: int = 8,
                 max_retries: int = 4, transport: httpx.BaseTransport | None = None):
        api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            raise RuntimeError("Defina TYPESAFE_API_KEY (ou use --backend mock).")
        self.url, self.model = url, model
        self.batch_size, self.max_workers, self.max_retries = batch_size, max_workers, max_retries
        # Um cliente só: conexões keep-alive reaproveitadas entre perguntas e threads.
        self._http = httpx.Client(
            timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            limits=httpx.Limits(max_connections=max_workers, max_keepalive_connections=max_workers),
        )

    def _build_payload(self, state: str, instructions: list[str]) -> dict:
        # TODO(validar): nomes de campos conforme doc oficial.
        return {
            "model": self.model,
            "state": [{"role": "user", "content": state}],
            "questions": [{"type": "noul", "id": f"q{i}", "instructions": t}
                          for i, t in enumerate(instructions)],
        }

    @staticmethod
    def _prob(item) -> float:
        if isinstance(item, (int, float)):
            return float(item)
        for key in ("probability", "value", "p"):
            if key in item:
                return float(item[key])
        raise JevError(f"Resposta sem probabilidade: {item!r}")

    @classmethod
    def _parse(cls, body: dict, n: int) -> list[float]:
        # TODO(validar): caminho da probabilidade na resposta.
        answers = body.get("answers") or body.get("results")
        if isinstance(answers, dict):
            items = [answers[f"q{i}"] for i in range(n)]
        elif isinstance(answers, list) and all(isinstance(a, dict) and "id" in a for a in answers):
            by_id = {a["id"]: a for a in answers}
            items = [by_id[f"q{i}"] for i in range(n)]
        elif isinstance(answers, list):
            items = answers
        else:
            raise JevError(f"Resposta Jev inesperada: {body!r}")
        if len(items) != n:
            raise JevError(f"Esperava {n} respostas, veio {len(items)}")
        ps = [cls._prob(it) for it in items]
        if any(not 0.0 <= p <= 1.0 for p in ps):
            raise JevError(f"Probabilidade fora de [0,1]: {ps}")
        return ps

    def _post(self, payload: dict) -> dict:
        return post_with_retry(self._http, self.url, payload, self.max_retries)

    def ask_noul_batch(self, state: str, instructions: list[str]) -> list[float]:
        chunks = [instructions[i:i + self.batch_size] for i in range(0, len(instructions), self.batch_size)]
        with ThreadPoolExecutor(self.max_workers) as ex:
            parts = list(ex.map(lambda c: self._parse(self._post(self._build_payload(state, c)), len(c)), chunks))
        return [p for part in parts for p in part]


class CachedBackend:
    """Cache em SQLite das respostas: mesma (modelo, state, pergunta) -> mesma P(sim).
    Torna reexecuções gratuitas e determinísticas (auditoria: refazer dá o mesmo resultado)."""

    def __init__(self, inner: NoulBackend, path: str | Path):
        self.inner = inner
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS noul (key TEXT PRIMARY KEY, p REAL NOT NULL)")
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @property
    def model(self) -> str:
        return self.inner.model  # lido a cada uso: o backend pode mudar de modo (logprobs -> verbal)

    def _key(self, state: str, q: str) -> str:
        return hashlib.sha256(f"{self.model}\0{state}\0{q}".encode()).hexdigest()

    def ask_noul_batch(self, state: str, instructions: list[str]) -> list[float]:
        keys = [self._key(state, q) for q in instructions]
        with self._lock:
            found = dict(self._db.execute(
                f"SELECT key, p FROM noul WHERE key IN ({','.join('?' * len(keys))})", keys).fetchall()) if keys else {}
        missing = [i for i, k in enumerate(keys) if k not in found]
        self.hits += len(keys) - len(missing)
        self.misses += len(missing)
        if missing:
            ps = self.inner.ask_noul_batch(state, [instructions[i] for i in missing])
            # grava com a identidade ATUAL do modelo (o modo pode ter mudado durante a chamada)
            with self._lock, self._db:
                self._db.executemany("INSERT OR REPLACE INTO noul VALUES (?, ?)",
                                     [(self._key(state, instructions[i]), p) for i, p in zip(missing, ps)])
            found.update({keys[i]: p for i, p in zip(missing, ps)})
        return [found[k] for k in keys]


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(s))


class MockBackend:
    """Backend offline e determinístico para testes/demo. NÃO substitui o Jev.
    - "é requisito?": procura palavras de obrigação/limite no trecho.
    - "atende?"/"viola?": fração dos termos significativos do requisito presentes no laudo."""

    model = "mock"
    OBLIGATION = re.compile(r"\b(deve|devem|obrigatori\w*|minimo|maximo|exigid\w*|limite|nao pode)\b")
    STOP = {"deve", "devem", "ser", "de", "do", "da", "o", "a", "os", "as", "e", "com", "no", "na",
            "em", "para", "por", "um", "uma", "que", "ao", "dos", "das", "laudo", "conter", "possuir"}

    def _one(self, state: str, q: str) -> float:
        if q.startswith("REQUISITO?"):
            return 0.95 if self.OBLIGATION.search(_norm(state)) else 0.05
        req = q.split("«", 1)[-1].rstrip("»")
        terms = {t for t in _tokens(req) if t not in self.STOP}
        if not terms:
            p = 0.5
        else:
            frac = len(terms & set(_tokens(state))) / len(terms)
            p = 0.95 if frac >= 0.8 else 0.05 if frac <= 0.3 else 0.5
        return 1 - p if q.startswith("VIOLA?") else p

    def ask_noul_batch(self, state: str, instructions: list[str]) -> list[float]:
        return [self._one(state, q) for q in instructions]


def make_backend(name: str, cache_path: str | Path | None = None) -> NoulBackend:
    if name == "mock":
        backend: NoulBackend = MockBackend()
    elif name == "groq":
        from .llm import ChatNoulBackend

        backend = ChatNoulBackend(mode=os.environ.get("GROQ_MODE", "auto"))
    elif name == "ollama":
        # LLM local (Ollama, API compatível com OpenAI). Nada sai da rede.
        from .llm import ChatNoulBackend

        backend = ChatNoulBackend(
            api_key="ollama", provider="ollama",
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"),
            url=os.environ.get("OLLAMA_URL", "http://localhost:11434/v1/chat/completions"),
            mode=os.environ.get("LLM_MODE", "auto"),
            timeout=float(os.environ.get("LLM_TIMEOUT", "600")),
            max_workers=int(os.environ.get("LLM_MAX_WORKERS", "1")))
    else:
        backend = JevClient()
    return CachedBackend(backend, cache_path) if cache_path else backend
