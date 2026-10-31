from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Integer, MetaData, Numeric,
    SmallInteger, String, Table, Text, insert, inspect, select, text, update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.schema import CreateSchema

from app.config import Settings
from app.structured.models import (
    DATA_SCHEMA, METADATA_SCHEMA, dataset_columns, datasets, ingestion_errors,
    ingestion_jobs, metadata,
)
from app.structured.pipeline import InvalidFile, error, identifier, parse_file, prepare


SQL_TYPES = {
    "INTEGER": Integer, "BIGINT": BigInteger, "NUMERIC": Numeric,
    "BOOLEAN": Boolean, "DATE": Date, "TIMESTAMP": DateTime, "TEXT": Text,
}


def compatible(inferred: str, existing) -> bool:
    """Only lossless assignments are candidates. Constraints are checked on insert."""
    if inferred == "TEXT":
        return isinstance(existing, String)
    if inferred == "BOOLEAN":
        return isinstance(existing, Boolean)
    if inferred == "TIMESTAMP":
        return isinstance(existing, DateTime) and not existing.timezone
    if inferred == "DATE":
        return isinstance(existing, Date) and not isinstance(existing, DateTime)
    if inferred == "NUMERIC":
        return isinstance(existing, Numeric) and existing.precision is None and existing.scale is None
    if inferred in {"INTEGER", "BIGINT"}:
        if isinstance(existing, Numeric):
            return existing.precision is None and existing.scale is None
        if inferred == "BIGINT":
            return isinstance(existing, BigInteger)
        # SmallInteger subclasses Integer, so explicitly exclude it.
        return isinstance(existing, Integer) and not isinstance(existing, SmallInteger)
    return False


