from __future__ import annotations

from pydantic import BaseModel, Field


class IngestResponse(BaseModel):
    document_id: str
    filename: str
    chunks_indexed: int


class SearchRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "major advantage of artificial intelligence",
                    "limit": 5,
                    "candidate_limit": 30,
                    "document_id": None,
                }
            ]
        }
    }

    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)
    candidate_limit: int = Field(default=30, ge=1, le=100)
    document_id: str | None = Field(
        default=None,
        description=(
            "Omit or use null to search all indexed documents. To search one document, "
            "use the document_id returned by POST /documents. This is an exact filter; "
            "a placeholder such as 'string' will return no results."
        ),
    )


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    filename: str
    text: str
    score: float
    chunk_index: int


class SearchResponse(BaseModel):
    results: list[SearchResult]


class AskRequest(SearchRequest):
    pass


class Citation(BaseModel):
    number: int
    document_id: str
    filename: str
    chunk_id: str
    text: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]


class DeleteResponse(BaseModel):
    document_id: str
    deleted: bool
