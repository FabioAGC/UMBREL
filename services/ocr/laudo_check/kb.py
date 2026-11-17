"""Base de conhecimento: guidelines (PDF/imagem/txt) -> OCR com cache -> passagens -> BM25."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .ocr import SUPPORTED, extract_pages

GENERAL_DIR = "geral"  # documentos aplicados a TODOS os materiais
CACHE_DIRNAME = ".cache"
MIN_PASSAGE = 25


@dataclass
class Passage:
    doc: str
    page: int
    index: int
    text: str

    @property
    def ref(self) -> str:
        return f"{self.doc} p.{self.page}#{self.index}"


@dataclass
class Doc:
    name: str  # caminho relativo à pasta
    general: bool
    sha256: str
    passages: list[Passage]
    span: range = field(default=range(0))  # posições das passagens no índice global


def tokenize(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z0-9]+", s) if len(t) > 1]


def split_passages(doc: str, page: int, text: str) -> list[Passage]:
    """Um requisito costuma ser um item numerado ou um parágrafo curto."""
    chunks: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        parts = re.split(r"\n(?=\s*(?:\d+(?:\.\d+)*[.)]?\s|[-•*]\s))", block)
        chunks += [" ".join(p.split()) for p in parts]
    chunks = [c for c in chunks if len(c) >= MIN_PASSAGE]
    return [Passage(doc, page, i, c) for i, c in enumerate(chunks, 1)]


class KnowledgeBase:
    def __init__(self, folder: str | Path, lang: str = "por", cache_dir: str | Path | None = None,
                 workers: int = 4):
        self.folder = Path(folder)
        self.lang = lang
        self.cache = Path(cache_dir) if cache_dir else self.folder / CACHE_DIRNAME
        self.docs: list[Doc] = []
        self._load(workers)
        self._build_bm25()

    # --- carga com cache de OCR (chave = hash do conteúdo + idioma) ---
    def _read(self, f: Path) -> tuple[str, list[str]]:
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        cf = self.cache / "ocr" / f"{sha}-{self.lang}.json"
        if cf.exists():
            return sha, json.loads(cf.read_text(encoding="utf-8"))
        pages = extract_pages(f, self.lang)
        cf.parent.mkdir(parents=True, exist_ok=True)
        tmp = cf.with_suffix(".tmp")
        tmp.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cf)  # escrita atômica: processo interrompido não deixa cache corrompido
        return sha, pages

    def _load(self, workers: int) -> None:
        files = sorted(p for p in self.folder.rglob("*")
                       if p.is_file() and p.suffix.lower() in SUPPORTED and self.cache not in p.parents)
        # Tesseract roda em subprocesso: threads bastam para paralelizar o OCR inicial.
        with ThreadPoolExecutor(workers) as ex:
            read = list(ex.map(self._read, files))
        for f, (sha, pages) in zip(files, read):
            rel = f.relative_to(self.folder)
            passages = [ps for n, t in enumerate(pages, 1) for ps in split_passages(rel.as_posix(), n, t)]
            self.docs.append(Doc(rel.as_posix(), rel.parts[0] == GENERAL_DIR, sha, passages))
        if not self.docs:
            raise ValueError(f"Nenhum documento suportado em {self.folder}")

    # --- BM25 sobre as passagens ---
    def _build_bm25(self) -> None:
        self._all: list[Passage] = []
        for d in self.docs:
            d.span = range(len(self._all), len(self._all) + len(d.passages))
            self._all += d.passages
        self._tf = [Counter(tokenize(p.text)) for p in self._all]
        self._len = [sum(c.values()) for c in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for c in self._tf:
            df.update(c.keys())
        n = len(self._all)
        self._idf = {t: math.log(1 + (n - v + 0.5) / (v + 0.5)) for t, v in df.items()}

    def _score(self, q: set[str], i: int, k1: float = 1.5, b: float = 0.75) -> float:
        tf, dl, s = self._tf[i], self._len[i], 0.0
        for t in q:
            if t in tf:
                s += self._idf[t] * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / self._avg))
        return s

    def search(self, query: str, k: int = 5) -> list[tuple[Passage, float]]:
        q = set(tokenize(query))
        ranked = sorted(((self._all[i], self._score(q, i)) for i in range(len(self._all))),
                        key=lambda x: -x[1])
        return [r for r in ranked[:k] if r[1] > 0]

    def rank_docs(self, query: str, top_passages: int = 5) -> list[tuple[Doc, float]]:
        """Pontua cada guideline específico pela soma das melhores passagens para o laudo."""
        q = set(tokenize(query))
        out = []
        for d in self.docs:
            if d.general:
                continue
            scores = sorted((self._score(q, i) for i in d.span), reverse=True)[:top_passages]
            out.append((d, sum(scores)))
        return sorted(out, key=lambda x: -x[1])

    def general_docs(self) -> list[Doc]:
        return [d for d in self.docs if d.general]

    def curation_path(self, doc: Doc) -> Path:
        return self.folder / f"{doc.name}.requisitos.yaml"
