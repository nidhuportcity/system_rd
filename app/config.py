from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_api_key: str | None = os.getenv("QDRANT_API_KEY")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "rag_documents")
    dense_model: str = os.getenv("DENSE_MODEL", "BAAI/bge-m3")
    reranker_model: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    dense_dimension: int = int(os.getenv("DENSE_DIMENSION", "1024"))
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    llm_model: str = os.getenv("LLM_MODEL", "qwen3:8b")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "1200"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "200"))
    max_document_bytes: int = int(os.getenv("MAX_DOCUMENT_BYTES", str(50 * 1024 * 1024)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
