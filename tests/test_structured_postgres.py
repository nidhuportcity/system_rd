"""Opt-in real PostgreSQL tests; all DDL and rows roll back after each test."""
import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, Text, UniqueConstraint, create_engine, func, insert, inspect, select
from sqlalchemy.schema import AddConstraint

from app.config import Settings
from app.structured.models import DATA_SCHEMA, datasets
from app.structured.pipeline import InvalidFile
from app.structured.service import IngestionService


@pytest.fixture
def service():
    url = os.getenv("STRUCTURED_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set STRUCTURED_TEST_DATABASE_URL to run PostgreSQL integration tests.")
    engine = create_engine(url)
    with engine.connect() as connection:
        outer = connection.begin()

        class TransactionEngine:
            @contextmanager
            def begin(self):
                with connection.begin_nested():
                    yield connection

            @contextmanager
            def connect(self):
                yield connection

        try:
            yield IngestionService(TransactionEngine(), Settings()), connection
        finally:
            outer.rollback()
    engine.dispose()


def test_create_append_audit_and_constraint_rejections(service):
    ingestion, connection = service
    token = "test_" + uuid4().hex[:16]
    key = token + "_id"
    content = f"{key},amount\n001,10.5\n002,20.5\n".encode()
    first = ingestion.ingest(content, token + ".csv")
    assert first["action"] == "CREATE" and first["rows_inserted"] == 2
    target = Table(first["target_table"].split(".")[1], MetaData(), schema=DATA_SCHEMA, autoload_with=connection)
    assert connection.execute(select(target.c[key]).order_by(target.c[key])).scalars().all() == ["001", "002"]
    # Add a constraint inside the disposable transaction to exercise bad-batch isolation.
    connection.execute(AddConstraint(UniqueConstraint(target.c[key])))
    second = ingestion.ingest(f"{key},amount\n001,20.5\n003,30.5\n".encode(), "other.csv")
    assert second["action"] == "APPEND" and second["target_table"] == first["target_table"]
    assert second["rows_inserted"] == 1 and second["rows_rejected"] == 1
    assert connection.execute(select(func.count()).select_from(target)).scalar_one() == 3
    job = ingestion.job(second["ingestion_id"])
    assert job["status"] == "COMPLETED" and job["errors"][0]["stage"] == "INSERT"
    assert job["schema_snapshot"][0]["name"] == key


def test_ambiguous_matches_create_new_table(service):
    ingestion, connection = service
    token = "test_" + uuid4().hex[:16]
    content = f"{token}_id\n001\n".encode()
    first = ingestion.ingest(content, token + ".csv")
    other_name = token + "_other"
    Table(other_name, MetaData(), Column(token + "_id", Text), schema=DATA_SCHEMA).create(connection)
    from datetime import datetime, timezone
    connection.execute(insert(datasets).values(id=str(uuid4()), table_schema=DATA_SCHEMA, table_name=other_name, created_at=datetime.now(timezone.utc)))
    result = ingestion.ingest(content, token + ".csv")
    assert result["action"] == "CREATE" and result["target_table"] != first["target_table"]


def test_failed_parse_retains_failed_audit_record(service):
    ingestion, connection = service
    with pytest.raises(InvalidFile) as failure:
        ingestion.ingest(b"bad", "bad.xlsx")
    job = ingestion.job(failure.value.ingestion_id)
    assert job["status"] == "FAILED"
    assert job["errors"][0]["stage"] == "PARSE"


def test_row_rejections_and_incompatible_types(service):
    ingestion, _ = service
    token = "test_" + uuid4().hex[:16]
    content = f"{token},amount\n".encode() + b"Alice,12\n" * 19 + b"Bob,bad\n"
    first = ingestion.ingest(content, token + ".csv")
    assert first["rows_received"] == 20 and first["rows_rejected"] == 1
    assert first["rows_inserted"] == 19
    second = ingestion.ingest(f"{token},amount\nBob,12.5\n".encode(), token + ".csv")
    assert second["action"] == "CREATE" and first["target_table"] != second["target_table"]


def test_system_failure_rolls_back_data_and_ddl_but_keeps_audit(service, monkeypatch):
    ingestion, connection = service
    token = "test_" + uuid4().hex[:16]
    original = ingestion.select_target

    def fail_after_create(*args):
        original(*args)
        raise RuntimeError("Simulated failure after table creation")

    monkeypatch.setattr(ingestion, "select_target", fail_after_create)
    with pytest.raises(RuntimeError):
        ingestion.ingest(f"{token}_id\n001\n".encode(), token + ".csv")
    from app.structured.models import ingestion_jobs
    job = connection.execute(select(ingestion_jobs).where(ingestion_jobs.c.filename == token + ".csv")).mappings().one()
    assert job["status"] == "FAILED" and job["rows_inserted"] == 0
    assert not inspect(connection).has_table(token, schema=DATA_SCHEMA)