class IngestionService:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings

    def initialize(self) -> None:
        with self.engine.begin() as connection:
            # Serialize first-use DDL across workers; no startup database dependency.
            connection.execute(text("SELECT pg_advisory_xact_lock(735941201)"))
            connection.execute(CreateSchema(METADATA_SCHEMA, if_not_exists=True))
            connection.execute(CreateSchema(DATA_SCHEMA, if_not_exists=True))
            metadata.create_all(connection)

    def ingest(self, content: bytes, filename: str) -> dict:
        self.initialize()
        job_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(insert(ingestion_jobs).values(
                id=job_id, filename=filename, status="RUNNING", started_at=datetime.now(timezone.utc),
            ))
        parsed, schema = None, None
        try:
            parsed = parse_file(content, filename, self.settings.max_structured_rows, self.settings.max_structured_columns)
            schema, records, errors = prepare(parsed)
            with self.engine.begin() as connection:
                # Table selection and creation are atomic across concurrent ingestions.
                connection.execute(text("SELECT pg_advisory_xact_lock(735941202)"))
                target, dataset_id, action = None, None, "NONE"
                inserted = 0
                if records:
                    target, dataset_id, action = self.select_target(connection, schema, filename)
                    # Fast bulk path; a bad batch is rolled back before isolating bad rows.
                    for start in range(0, len(records), 1000):
                        batch = records[start:start + 1000]
                        try:
                            with connection.begin_nested():
                                connection.execute(insert(target), [row for _, row in batch])
                            inserted += len(batch)
                        except (IntegrityError, DataError):
                            for number, row in batch:
                                try:
                                    with connection.begin_nested():
                                        connection.execute(insert(target), row)
                                    inserted += 1
                                except (IntegrityError, DataError):
                                    errors.append(error(number, None, "INSERT", "Row violates a database constraint or target type."))
                rejected = len({item["row_number"] for item in errors if item["row_number"] is not None})
                result = {
                    "status": "COMPLETED" if inserted else "REJECTED", "ingestion_id": job_id,
                    "target_table": f"{DATA_SCHEMA}.{target.name}" if target is not None else None,
                    "action": action, "rows_received": parsed.received,
                    "rows_inserted": inserted, "rows_rejected": rejected,
                    "schema": schema, "errors": errors[:100], "error_count": len(errors),
                }
                connection.execute(update(ingestion_jobs).where(ingestion_jobs.c.id == job_id).values(
                    dataset_id=dataset_id, status=result["status"], action=action,
                    rows_received=parsed.received, rows_inserted=inserted, rows_rejected=rejected,
                    schema_snapshot=schema, profile={c["name"]: c["profile"] for c in schema},
                    finished_at=datetime.now(timezone.utc),
                ))
                if errors:
                    connection.execute(insert(ingestion_errors), [dict(id=str(uuid4()), ingestion_id=job_id, **item) for item in errors])
                return result
        except Exception as exc:
            # Separate transaction retains the failure even if DDL/data was rolled back.
            with self.engine.begin() as connection:
                message = str(exc) if isinstance(exc, InvalidFile) else "Ingestion failed; data transaction was rolled back."
                connection.execute(update(ingestion_jobs).where(ingestion_jobs.c.id == job_id).values(
                    status="FAILED", message=message,
                    rows_received=parsed.received if parsed is not None else 0,
                    schema_snapshot=schema,
                    finished_at=datetime.now(timezone.utc),
                ))
                connection.execute(insert(ingestion_errors).values(
                    id=str(uuid4()), ingestion_id=job_id,
                    **error(None, None, "PARSE" if isinstance(exc, InvalidFile) else "SYSTEM", message),
                ))
            if isinstance(exc, InvalidFile):
                exc.ingestion_id = job_id
            raise

    def select_target(self, connection, schema: list[dict], filename: str):
        inspector = inspect(connection)
        candidates = []
        # Restrict automatic append to registered datasets; public application tables
        # and audit tables must never become accidental targets.
        registered = connection.execute(select(datasets)).mappings().all()
        for dataset in registered:
            if dataset["table_schema"] != DATA_SCHEMA or not inspector.has_table(dataset["table_name"], schema=DATA_SCHEMA):
                continue
            columns = inspector.get_columns(dataset["table_name"], schema=DATA_SCHEMA)
            existing = {column["name"]: column for column in columns}
            if set(existing) != {column["name"] for column in schema}:
                continue
            if any(column.get("computed") or column.get("identity") for column in columns):
                continue
            if all(compatible(column["data_type"], existing[column["name"]]["type"]) for column in schema):
                candidates.append(dataset)
        # Multiple structurally identical datasets are ambiguous: create separately.
        if len(candidates) == 1:
            dataset = candidates[0]
            table = Table(dataset["table_name"], MetaData(), schema=DATA_SCHEMA, autoload_with=connection)
            return table, dataset["id"], "APPEND"

        name = identifier(Path(filename).stem)
        if inspector.has_table(name, schema=DATA_SCHEMA) or any(d["table_name"] == name for d in registered):
            name = name[:50] + "_" + uuid4().hex[:12]
        table = Table(name, MetaData(), *(Column(col["name"], SQL_TYPES[col["data_type"]](), nullable=True) for col in schema), schema=DATA_SCHEMA)
        table.create(connection)
        dataset_id = str(uuid4())
        connection.execute(insert(datasets).values(id=dataset_id, table_schema=DATA_SCHEMA, table_name=name, created_at=datetime.now(timezone.utc)))
        connection.execute(insert(dataset_columns), [
            dict(dataset_id=dataset_id, name=col["name"], position=index,
                 original_name=col["original_name"], data_type=col["data_type"], nullable=1)
            for index, col in enumerate(schema)
        ])
        return table, dataset_id, "CREATE"

    def job(self, job_id: str, offset: int = 0, limit: int = 100) -> dict | None:
        self.initialize()
        with self.engine.connect() as connection:
            job = connection.execute(select(ingestion_jobs).where(ingestion_jobs.c.id == job_id)).mappings().first()
            if job is None:
                return None
            errors = connection.execute(select(ingestion_errors).where(
                ingestion_errors.c.ingestion_id == job_id,
            ).order_by(ingestion_errors.c.row_number, ingestion_errors.c.id).offset(offset).limit(limit)).mappings().all()
            return dict(job, errors=[dict(item) for item in errors])
