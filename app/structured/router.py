import logging
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import get_engine
from app.structured.pipeline import InvalidFile
from app.structured.service import IngestionService
from app.structured.dump_service import PostgresDumpIngestionService
from app.structured.dump_restore import RestoreUnavailable

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/structured", tags=["Structured ingestion"])


class IngestionResult(BaseModel):
    status: str
    ingestion_id: str
    target_table: str | None
    action: str
    rows_received: int
    rows_inserted: int
    rows_rejected: int
    inferred_schema: list[dict] = Field(alias="schema")
    errors: list[dict]
    error_count: int


def get_ingestion_service() -> IngestionService:
    return IngestionService(get_engine(), get_settings())


class DumpIngestionResult(BaseModel):
    status: str
    ingestion_id: str
    source_file: str
    dump_format: str
    schemas_discovered: int
    tables_discovered: int
    relationships_discovered: int
    tables: list[str]


def get_dump_ingestion_service() -> PostgresDumpIngestionService:
    return PostgresDumpIngestionService(get_engine(), get_settings())


@router.post("/ingest-dump", response_model=DumpIngestionResult, status_code=201)
async def ingest_dump(
    file: UploadFile = File(...),
    service: PostgresDumpIngestionService = Depends(get_dump_ingestion_service),
):
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    try:
        if Path(filename).suffix.lower() not in {".sql", ".dump", ".backup"}:
            raise HTTPException(415, "Upload a .sql, .dump or .backup file.")
        with tempfile.TemporaryDirectory(prefix="postgres_dump_") as directory:
            path = Path(directory) / "upload"
            size = 0
            with path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > get_settings().max_dump_bytes:
                        raise HTTPException(413, "The file exceeds MAX_DUMP_BYTES.")
                    await run_in_threadpool(output.write, chunk)
            if not size:
                raise HTTPException(400, "The uploaded file is empty.")
            return await run_in_threadpool(service.ingest, path, filename)
    except (InvalidFile, RestoreUnavailable) as exc:
        raise HTTPException(503 if isinstance(exc, RestoreUnavailable) else 422, {
            "message": str(exc), "ingestion_id": getattr(exc, "ingestion_id", None),
        }) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("PostgreSQL dump ingestion failed")
        raise HTTPException(500, {
            "message": "PostgreSQL dump ingestion failed. Check server logs.",
            "ingestion_id": getattr(exc, "ingestion_id", None),
        }) from exc
    finally:
        await file.close()


@router.post("/ingest", response_model=IngestionResult, status_code=201)
async def ingest(file: UploadFile = File(...), service: IngestionService = Depends(get_ingestion_service)):
    filename = file.filename or ""
    try:
        if Path(filename).suffix.lower() not in {".csv", ".xlsx"}:
            raise HTTPException(415, "Upload a .csv or .xlsx file.")
        content = await file.read(get_settings().max_structured_bytes + 1)
    finally:
        await file.close()
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(content) > get_settings().max_structured_bytes:
        raise HTTPException(413, "The file exceeds the structured upload limit.")
    try:
        return await run_in_threadpool(service.ingest, content, filename)
    except InvalidFile as exc:
        raise HTTPException(422, {"message": str(exc), "ingestion_id": getattr(exc, "ingestion_id", None)}) from exc
    except Exception as exc:
        logger.exception("Structured ingestion failed")
        raise HTTPException(500, "Structured ingestion failed. Check database availability and server logs.") from exc


@router.get("/ingestions/{ingestion_id}")
def get_ingestion(
    ingestion_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000),
    service: IngestionService = Depends(get_ingestion_service),
):
    job = service.job(str(ingestion_id), offset, limit)
    if job is None:
        raise HTTPException(404, "Ingestion job not found.")
    return job
