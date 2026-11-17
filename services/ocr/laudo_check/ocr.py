from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Tesseract usa OpenMP; com várias chamadas em paralelo, threads internas disputam CPU e tudo
# fica até 20x mais lento. Uma thread por processo e o paralelismo fica do nosso lado.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SUPPORTED = IMAGE_EXTS | {".pdf", ".txt"}
MIN_NATIVE_CHARS = 30  # abaixo disso a página do PDF é tratada como escaneada


@dataclass
class Page:
    text: str
    ocr_conf: float | None  # confiança média do Tesseract (0-100); None = texto nativo
    doubts: list[str] = field(default_factory=list)  # valores numéricos com leituras divergentes


def extract_text(path: str | Path, lang: str = "por") -> str:
    return "\n\n".join(p.text for p in extract(path, lang))


def extract_pages(path: str | Path, lang: str = "por") -> list[str]:
    return [p.text for p in extract(path, lang)]


def extract(path: str | Path, lang: str = "por") -> list[Page]:
    """Texto por página de PDF (camada de texto, com OCR nas páginas escaneadas), imagem ou .txt."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".txt":
        return [Page(path.read_text(encoding="utf-8"), None)]
    if ext == ".pdf":
        import fitz  # pymupdf

        pages, header = [], None
        with fitz.open(path) as doc:
            for page in doc:
                if len(page.get_text().strip()) >= MIN_NATIVE_CHARS:
                    text, header = _native_page(page, header)
                    pages.append(Page(text, None))
                else:
                    pix = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY)
                    pages.append(_ocr_gray(_pix_to_gray(pix), pix.tobytes("png"), lang))
        return pages
    if ext in IMAGE_EXTS:
        import io

        import numpy as np
        from PIL import Image, ImageOps

        data = path.read_bytes()
        gray = np.asarray(ImageOps.grayscale(Image.open(io.BytesIO(data))))
        return [_ocr_gray(gray, data, lang)]
    raise ValueError(f"Formato não suportado: {ext}")


def _ocr_bytes(data: bytes, lang: str) -> Page:
    """Uma passada só do Tesseract: texto e confiança saem do mesmo image_to_data."""
    import io

    import pytesseract
    from PIL import Image, ImageOps

    img = ImageOps.autocontrast(ImageOps.grayscale(Image.open(io.BytesIO(data))))
    d = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(word)
        if float(d["conf"][i]) >= 0:
            confs.append(float(d["conf"][i]))
    out, prev_par = [], None
    for (block, par, _), ws in lines.items():
        if prev_par is not None and (block, par) != prev_par:
            out.append("")  # linha em branco entre parágrafos: split_passages depende disso
        out.append(" ".join(ws))
        prev_par = (block, par)
    text = "\n".join(out)
    return Page(text, sum(confs) / len(confs) if confs else 0.0)


# --- texto nativo com tabelas -------------------------------------------------------------

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)*")


def _clean(c) -> str:
    return " ".join(str(c).split()) if c is not None else ""


def table_lines(rows: list[list], prev_header: list[str] | None = None) -> tuple[list[str], list[str] | None]:
    """Linhas de tabela -> uma linha autocontida por registro ("Cabeçalho: valor; ...").

    - tabela rótulo/valor (uma célula preenchida por linha, valor ANTES do rótulo, como nas SPECs
      exportadas de PLM) vira "rótulo: valor";
    - cabeçalho em várias linhas é mesclado coluna a coluna;
    - tabela sem cabeçalho e com o mesmo nº de colunas da anterior é continuação (quebra de página).
    """
    rows = [[_clean(c) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], prev_header
    if all(sum(1 for c in r if c) == 1 for r in rows) and len(rows[0]) == 2:
        out, pending = [], None
        for r in rows:
            if r[1]:
                pending = r[1]
            else:
                out.append(f"{r[0]}: {pending}" if pending else r[0])
                pending = None
        return out, prev_header
    is_data = [any(_NUM.fullmatch(c) for c in r) for r in rows]
    # Sem célula numérica: 1ª linha é cabeçalho e o resto é dado (ex.: local de produção).
    n_head = is_data.index(True) if True in is_data else (1 if len(rows) > 1 else len(rows))
    if n_head == 0 and prev_header and len(prev_header) == len(rows[0]):
        header = prev_header
    elif n_head == 0:
        header = None
    else:
        header = [" ".join(r[i] for r in rows[:n_head] if r[i]) for i in range(len(rows[0]))]
    out = []
    for r in rows[n_head:]:
        if header:
            out.append("; ".join(f"{h}: {v}" if h else v for h, v in zip(header, r) if v))
        else:
            out.append(" | ".join(c for c in r if c))
    if n_head == len(rows) and header:  # só cabeçalho: guarda para a continuação
        return [], header
    return out, header or prev_header


def _native_page(page, prev_header):
    """Blocos de texto fora das tabelas + tabelas linha a linha, na ordem vertical da página."""
    items: list[tuple[float, str]] = []
    tables = page.find_tables().tables
    boxes = [t.bbox for t in tables]
    for t in tables:
        lines, prev_header = table_lines(t.extract(), prev_header)
        y0 = t.bbox[1]
        items += [(y0 + i * 1e-3, line) for i, line in enumerate(lines)]
    for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes):
            items.append((y0, " ".join(text.split())))
    items.sort(key=lambda it: it[0])
    return "\n\n".join(t for _, t in items if t), prev_header


# --- OCR ------------------------------------------------------------------------------------

def _pix_to_gray(pix):
    import numpy as np

    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w)


def _ocr_gray(gray, png_bytes: bytes, lang: str) -> Page:
    """Com OpenCV: OCR célula a célula nas tabelas (preserva números e colunas). Sem: OCR de página."""
    try:
        import cv2  # noqa: F401

        from .table_ocr import ocr_page
    except ImportError:
        return _ocr_bytes(png_bytes, lang)
    text, conf, doubts = ocr_page(gray, lang)
    return Page(text, conf, doubts)
