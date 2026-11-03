"""Docling extraction accuracy on generated documents.

These tests run the real Docling models (slow on first run while models load).
Skip them with: pytest -m "not integration"
"""
import pytest

from app.documents.parser import DocumentError, DocumentParser
from evaluation.corpus import build_corpus, build_invalid_cases
from evaluation.extraction_report import evaluate_case

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def parser():
    return DocumentParser()


@pytest.mark.parametrize("case", build_corpus(), ids=lambda case: case.name)
def test_extraction_meets_accuracy_thresholds(parser, case):
    result = evaluate_case(parser, case)
    if case.known_issue:
        if result.status == "PASS":
            pytest.fail(f"Known issue no longer reproduces; remove known_issue: {case.known_issue}")
        pytest.xfail(case.known_issue)
    assert result.status == "PASS", result.failures or result.note


@pytest.mark.parametrize(
    "case", [case for case in build_invalid_cases() if case.needs_docling], ids=lambda case: case.name
)
def test_unparseable_documents_raise_client_errors(parser, case):
    with pytest.raises(DocumentError) as error:
        parser.parse(case.content, case.filename)
    assert error.value.status_code == case.status_code


def test_encrypted_pdf_reports_password(parser):
    case = next(case for case in build_invalid_cases() if case.name == "encrypted_pdf")
    with pytest.raises(DocumentError, match="password-protected"):
        parser.parse(case.content, case.filename)


def test_pdf_pages_and_scanned_pages_are_reported(parser):
    cases = {case.name: case for case in build_corpus()}
    multipage = parser.parse(cases["pdf_multipage"].content, "pages.pdf")
    assert multipage.page_count == 3
    assert sorted(multipage.pages) == [1, 2, 3]
    assert multipage.scanned_pages == []

    scanned = parser.parse(cases["pdf_scanned"].content, "scanned.pdf")
    assert scanned.scanned_pages == [1]
    assert any("OCR" in warning for warning in scanned.warnings)
