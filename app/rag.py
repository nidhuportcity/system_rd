from __future__ import annotations

import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.config import Settings
from app.schemas import AskResponse, Citation, SearchResult


class RAGService:
    """Docling -> BGE-M3/BM25 -> Qdrant RRF -> BGE reranker -> LLM."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._dense: Any = None
        self._sparse: Any = None
        self._reranker: Any = None
        self._converter: Any = None
        self._qdrant: Any = None

    @property
    def qdrant(self) -> Any:
        if self._qdrant is None:
            from qdrant_client import QdrantClient

            self._qdrant = QdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key,
                timeout=60,
            )
        return self._qdrant

    @property
    def dense(self) -> Any:
        if self._dense is None:
            from sentence_transformers import SentenceTransformer

            self._dense = SentenceTransformer(self.settings.dense_model)
        return self._dense

    @property
    def sparse(self) -> Any:
        if self._sparse is None:
            from fastembed import SparseTextEmbedding

            self._sparse = SparseTextEmbedding(model_name="Qdrant/bm25")
        return self._sparse

    @property
    def reranker(self) -> Any:
        if self._reranker is None:
            from sentence_transformers import CrossEncoder

            self._reranker = CrossEncoder(self.settings.reranker_model)
        return self._reranker

    @property
    def converter(self) -> Any:
        if self._converter is None:
            from docling.document_converter import DocumentConverter

            self._converter = DocumentConverter()
        return self._converter

    def ensure_collection(self) -> None:
        from qdrant_client import models

        if self.qdrant.collection_exists(self.settings.qdrant_collection):
            return
        self.qdrant.create_collection(
            collection_name=self.settings.qdrant_collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=self.settings.dense_dimension,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        self.qdrant.create_payload_index(
            collection_name=self.settings.qdrant_collection,
            field_name="document_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )

    def parse_document(self, content: bytes, filename: str) -> str:
        suffix = Path(filename).suffix or ".bin"
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(content)
                temp_path = handle.name
            result = self.converter.convert(temp_path)
            text = result.document.export_to_markdown().strip()
            if not text:
                raise ValueError("Docling extracted no text")
            return text
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    def chunk_text(self, text: str) -> list[str]:
        size = self.settings.chunk_size
        overlap = min(self.settings.chunk_overlap, size - 1)
        text = re.sub(r"[ \t]+", " ", text).strip()
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                boundary = max(text.rfind("\n", start + size // 2, end),
                               text.rfind(" ", start + size // 2, end))
                if boundary > start:
                    end = boundary
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
        return chunks

    def ingest(self, content: bytes, filename: str) -> tuple[str, int]:
        return self.ingest_text(self.parse_document(content, filename), filename)

    def ingest_text(self, text: str, filename: str) -> tuple[str, int]:
        """Index extracted text using the same vectors and payload as documents."""
        from qdrant_client import models

        chunks = self.chunk_text(text)
        if not chunks:
            raise HTTPException(status_code=422, detail="No text chunks could be created.")
        self.ensure_collection()
        dense_vectors = self.dense.encode(
            chunks, normalize_embeddings=True, show_progress_bar=False
        ).tolist()
        sparse_vectors = list(self.sparse.embed(chunks))
        document_id = str(uuid.uuid4())
        points = []
        for index, (chunk, dense, sparse) in enumerate(
            zip(chunks, dense_vectors, sparse_vectors, strict=True)
        ):
            chunk_id = str(uuid.uuid5(uuid.UUID(document_id), str(index)))
            points.append(
                models.PointStruct(
                    id=chunk_id,
                    vector={
                        "dense": dense,
                        "bm25": models.SparseVector(
                            indices=sparse.indices.tolist(), values=sparse.values.tolist()
                        ),
                    },
                    payload={
                        "document_id": document_id,
                        "filename": filename,
                        "chunk_index": index,
                        "text": chunk,
                    },
                )
            )
        self.qdrant.upsert(
            collection_name=self.settings.qdrant_collection, points=points, wait=True
        )
        return document_id, len(points)

    def search(
        self, query: str, limit: int, candidate_limit: int, document_id: str | None = None
    ) -> list[SearchResult]:
        from qdrant_client import models

        self.ensure_collection()
        dense_query = self.dense.encode(query, normalize_embeddings=True).tolist()
        sparse_query = next(iter(self.sparse.query_embed(query)))
        query_filter = None
        if document_id:
            query_filter = models.Filter(
                must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
            )
        response = self.qdrant.query_points(
            collection_name=self.settings.qdrant_collection,
            prefetch=[
                models.Prefetch(query=dense_query, using="dense", limit=candidate_limit, filter=query_filter),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse_query.indices.tolist(), values=sparse_query.values.tolist()
                    ),
                    using="bm25",
                    limit=candidate_limit,
                    filter=query_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=candidate_limit,
            with_payload=True,
        )
        candidates = response.points
        if not candidates:
            return []
        rerank_scores = self.reranker.predict(
            [(query, str(point.payload.get("text", ""))) for point in candidates],
            show_progress_bar=False,
        )
        ranked = sorted(zip(candidates, rerank_scores), key=lambda item: float(item[1]), reverse=True)
        return [
            SearchResult(
                chunk_id=str(point.id),
                document_id=str((point.payload or {}).get("document_id", "")),
                filename=str((point.payload or {}).get("filename", "")),
                text=str((point.payload or {}).get("text", "")),
                score=float(score),
                chunk_index=int((point.payload or {}).get("chunk_index", 0)),
            )
            for point, score in ranked[:limit]
        ]

    def answer(self, query: str, results: list[SearchResult]) -> AskResponse:
        if not results:
            return AskResponse(answer="I could not find relevant information in the indexed documents.", citations=[])
        from ollama import Client

        context = "\n\n".join(
            f"[{number}] {item.filename}\n{item.text}"
            for number, item in enumerate(results, start=1)
        )
        prompt = (
            "Answer using only the context. Cite support with [1], [2], etc. If the "
            "context lacks the answer, say so.\n\n"
            f"Context:\n{context}\n\nQuestion: {query}"
        )
        client = Client(host=self.settings.ollama_host, timeout=300)
        completion = client.chat(
            model=self.settings.llm_model,
            messages=[
                {"role": "system", "content": "You are a precise, grounded document assistant."},
                {"role": "user", "content": prompt},
            ],
            think=False,
            options={"num_predict": 800, "temperature": 0.1},
        )
        answer = completion.message.content or "No answer was generated."
        citations = [
            Citation(number=n, document_id=item.document_id, filename=item.filename,
                     chunk_id=item.chunk_id, text=item.text)
            for n, item in enumerate(results, start=1)
        ]
        return AskResponse(answer=answer, citations=citations)

    def delete_document(self, document_id: str) -> None:
        from qdrant_client import models

        self.ensure_collection()
        self.qdrant.delete(
            collection_name=self.settings.qdrant_collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
                )
            ),
            wait=True,
        )
