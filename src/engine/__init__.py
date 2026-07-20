"""Quarterly-report engine (client deliverable).

Layers:
    tables.py / tables_llm.py / filing_chat.py   raw statement extraction
                                                  (whole-file upload + tie-out)
    vision.py                                     consensus vision for scanned pages
    statements.py                                 shared parsing/validation helpers
    client_map.py                                 raw statements -> client taxonomy
                                                  workbooks (wide default + long)
    mdna.py                                       MD&A summary from annual report

The original annual-report datapoint engine lives in orig_bkp/ (not imported).
"""
