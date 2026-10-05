"""Opt-in QA observation of REAL tool results, never a mocked tool adapter.

Only public research/local CSV results are saved. No key-store requests,
credentials, channel traffic, model payloads or unrestricted file data.
"""
import os
from qa.acceptance.tool_evidence import install
path = os.environ.get("WISE_QA_TOOL_EVIDENCE_PATH")
if not path or not os.environ.get("WISE_QA_BUDGET_PATH"):
    raise RuntimeError("QA instrumentation requires an explicitly opted-in isolated run")
install(path)
receipt_path = os.environ.get("WISE_QA_RECEIPT_DRAFT_PATH")
if receipt_path:
    from qa.acceptance.receipt_draft_evidence import install as install_receipt_drafts
    install_receipt_drafts(receipt_path)
research_path = os.environ.get("WISE_QA_RESEARCH_DRAFT_PATH")
if research_path:
    from qa.acceptance.research_draft_evidence import install as install_research_drafts
    install_research_drafts(research_path, capture_public_draft=os.environ.get("WISE_QA_PUBLIC_GEMINI_DRAFT") == "1")
public_docs_path = os.environ.get("WISE_QA_PUBLIC_DOCS_EVIDENCE_PATH")
if public_docs_path:
    from qa.acceptance.research_draft_evidence import install_public_docs
    install_public_docs(public_docs_path)
structured_write_path = os.environ.get("WISE_QA_STRUCTURED_WRITE_EVIDENCE_PATH")
if structured_write_path:
    from qa.acceptance.structured_write_evidence import install as install_structured_writes
    install_structured_writes(structured_write_path)
from api.server import app
