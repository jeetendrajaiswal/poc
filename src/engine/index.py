"""Per-page text index with BM25 lexical retrieval.

The "ask anything" layer needs to find the few pages relevant to an arbitrary
question inside a 200-700 page report. We do this deterministically and for free
(no embeddings, no vector store -> nothing leaves the machine, honouring the
privacy contract) with BM25 over per-page layout text.

Page text is extracted once (poppler layout, falling back to PyMuPDF) and cached,
so statements/QA/KPI passes over the same report share one extraction.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from functools import cached_property

import fitz  # PyMuPDF

_PDFTOTEXT = shutil.which("pdftotext")

# Small English + boilerplate stopword set; keeps BM25 focused on content terms.
_STOP = set("""a an the of to in on at for and or as is are was were be been being by with from this that these
those it its their our your his her they we you i he she them us has have had do does did not no nor so than then
there here which who whom whose what when where why how all any both each few more most other some such only own same
will would shall should can could may might must per ended year years as_at note notes rs inr crore lakh million""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9&/\-]*")


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def _dual_column_text(page) -> str | None:
    """Re-flow a genuine TWO-COLUMN page into reading order: left half in full, then right
    half in full. Returns None unless the page divides at ONE central gutter into two halves
    that EACH contain a full sub-block of text — so single-column tables are left untouched.

    Critical distinction (this is what a naive split gets wrong): a normal vertical financial
    table is `Label | Note | Value | Value` with a wide whitespace gap before the numbers. That
    gap looks like a gutter, but the number side is NOT its own text block, so it is NOT a valid
    split — the table stays intact (label stays with its values). Only when BOTH sides of a
    central gutter are substantial text blocks (Assets table | Equity table; two notes side by
    side) do we split, and then each half is rendered WHOLE (labels AND values together), which
    fixes the -layout interleaving without ever separating a label from its value."""
    W = page.rect.width
    words = [w for w in page.get_text("words") if w[4].strip()]
    if len(words) < 50 or W <= 0:
        return None
    # empty vertical gutters (no word covers the x-band) at least GUT pts wide
    cov = [0] * (int(W) + 2)
    for x0, _y0, x1, _y1, *_ in words:
        for x in range(max(0, int(x0)), min(len(cov), int(x1) + 1)):
            cov[x] += 1
    GUT = 16
    gutters, x = [], 1
    while x < int(W):
        if cov[x] == 0:
            s = x
            while x < int(W) and cov[x] == 0:
                x += 1
            if x - s >= GUT:
                gutters.append((s + x) // 2)
        else:
            x += 1

    def _text_rows(a, b):
        rows: dict[int, list] = {}
        for w in words:
            if a <= w[0] < b:
                rows.setdefault(round(w[1] / 3.0), []).append(w)
        return sum(1 for ws in rows.values()
                   if re.search(r"[A-Za-z]{4}", " ".join(x[4] for x in ws)))

    # the split is the gutter NEAREST the horizontal centre with a real text block on BOTH sides
    best = None
    for g in gutters:
        if not (0.25 * W <= g <= 0.75 * W):
            continue
        if _text_rows(0, g) >= 12 and _text_rows(g, int(W) + 1) >= 12:
            d = abs(g - W / 2)
            if best is None or d < best[1]:
                best = (g, d)
    if best is None:
        return None
    g = best[0]

    charw = max(4.0, W / 200.0)
    def _render(a, b):
        rows: dict[int, list] = {}
        for w in words:
            if a <= w[0] < b:
                rows.setdefault(round(w[1] / 3.0), []).append(w)
        lines = []
        for k in sorted(rows):
            line = ""
            for w in sorted(rows[k], key=lambda w: w[0]):
                pos = int((w[0] - a) / charw)
                if pos > len(line):
                    line = line.ljust(pos)
                elif line and not line.endswith(" "):
                    line += " "
                line += w[4]
            lines.append(line.rstrip())
        return "\n".join(lines)
    return _render(0, g) + "\n\n" + _render(g, int(W) + 1)


class PageIndex:
    """Lazily extracts and indexes a PDF's pages, then ranks them per query."""

    def __init__(self, path: str):
        self.path = path

    # --- page text (cached) ---------------------------------------------------
    @cached_property
    def page_text(self) -> list[str]:
        doc = fitz.open(self.path)
        n = doc.page_count
        texts: list[str] = []
        for p in range(1, n + 1):
            t = ""
            if _PDFTOTEXT:
                t = subprocess.run(
                    [_PDFTOTEXT, "-layout", "-f", str(p), "-l", str(p), self.path, "-"],
                    capture_output=True, text=True,
                ).stdout
            if len(t.strip()) < 20:               # poppler empty -> PyMuPDF
                t = doc[p - 1].get_text()
            texts.append(t)
        doc.close()
        return texts

    @property
    def n_pages(self) -> int:
        return len(self.page_text)

    # --- BM25 index (cached) --------------------------------------------------
    @cached_property
    def _bm25(self):
        docs = [_tokens(t) for t in self.page_text]
        N = max(1, len(docs))
        df: dict[str, int] = {}
        tf: list[dict[str, int]] = []
        for toks in docs:
            counts: dict[str, int] = {}
            for w in toks:
                counts[w] = counts.get(w, 0) + 1
            tf.append(counts)
            for w in counts:
                df[w] = df.get(w, 0) + 1
        lengths = [len(d) for d in docs]
        avg = (sum(lengths) / N) or 1.0
        idf = {w: math.log(1 + (N - n + 0.5) / (n + 0.5)) for w, n in df.items()}
        return tf, lengths, avg, idf

    def search(self, query: str, k: int = 6, expansion: list[str] | None = None) -> list[int]:
        """Return up to k 1-based page numbers most relevant to `query`,
        best first. `expansion` adds extra query terms (e.g. domain synonyms)."""
        tf, lengths, avg, idf = self._bm25
        terms = _tokens(query) + [t.lower() for t in (expansion or [])]
        if not terms:
            return []
        k1, b = 1.5, 0.75
        scored: list[tuple[float, int]] = []
        for i, counts in enumerate(tf):
            dl = lengths[i] or 1
            s = 0.0
            for w in terms:
                if w not in counts:
                    continue
                f = counts[w]
                s += idf.get(w, 0.0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avg))
            if s > 0:
                scored.append((s, i + 1))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [pg for _, pg in scored[:k]]

    # --- column-aware text (cached) ------------------------------------------
    @cached_property
    def column_text(self) -> list[str | None]:
        """Per-page text re-flowed into COLUMN order when the page is a genuine
        multi-column layout, else None. `pdftotext -layout` linearises by x-position,
        so on a two-notes-side-by-side page it glues the right column's table onto the
        left column's prose (e.g. a Share Capital table welded to held-for-sale text).
        Reading true word coordinates and splitting at the empty vertical gutter keeps
        each column contiguous. None for ordinary single-column pages so the proven
        -layout text is used unchanged."""
        doc = fitz.open(self.path)
        out: list[str | None] = []
        for p in range(doc.page_count):
            try:
                out.append(_dual_column_text(doc[p]))
            except Exception:
                out.append(None)
        doc.close()
        return out

    def text_of(self, pages: list[int], columns: bool = False) -> str:
        """Concatenate the layout text of the given 1-based pages, labelled.
        columns=True uses the column-reflowed text on genuine multi-column pages
        (falls back to -layout where the page is single-column)."""
        def _txt(p: int) -> str:
            if columns:
                c = self.column_text[p - 1]
                if c:
                    return c
            return self.page_text[p - 1]
        return "\n".join(f"=== PAGE {p} ===\n{_txt(p)}"
                         for p in pages if 1 <= p <= self.n_pages)
