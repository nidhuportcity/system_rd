from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    max_dump_bytes: int = int(os.getenv("MAX_DUMP_BYTES", str(100 * 1024 * 1024)))
    dump_timeout_seconds: int = int(os.getenv("DUMP_TIMEOUT_SECONDS", "600"))
    dump_postgres_image: str = os.getenv("DUMP_POSTGRES_IMAGE", "postgres:18.6")
    dump_memory: str = os.getenv("DUMP_MEMORY", "1g")
    dump_exact_count_limit: int = int(os.getenv("DUMP_EXACT_COUNT_LIMIT", "10000"))
    database_url: str | None = os.getenv("DATABASE_URL")
    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5433"))
    postgres_db: str = os.getenv("POSTGRES_DB", "rag")
    postgres_user: str = os.getenv("POSTGRES_USER", "rag")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "rag_dev_password")
    max_structured_bytes: int = int(os.getenv("MAX_STRUCTURED_BYTES", str(20 * 1024 * 1024)))
    max_structured_rows: int = int(os.getenv("MAX_STRUCTURED_ROWS", "100000"))
    max_structured_columns: int = int(os.getenv("MAX_STRUCTURED_COLUMNS", "200"))
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
