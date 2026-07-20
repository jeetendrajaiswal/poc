"""Report — the single entry point that ties the engine together.

    r = Report("annual_report.pdf")
    r.format            -> 'bank' | 'nbfc' | 'insurer' | 'manufacturer'
    r.statements()      -> {bs,pl,cf} validated via arithmetic tie-out
    r.ask("question")   -> citation-grounded Answer to anything in the report
    r.kpis()            -> sector-appropriate KPIs (from config/kpis.yaml)

Routing (all automatic):
  * SMALL report (fits in context, e.g. a quarterly results filing) -> whole-document
    path: the entire report goes to the model in one shot, no page-location needed.
    A scanned small report is uploaded and read natively, then deleted.
  * BIG report (annual) -> locate -> extract -> verify pipeline; scanned -> vision.
Everything shares ONE page-text extraction.
"""
from __future__ import annotations

import os
from functools import cached_property

from src import llm
from src.engine import sector, whole_doc
from src.engine import statements as st
from src.engine.index import PageIndex
from src.engine.qa import Answer, ask

SMALL_PAGES = 50          # quarterly results are short; annual reports are hundreds of pages


class Report:
    def __init__(self, path: str):
        self.path = path
        self.index = PageIndex(path)

    # --- nature / classification ---------------------------------------------
    @cached_property
    def small(self) -> bool:
        """Fits in context -> use the whole-document path (no page location)."""
        return self.index.n_pages <= SMALL_PAGES and whole_doc.is_small(self.index.page_text)

    @cached_property
    def scanned(self) -> bool:
        return st.is_image_pdf(self.path)

    @cached_property
    def image_only(self) -> bool:
        if self.small:
            return self.scanned          # small scanned -> handled by whole_doc (file upload)
        if self.scanned:
            return True
        # big text PDF whose financials are designed/scanned -> nothing locatable
        return all(self._text_statements[k].status == "no-candidate" for k in st.KINDS)

    @cached_property
    def format(self) -> str:
        if self.small:
            return sector.detect_format(self.index.page_text)   # union over the whole small doc
        if self.image_only:
            return "manufacturer"          # can't fingerprint text; safe default
        cands = st.candidate_pages(self.path, "bs")
        texts = [self.index.page_text[p - 1] for p in cands] or self.index.page_text
        return sector.detect_format(texts)

    @property
    def format_label(self) -> str:
        return sector.format_label(self.format)

    # --- primary statements (arithmetic tie-out) -----------------------------
    @cached_property
    def _text_statements(self) -> dict[str, st.StatementResult]:
        if self.scanned:
            return {k: st.StatementResult(kind=k, status="no-candidate") for k in st.KINDS}
        return {k: st.validate_statement(self.path, k) for k in st.KINDS}

    @cached_property
    def _statements(self) -> dict[str, st.StatementResult]:
        if self.small:
            if self.scanned:
                # upload the PDF, read it natively, delete it (success OR error)
                with open(self.path, "rb") as fh:
                    data = fh.read()
                with llm.ephemeral_file(data, os.path.basename(self.path)) as fid:
                    return whole_doc.statements_whole(file_id=fid)
            full = self.index.text_of(list(range(1, self.index.n_pages + 1)))
            return whole_doc.statements_whole(full_text=full)
        if self.image_only:
            from src.engine import vision        # lazy: avoids import cost on text path
            return vision.validate_report_vision(self.path).statements
        return self._text_statements

    def statements(self) -> dict[str, st.StatementResult]:
        return self._statements

    @property
    def fully_validated(self) -> bool:
        """All PRESENT statements tie out. 'absent' is fine (Q1/Q3 omit BS & CF)."""
        sts = [self._statements[k].status for k in st.KINDS]
        return all(s in ("tie", "absent") for s in sts) and any(s == "tie" for s in sts)

    def statement_page(self, kind: str) -> list[int]:
        r = self._statements.get(kind)
        return [r.page] if r and r.page else []

    # --- ask anything --------------------------------------------------------
    def ask(self, question: str, expansion: list[str] | None = None) -> Answer:
        if self.small:
            if self.scanned:
                with open(self.path, "rb") as fh:
                    data = fh.read()
                with llm.ephemeral_file(data, os.path.basename(self.path)) as fid:
                    return _ask_file(fid, question)
            # whole-document: feed every page, no retrieval
            allpages = list(range(1, self.index.n_pages + 1))
            return ask(self.index, question, pages=allpages)
        # big report: retrieve, biased toward the relevant located statement page
        prefer: list[int] = []
        ql = question.lower()
        if any(w in ql for w in ("asset", "liabilit", "equity", "borrow", "net worth")):
            prefer += self.statement_page("bs")
        if any(w in ql for w in ("revenue", "profit", "income", "expense", "eps", "earning", "tax")):
            prefer += self.statement_page("pl")
        if any(w in ql for w in ("cash", "operating", "investing", "financing")):
            prefer += self.statement_page("cf")
        return ask(self.index, question, expansion=expansion, prefer_pages=prefer)

    # --- taxonomy datapoints (meaning + structure, no company names) ---------
    def datapoints(self, scope: str = "standalone"):
        """Extract the taxonomy's target datapoints for a scope, grouped by note."""
        from src.engine.datapoints import extract_datapoints
        return extract_datapoints(self.index, scope)

    # --- sector KPIs ---------------------------------------------------------
    def kpis(self) -> dict[str, Answer]:
        out: dict[str, Answer] = {}
        for spec in sector.kpi_catalog(self.format):
            out[spec["key"]] = self.ask(spec["q"], expansion=spec.get("also"))
        return out


def _ask_file(file_id: str, question: str) -> Answer:
    """Answer from an uploaded (scanned, small) PDF read natively. No page grounding."""
    from src.engine.qa import _SCHEMA, _INSTR
    out = llm.extract_json(
        instructions=_INSTR, user_input=f"QUESTION: {question}\n\nAnswer from the attached file.",
        schema_name="answer", schema=_SCHEMA, file_ids=[file_id],
        max_output_tokens=900, reasoning="low",
    )
    if out.get("_empty") or not out.get("found"):
        return Answer(question=question, found=False, pages_searched=[])
    return Answer(question=question, found=True, answer=out.get("answer", ""),
                  value=out.get("value"), unit=out.get("unit"), page=out.get("page"),
                  quote=out.get("quote", ""), confidence=float(out.get("confidence", 0.0)),
                  grounded=False, pages_searched=[])
