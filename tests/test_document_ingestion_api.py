import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.documents.parser import DocumentParseError
from app.rag import RAGService
from evaluation.corpus import build_invalid_cases


class IndexCapturingRAG(RAGService):
    """Real parsing and validation, with indexing replaced by a capture."""

    def __init__(self):
        super().__init__(Settings())
        self.indexed = []

    def ingest_text(self, text, filename):
        self.indexed.append((text, filename))
        return "doc-1", 1


def upload(filename, content):
    return TestClient(main.app).post("/documents", files={"file": (filename, content)})


@pytest.mark.parametrize(
    "case", [case for case in build_invalid_cases() if not case.needs_docling], ids=lambda case: case.name
)
def test_invalid_uploads_return_client_errors(monkeypatch, case):
    service = IndexCapturingRAG()
    monkeypatch.setattr(main, "get_rag_service", lambda: service)
    response = upload(case.filename, case.content)
    assert response.status_code == case.status_code
    assert response.json()["detail"]
    assert service.indexed == []


def test_parse_errors_return_422(monkeypatch):
    class FailingRAG:
        def ingest(self, content, filename):
            raise DocumentParseError("The PDF file could not be parsed; it may be corrupt.")

    monkeypatch.setattr(main, "get_rag_service", lambda: FailingRAG())
    response = upload("broken.pdf", b"%PDF-1.4 broken")
    assert response.status_code == 422
    assert response.json() == {"detail": "The PDF file could not be parsed; it may be corrupt."}


def test_plain_text_reaches_index_unchanged(monkeypatch):
    service = IndexCapturingRAG()
    monkeypatch.setattr(main, "get_rag_service", lambda: service)
    response = upload("notes.txt", b"\xef\xbb\xbfinvoice_total is 5 * 3\r\n# not a heading\r\n")
    assert response.status_code == 201
    assert response.json() == {"document_id": "doc-1", "filename": "notes.txt", "chunks_indexed": 1}
    assert service.indexed == [("invoice_total is 5 * 3\n# not a heading", "notes.txt")]


def test_empty_upload_returns_400():
    assert upload("empty.pdf", b"").status_code == 400


def test_oversized_upload_returns_413(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: Settings(max_document_bytes=10))
    assert upload("big.txt", b"x" * 11).status_code == 413
