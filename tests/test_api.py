from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from app import main


def image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_health() -> None:
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ingest_document(monkeypatch) -> None:
    class FakeRAG:
        def ingest(self, content, filename):
            assert content == b"hello"
            assert filename == "notes.md"
            return "doc-1", 2

    monkeypatch.setattr(main, "get_rag_service", lambda: FakeRAG())
    response = TestClient(main.app).post(
        "/documents", files={"file": ("notes.md", b"hello", "text/markdown")}
    )
    assert response.status_code == 201
    assert response.json() == {
        "document_id": "doc-1", "filename": "notes.md", "chunks_indexed": 2
    }


def test_search_uses_rag_service(monkeypatch) -> None:
    class FakeRAG:
        def search(self, query, limit, candidate_limit, document_id):
            assert (query, limit, candidate_limit, document_id) == ("hello", 3, 10, None)
            return []

    monkeypatch.setattr(main, "get_rag_service", lambda: FakeRAG())
    response = TestClient(main.app).post(
        "/search", json={"query": "hello", "limit": 3, "candidate_limit": 10}
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_ocr_returns_text_and_lines(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "extract_lines",
        lambda _image: [
            main.OCRLine(
                text="Hello world",
                confidence=0.99,
                box=[[1, 2], [10, 2], [10, 8], [1, 8]],
            )
        ],
    )
    response = TestClient(main.app).post(
        "/ocr", files={"file": ("sample.png", image_bytes(), "image/png")}
    )

    assert response.status_code == 200
    assert response.json()["text"] == "Hello world"
    assert response.json()["lines"][0]["confidence"] == 0.99


def test_rejects_non_image_content_type() -> None:
    response = TestClient(main.app).post(
        "/ocr", files={"file": ("notes.txt", b"not an image", "text/plain")}
    )
    assert response.status_code == 415


def test_ocr_indexes_extracted_text(monkeypatch) -> None:
    class FakeRAG:
        def ingest_text(self, text, filename):
            assert text == "Invoice total 100"
            assert filename == "sample_ocr.png"
            return "ocr-doc-1", 1

    monkeypatch.setattr(main, "get_rag_service", lambda: FakeRAG())
    monkeypatch.setattr(main, "extract_lines", lambda _: [
        main.OCRLine(text="Invoice total 100", confidence=0.99, box=[])
    ])
    response = TestClient(main.app).post(
        "/ocr?index=true",
        files={"file": ("sample_ocr.png", image_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["document_id"] == "ocr-doc-1"
    assert response.json()["chunks_indexed"] == 1
    assert response.json()["filename"] == "sample_ocr.png"


def test_ocr_does_not_index_by_default(monkeypatch) -> None:
    def unexpected_service():
        raise AssertionError("Extraction must not access Qdrant")

    monkeypatch.setattr(main, "get_rag_service", unexpected_service)
    monkeypatch.setattr(main, "extract_lines", lambda _: [])
    response = TestClient(main.app).post(
        "/ocr", files={"file": ("sample.png", image_bytes(), "image/png")}
    )
    assert response.status_code == 200
    assert response.json()["document_id"] is None
    assert response.json()["chunks_indexed"] == 0


def test_ocr_rejects_indexing_empty_text(monkeypatch) -> None:
    monkeypatch.setattr(main, "extract_lines", lambda _: [])
    response = TestClient(main.app).post(
        "/ocr?index=true", files={"file": ("blank.png", image_bytes(), "image/png")}
    )
    assert response.status_code == 422


def test_ocr_reports_indexing_failure(monkeypatch) -> None:
    class FakeRAG:
        def ingest_text(self, text, filename):
            raise RuntimeError("Qdrant unavailable")

    monkeypatch.setattr(main, "get_rag_service", lambda: FakeRAG())
    monkeypatch.setattr(main, "extract_lines", lambda _: [
        main.OCRLine(text="Invoice", confidence=0.99, box=[])
    ])
    response = TestClient(main.app).post(
        "/ocr?index=true", files={"file": ("invoice.png", image_bytes(), "image/png")}
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "OCR text indexing failed."


def test_rejects_invalid_image() -> None:
    response = TestClient(main.app).post(
        "/ocr", files={"file": ("fake.png", b"not an image", "image/png")}
    )
    assert response.status_code == 400


def test_extract_lines_supports_paddle_result(monkeypatch) -> None:
    class FakeEngine:
        def predict(self, _image):
            return [
                {
                    "res": {
                        "rec_texts": ["OCR text"],
                        "rec_scores": [0.95],
                        "rec_boxes": [[1, 2, 11, 12]],
                    }
                }
            ]

    monkeypatch.setattr(main, "get_ocr_engine", lambda: FakeEngine())
    lines = main.extract_lines(main.np.zeros((10, 20, 3), dtype=main.np.uint8))

    assert lines[0].text == "OCR text"
    assert lines[0].box == [[1, 2], [11, 2], [11, 12], [1, 12]]
