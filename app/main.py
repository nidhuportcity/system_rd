from __future__ import annotations

import io
import logging
import os
import threading
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.structured.router import router as structured_router
from app.rag import RAGService
from app.schemas import AskRequest, AskResponse, DeleteResponse, IngestResponse, SearchRequest, SearchResponse

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/tiff",
}
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
_ocr_lock = threading.Lock()
logger = logging.getLogger(__name__)


class OCRLine(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
    box: list[list[float]]


class OCRResponse(BaseModel):
    text: str
    lines: list[OCRLine]
    document_id: str | None = None
    filename: str | None = None
    chunks_indexed: int = 0


@lru_cache(maxsize=1)
def get_ocr_engine() -> Any:
    # Load lazily so health checks do not wait for the large OCR model.
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang=os.getenv("OCR_LANG", "en"),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        engine="paddle",
    )


def _result_data(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        data: Any = result
    else:
        data = getattr(result, "json", None)
        if callable(data):
            data = data()
    if not isinstance(data, Mapping):
        raise RuntimeError("PaddleOCR returned an unsupported result format")
    nested = data.get("res")
    return nested if isinstance(nested, Mapping) else data


def _normalize_box(box: Any) -> list[list[float]]:
    value = box.tolist() if hasattr(box, "tolist") else box
    if len(value) == 4 and not isinstance(value[0], (list, tuple, np.ndarray)):
        x1, y1, x2, y2 = (float(item) for item in value)
        return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    return [[float(coordinate) for coordinate in point] for point in value]


def extract_lines(image: np.ndarray) -> list[OCRLine]:
    # Serialize inference because a shared Paddle pipeline may not be thread-safe.
    with _ocr_lock:
        results = get_ocr_engine().predict(image)

    lines: list[OCRLine] = []
    for result in results:
        data = _result_data(result)
        texts = data.get("rec_texts", [])
        scores = data.get("rec_scores", [])
        boxes = data.get("rec_polys", data.get("rec_boxes", []))
        for text, score, box in zip(texts, scores, boxes):
            lines.append(
                OCRLine(
                    text=str(text),
                    confidence=float(score),
                    box=_normalize_box(box),
                )
            )
    return lines


def decode_image(content: bytes) -> np.ndarray:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            return np.asarray(image.convert("RGB"))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is not a valid image.",
        ) from exc


app = FastAPI(
    title="Hybrid RAG API",
    description="Docling ingestion, BGE-M3 + BM25 hybrid retrieval, RRF, and BGE reranking.",
    version="2.0.0",
)
app.include_router(structured_router)


@lru_cache(maxsize=1)
def get_rag_service() -> RAGService:
    return RAGService(get_settings())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/documents", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_document(file: UploadFile = File(...)) -> IngestResponse:
    settings = get_settings()
    content = await file.read(settings.max_document_bytes + 1)
    filename = file.filename or "document"
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded document is empty.")
    if len(content) > settings.max_document_bytes:
        raise HTTPException(status_code=413, detail="The document exceeds the upload limit.")
    try:
        document_id, count = await run_in_threadpool(get_rag_service().ingest, content, filename)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Document ingestion failed: {exc}") from exc
    return IngestResponse(document_id=document_id, filename=filename, chunks_indexed=count)


@app.post("/search", response_model=SearchResponse)
async def hybrid_search(request: SearchRequest) -> SearchResponse:
    try:
        results = await run_in_threadpool(
            get_rag_service().search, request.query, request.limit,
            request.candidate_limit, request.document_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}") from exc
    return SearchResponse(results=results)


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    service = get_rag_service()
    try:
        results = await run_in_threadpool(
            service.search, request.query, request.limit,
            request.candidate_limit, request.document_id,
        )
        return await run_in_threadpool(service.answer, request.query, results)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Question answering failed: {exc}") from exc


@app.delete("/documents/{document_id}", response_model=DeleteResponse)
async def delete_document(document_id: str) -> DeleteResponse:
    try:
        await run_in_threadpool(get_rag_service().delete_document, document_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Document deletion failed: {exc}") from exc
    return DeleteResponse(document_id=document_id, deleted=True)


@app.post("/ocr", response_model=OCRResponse)
async def ocr_image(
    file: UploadFile = File(...),
    index: bool = Query(default=False, description="Index extracted text in Qdrant for search and ask."),
) -> OCRResponse:
    filename = file.filename or "image"
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG, WebP, BMP, or TIFF image.",
        )

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded image is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"The image exceeds the {MAX_UPLOAD_BYTES}-byte upload limit.",
        )

    image = decode_image(content)
    try:
        lines = await run_in_threadpool(extract_lines, image)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("OCR processing failed")
        raise HTTPException(status_code=500, detail="OCR processing failed.") from exc

    text = "\n".join(line.text for line in lines)
    document_id = None
    chunks_indexed = 0
    if index:
        if not text.strip():
            raise HTTPException(status_code=422, detail="No text was extracted to index.")
        try:
            document_id, chunks_indexed = await run_in_threadpool(
                get_rag_service().ingest_text, text, filename
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("OCR text indexing failed")
            raise HTTPException(status_code=500, detail="OCR text indexing failed.") from exc
    return OCRResponse(
        text=text, lines=lines, document_id=document_id,
        filename=filename, chunks_indexed=chunks_indexed,
    )
