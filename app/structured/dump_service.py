from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert, update

from app.structured.dump_restore import (
    DumpFormatDetector, DumpValidator, PostgresRestoreService, SchemaDiscoveryService,
)
from app.structured.models import (
    dataset_columns, datasets, dump_sources, ingestion_errors, ingestion_jobs, table_relationships,
)
from app.structured.service import IngestionService
from app.structured.pipeline import InvalidFile
from app.structured.dump_restore import RestoreUnavailable


class SchemaRegistryService:
    def register(self, connection, job_id: str, container: str, snapshot: dict) -> int:
        ids = {(t["schema"], t["name"]): str(uuid4()) for t in snapshot["tables"]}
        for table in snapshot["tables"]:
            dataset_id = ids[table["schema"], table["name"]]
            # Existing datasets.table_name is globally unique, including CSV names.
            # Keep actual PostgreSQL names verbatim in dump_sources instead.
            connection.execute(insert(datasets).values(
                id=dataset_id, table_schema=table["schema"],
                table_name="dump_" + dataset_id.replace("-", ""),
                created_at=datetime.now(timezone.utc),
            ))
            for column in table["columns"]:
                connection.execute(insert(dataset_columns).values(
                    dataset_id=dataset_id, name=column["name"], original_name=column["name"],
                    position=column["position"], nullable=int(column["nullable"]),
                    data_type=column["data_type"] if len(column["data_type"]) <= 32 else "USER-DEFINED",
                ))
            # Preserve unlimited PostgreSQL type names and all constraint details.
            connection.execute(insert(dump_sources).values(
                dataset_id=dataset_id, ingestion_id=job_id, container_name=container,
                database_name="staging", source_schema=table["schema"],
                source_table=table["name"], schema_metadata=table,
            ))
        relationships = 0
        for table in snapshot["tables"]:
            for constraint in table["constraints"]:
                if constraint["kind"] != "f":
                    continue
                target = ids.get((constraint["target_schema"], constraint["target_table"]))
                if target is None:
                    raise ValueError("Foreign key target was not discovered.")
                connection.execute(insert(table_relationships).values(
                    id=str(uuid4()), ingestion_id=job_id,
                    source_dataset_id=ids[table["schema"], table["name"]],
                    target_dataset_id=target, constraint_name=constraint["name"],
                    source_columns=constraint["columns"], target_columns=constraint["target_columns"],
                    definition=constraint["definition"],
                ))
                relationships += 1
        return relationships


class PostgresDumpIngestionService:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.validator = DumpValidator()
        self.detector = DumpFormatDetector()
        self.discovery = SchemaDiscoveryService()
        self.registry = SchemaRegistryService()

    def ingest(self, path: Path, filename: str) -> dict:
        IngestionService(self.engine, self.settings).initialize()
        job_id = str(uuid4())
        restore = PostgresRestoreService(self.settings, job_id)
        stage = "VALIDATE"
        with self.engine.begin() as connection:
            connection.execute(insert(ingestion_jobs).values(
                id=job_id, filename=filename, status="RUNNING", action="RESTORE",
                started_at=datetime.now(timezone.utc),
            ))
        try:
            self.validator.validate(path, filename, self.settings.max_dump_bytes)
            dump_format = self.detector.detect(path)
            stage = "RESTORE"
            restore.restore(path, dump_format)
            stage = "DISCOVER"
            snapshot = self.discovery.discover(restore)
            stage = "REGISTER"
            with self.engine.begin() as connection:
                relationships = self.registry.register(connection, job_id, restore.container, snapshot)
                result = dict(
                    status="COMPLETED", ingestion_id=job_id, source_file=filename,
                    dump_format=dump_format, schemas_discovered=len(snapshot["schemas"]),
                    tables_discovered=len(snapshot["tables"]), relationships_discovered=relationships,
                    tables=[t["schema"] + "." + t["name"] for t in snapshot["tables"]],
                )
                connection.execute(update(ingestion_jobs).where(ingestion_jobs.c.id == job_id).values(
                    status="COMPLETED", schema_snapshot=snapshot,
                    profile=dict(result, container_name=restore.container, database_name="staging"),
                    finished_at=datetime.now(timezone.utc),
                ))
            return result
        except Exception as exc:
            restore.remove()
            message = f"Dump ingestion failed during {stage.lower()}."
            if isinstance(exc, (InvalidFile, RestoreUnavailable)):
                message += " " + str(exc)
            with self.engine.begin() as connection:
                connection.execute(update(ingestion_jobs).where(ingestion_jobs.c.id == job_id).values(
                    status="FAILED", message=message, finished_at=datetime.now(timezone.utc),
                ))
                connection.execute(insert(ingestion_errors).values(
                    id=str(uuid4()), ingestion_id=job_id, stage=stage, message=message,
                ))
            exc.ingestion_id = job_id
            raise
