from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.structured.dump_restore import DumpCommandError, DumpFormatDetector, DumpValidator, PostgresRestoreService, SchemaDiscoveryService
from app.structured.pipeline import InvalidFile
from app.structured.router import get_dump_ingestion_service, router
from app.structured.dump_service import PostgresDumpIngestionService, SchemaRegistryService
from contextlib import contextmanager


def test_content_detection_ignores_suffix(tmp_path):
    path = tmp_path / "misnamed.sql"
    path.write_bytes(b"PGDMP" + bytes(30))
    assert DumpFormatDetector().detect(path) == "custom"
    path.write_bytes(b"-- PostgreSQL dump\nCREATE TABLE t(id integer);")
    assert DumpFormatDetector().detect(path) == "plain"
    path.write_bytes(b"PK\x00\xff")
    with pytest.raises(InvalidFile):
        DumpFormatDetector().detect(path)


def test_validator_limits_and_extensions(tmp_path):
    path = tmp_path / "dump"
    path.write_bytes(b"SELECT 1;")
    validator = DumpValidator()
    for filename, limit in [("x.csv", 100), ("x.sql", 1)]:
        with pytest.raises(InvalidFile):
            validator.validate(path, filename, limit)
    validator.validate(path, "x.BACKUP", 100)


def test_command_error_preserves_bounded_diagnostic_without_credentials_or_rows(monkeypatch):
    restore = PostgresRestoreService(Settings(), "test")

    def run(command, **kwargs):
        assert isinstance(command, list)
        kwargs["stderr"].write((
            f'pg_restore: error: authentication failed for password {restore.password}\n'
            'CONTEXT: COPY users, line 1: "private uploaded row"\n'
        ).encode())
        return Mock(returncode=1)

    monkeypatch.setattr("app.structured.dump_restore.subprocess.run", run)
    with pytest.raises(DumpCommandError) as failure:
        restore.run(["exec", restore.container, "pg_restore", "/tmp/upload.dump"])
    message = str(failure.value)
    assert "pg_restore failed (exit 1)" in message
    assert "authentication failed" in message
    assert restore.password not in message
    assert "private uploaded row" not in message
    assert "[REDACTED]" in message


@pytest.mark.parametrize("format,tool", [("plain", "psql"), ("custom", "pg_restore")])
def test_restore_isolation_and_command_arguments(tmp_path, format, tool):
    restore = PostgresRestoreService(Settings(), "abc")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "inspect":
            return b"true"
        if args[-1] == "/proc/1/comm":
            return b"postgres"
        return b""

    restore.run = run
    restore.restore(tmp_path / "upload", format)
    start = next(args for args, _ in calls if args[0] == "run")
    assert start[start.index("--network") + 1] == "none"
    assert "-p" not in start and "-v" not in start
    command = next(args for args, _ in calls if tool in args and "/tmp/upload.dump" in args)
    assert command[command.index("--user") + 1] == "nobody"
    assert command[command.index("-U") + 1] == "ingest"
    assert command[command.index("-d") + 1] == "staging"
    assert "--no-owner" in command if format == "custom" else "--single-transaction" in command
    cleanup = next(args for args, _ in calls if "rm" in args)
    assert cleanup[cleanup.index("--user") + 1] == "root"


def test_discovery_bounded_count_and_quoted_identifiers():
    restore = Mock()
    restore.settings = replace(Settings(), dump_exact_count_limit=5)
    restore.psql.side_effect = [
        b'[{"schema":"a","name":"x\\\"y","estimated_rows":-1,"columns":[],"constraints":[],"indexes":[]}]',
        b"6", b'["a"]',
    ]
    snapshot = SchemaDiscoveryService().discover(restore)
    assert snapshot["tables"][0]["row_count"] is None
    assert snapshot["tables"][0]["row_count_kind"] == "unknown"
    assert '"a"."x""y" LIMIT 6' in restore.psql.call_args_list[1].args[0]


def test_upload_route_streams_and_removes_temp_file():
    app = FastAPI()
    app.include_router(router)
    service = Mock()
    paths = []

    def ingest(path, filename):
        assert path.read_bytes() == b"SELECT 1;"
        assert filename == "company.backup"
        paths.append(path)
        return dict(status="COMPLETED", ingestion_id="id", source_file=filename,
                    dump_format="plain", schemas_discovered=1, tables_discovered=0,
                    relationships_discovered=0, tables=[])

    service.ingest.side_effect = ingest
    app.dependency_overrides[get_dump_ingestion_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/structured/ingest-dump", files={"file": ("company.backup", b"SELECT 1;")})
        assert response.status_code == 201
        assert response.json()["dump_format"] == "plain"
        assert client.post("/structured/ingest-dump", files={"file": ("x.sql", b"")}).status_code == 400
        assert client.post("/structured/ingest-dump", files={"file": ("x.zip", b"bad")}).status_code == 415
    assert not paths[0].exists()


def test_registry_maps_composite_relationships_without_name_collisions():
    connection = Mock()
    snapshot = {"tables": [
        dict(schema="a", name="same", columns=[dict(name="id", position=1,
             nullable=False, data_type="numeric(10,2)")], constraints=[dict(
                 kind="f", name="fk", columns=["x", "y"], target_columns=["id", "code"],
                 target_schema="b", target_table="same", definition="FOREIGN KEY ...")]),
        dict(schema="b", name="same", columns=[], constraints=[]),
    ]}
    assert SchemaRegistryService().register(connection, "job", "container", snapshot) == 1
    statements = [call.args[0] for call in connection.execute.call_args_list]
    dataset_values = [s.compile().params for s in statements if s.table.name == "datasets"]
    assert len({v["table_name"] for v in dataset_values}) == 2
    relationship = next(s.compile().params for s in statements if s.table.name == "table_relationships")
    assert relationship["source_columns"] == ["x", "y"]
    assert relationship["target_columns"] == ["id", "code"]
    assert relationship["source_dataset_id"] == dataset_values[0]["id"]
    assert relationship["target_dataset_id"] == dataset_values[1]["id"]


def test_restore_failure_retains_audit_and_removes_container(tmp_path, monkeypatch):
    connection = Mock()
    engine = Mock()

    @contextmanager
    def begin():
        yield connection

    engine.begin = begin
    monkeypatch.setattr("app.structured.dump_service.IngestionService.initialize", lambda self: None)
    restore = Mock()
    restore.restore.side_effect = InvalidFile("invalid archive")
    monkeypatch.setattr("app.structured.dump_service.PostgresRestoreService", lambda *args: restore)
    path = tmp_path / "dump"
    path.write_bytes(b"PGDMPbad")
    with pytest.raises(InvalidFile) as failure:
        PostgresDumpIngestionService(engine, Settings()).ingest(path, "test.dump")
    assert failure.value.ingestion_id
    restore.remove.assert_called_once()
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert any(s.compile().params.get("status") == "FAILED" for s in statements)
    assert any(s.table.name == "ingestion_errors" and s.compile().params["stage"] == "RESTORE" for s in statements)
    assert any(s.table.name == "ingestion_errors" and "invalid archive" in s.compile().params["message"] for s in statements)
