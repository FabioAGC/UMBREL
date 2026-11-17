"""OCR consciente de tabela: detecta a grade, lê cada célula isoladamente e devolve linhas
"c1 | c2 | ...". O OCR de página inteira do Tesseract perde linhas e vírgulas decimais em
tabelas com grade (ex.: "21,50" lido como "250") mesmo com confiança média alta."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

_NUMERIC = re.compile(r"^[\s\d.,%+\-/]*\d[\s\d.,%+\-/]*$")


@dataclass
class Cell:
    x: int
    y: int
    w: int
    h: int
    text: str = ""
    conf: float | None = None
    doubt: str | None = None  # leituras divergentes de um valor numérico


def _grid_mask(binary: np.ndarray):
    import cv2

    h, w = binary.shape
    horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 40, 10), 1)))
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 80, 10))))
    grid = cv2.dilate(cv2.add(horiz, vert), np.ones((3, 3), np.uint8))
    return grid


def find_cells(gray: np.ndarray, min_w: int = 25, min_h: int = 18) -> list[Cell]:
    import cv2

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    grid = _grid_mask(binary)
    # Células = regiões brancas fechadas pela grade.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(255 - grid, connectivity=4)
    H, W = gray.shape
    cells = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < min_w or h < min_h or w > 0.98 * W or h > 0.5 * H:
            continue  # ruído, ou o fundo da página
        if area < 0.6 * w * h:
            continue  # região irregular (não é célula retangular)
        cells.append(Cell(int(x), int(y), int(w), int(h)))
    return cells


def group_rows(cells: list[Cell], tol: float = 0.5) -> list[list[Cell]]:
    """Agrupa por centro vertical; células mescladas (mais altas) entram na linha do seu centro."""
    rows: list[list[Cell]] = []
    for c in sorted(cells, key=lambda c: (c.y + c.h / 2, c.x)):
        cy = c.y + c.h / 2
        if rows:
            last = rows[-1]
            ly = sum(k.y + k.h / 2 for k in last) / len(last)
            lh = min(k.h for k in last)
            if abs(cy - ly) <= tol * min(lh, c.h):
                last.append(c)
                continue
        rows.append([c])
    return [sorted(r, key=lambda c: c.x) for r in rows]


def _read(img, lang: str, config: str) -> tuple[str, float | None]:
    import pytesseract

    d = pytesseract.image_to_data(img, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    words = [(w, float(cf)) for w, cf in zip(d["text"], d["conf"]) if w.strip()]
    confs = [cf for _, cf in words if cf >= 0]
    return " ".join(w for w, _ in words), (sum(confs) / len(confs) if confs else None)


def _norm_num(t: str) -> str:
    return re.sub(r"\s+", "", t)


def _ocr_cell(gray: np.ndarray, c: Cell, lang: str, single_line: bool = True) -> tuple[str, float | None, str | None]:
    """Lê uma célula. Se o conteúdo é numérico, confirma com leituras independentes (outro modelo
    de idioma, outra escala, lista de caracteres restrita) e decide por maioria; sem maioria, marca
    a dúvida. Trocar 7 por 1 num resultado de ensaio é o erro que mais custa e o que a confiança
    média do Tesseract menos denuncia."""
    from PIL import Image

    pad = 3
    crop = gray[c.y + pad:c.y + c.h - pad, c.x + pad:c.x + c.w - pad]
    if crop.size == 0 or crop.min() > 200:  # célula vazia
        return "", None, None
    base = Image.fromarray(np.pad(crop, 12, constant_values=255))
    big = base.resize((base.width * 2, base.height * 2), Image.LANCZOS)
    psm = 7 if single_line else 6  # uma linha vs. célula com quebra de linha
    text, conf = _read(big, lang, f"--psm {psm}")
    if not text or psm != 7 or not _NUMERIC.match(text):
        return text, conf, None

    votes = [_norm_num(text), _norm_num(_read(base, "eng", "--psm 7")[0])]
    if votes[0] != votes[1]:
        votes.append(_norm_num(_read(base, lang, "--psm 7 -c tessedit_char_whitelist=0123456789,.-%/")[0]))
    best, n = Counter(votes).most_common(1)[0]
    if n >= 2:
        return best, conf, None
    return f"[ILEGÍVEL: {' / '.join(dict.fromkeys(votes))}]", conf, " / ".join(dict.fromkeys(votes))


def ocr_page(gray: np.ndarray, lang: str = "por", workers: int = 8) -> tuple[str, float, list[str]]:
    """Texto da página: blocos fora das tabelas pelo OCR normal, tabelas célula a célula, na ordem vertical."""
    from concurrent.futures import ThreadPoolExecutor

    import pytesseract
    from PIL import Image

    cells = find_cells(gray)
    # Célula de várias linhas = bem mais alta que a típica da página (não um limite fixo em px,
    # que muda com o DPI e desligaria a conferência dos números).
    median_h = float(np.median([c.h for c in cells])) if cells else 0.0
    with ThreadPoolExecutor(workers) as ex:
        reads = ex.map(lambda c: _ocr_cell(gray, c, lang, c.h < 1.6 * median_h), cells)
        for c, (t, cf, doubt) in zip(cells, reads):
            c.text, c.conf, c.doubt = t, cf, doubt

    # Fora da grade: apaga as células e lê o resto como texto corrido.
    rest = gray.copy()
    for c in cells:
        rest[c.y:c.y + c.h, c.x:c.x + c.w] = 255
    d = pytesseract.image_to_data(Image.fromarray(rest), lang=lang, config="--psm 4",
                                  output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list] = {}
    for i, w in enumerate(d["text"]):
        if w.strip():
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(key, []).append((d["top"][i], w, float(d["conf"][i])))

    items: list[tuple[float, str]] = []
    confs: list[float] = []
    for ws in lines.values():
        items.append((min(t for t, _, _ in ws), " ".join(w for _, w, _ in ws)))
        confs += [cf for _, _, cf in ws if cf >= 0]
    for row in group_rows([c for c in cells if c.text]):
        items.append((min(c.y for c in row), " | ".join(c.text for c in row)))
        confs += [c.conf for c in row if c.conf is not None]
    items.sort(key=lambda it: it[0])
    doubts = []
    for row in group_rows([c for c in cells if c.text]):
        doubts += [f"{row[0].text}: {c.doubt}" for c in row if c.doubt]
    return "\n\n".join(t for _, t in items), (sum(confs) / len(confs) if confs else 0.0), doubts
