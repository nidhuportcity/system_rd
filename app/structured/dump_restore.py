"""Untrusted dump execution occurs only inside a per-ingestion container."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import secrets
import subprocess
import time

from app.config import Settings
from app.structured.pipeline import InvalidFile

logger = logging.getLogger(__name__)


class RestoreUnavailable(RuntimeError):
    pass


class DumpCommandError(InvalidFile):
    """Bounded, credential-redacted diagnostic from a failed external command."""

    def __init__(self, operation: str, returncode: int, diagnostic: str):
        self.operation = operation
        self.returncode = returncode
        self.diagnostic = diagnostic
        super().__init__(f"{operation} failed (exit {returncode}): {diagnostic}")


class DumpFormatDetector:
    def detect(self, path: Path) -> str:
        with path.open("rb") as stream:
            header = stream.read(8192)
        if header.startswith(b"PGDMP"):
            return "custom"
        try:
            decoded = header.decode("utf-8-sig")
        except UnicodeError as exc:
            raise InvalidFile("Expected a PostgreSQL custom archive or UTF-8 SQL dump.") from exc
        if b"\x00" in header or not decoded.strip():
            raise InvalidFile("Unsupported or empty dump content.")
        # This identifies text candidates; PostgreSQL validates the SQL during restore.
        return "plain"


class DumpValidator:
    def validate(self, path: Path, filename: str, limit: int) -> None:
        if Path(filename).suffix.lower() not in {".sql", ".dump", ".backup"}:
            raise InvalidFile("Upload a .sql, .dump or .backup file.")
        size = path.stat().st_size
        if not size or size > limit:
            raise InvalidFile("Dump is empty or exceeds MAX_DUMP_BYTES.")


class PostgresRestoreService:
    def __init__(self, settings: Settings, ingestion_id: str):
        self.settings = settings
        self.container = "dump_" + ingestion_id.replace("-", "")
        self.password = secrets.token_hex(32)
        self.admin_password = secrets.token_hex(32)
        self.deadline = time.monotonic() + settings.dump_timeout_seconds

    def run(self, args: list[str], *, data: bytes | None = None) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise InvalidFile("Dump restore/discovery exceeded its time limit.")
        try:
            # Spool output to disk to avoid unbounded capture_output allocations.
            import tempfile
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run(
                    ["docker", *args], input=data, stdout=output,
                    stderr=errors, timeout=remaining, check=False,
                )
                if result.returncode:
                    errors.seek(0)
                    diagnostic = errors.read(8192).decode("utf-8", errors="replace")
                    for secret in (self.password, self.admin_password):
                        diagnostic = diagnostic.replace(secret, "[REDACTED]")
                    diagnostic = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", diagnostic).strip()
                    # SQL context/COPY records can contain uploaded private data.
                    # Return the first error line, not SQL statements or row values.
                    lines = diagnostic.splitlines()
                    first_error = next((line for line in lines if "error:" in line.lower() or "fatal:" in line.lower()), None)
                    diagnostic = (first_error or (lines[0] if lines else "No diagnostic output."))[:2000]
                    operation = next((tool for tool in ("pg_restore", "psql", "pg_isready") if tool in args), "docker " + args[0])
                    raise DumpCommandError(operation, result.returncode, diagnostic)
                output.seek(0)
                content = output.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise InvalidFile("Schema discovery output exceeds 16 MiB.")
                return content
        except FileNotFoundError as exc:
            raise RestoreUnavailable("Docker CLI is required on the API host.") from exc
        except subprocess.TimeoutExpired as exc:
            raise InvalidFile("Dump restore/discovery exceeded its time limit.") from exc

    def psql(self, sql: str, role: str = "ingest") -> bytes:
        return self.run([
            "exec", "-i", *self.client_identity(role), self.container,
            "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", role, "-d", "staging",
            *([] if role == "postgres" else ["-h", "127.0.0.1"]),
        ], data=sql.encode())

    def client_identity(self, role: str = "ingest") -> list[str]:
        if role == "postgres":
            return ["--user", "postgres"]
        return ["--user", "nobody", "--env", "PGPASSWORD=" + self.password]

    def restore(self, path: Path, dump_format: str) -> None:
        try:
            self.run(["version", "--format", "{{.Server.Version}}"])
        except InvalidFile as exc:
            raise RestoreUnavailable("Docker daemon is unavailable to the API host.") from exc
        self.run([
            "run", "-d", "--name", self.container, "--network", "none",
            "--label", "structured.dump=true", "--memory", self.settings.dump_memory,
            "--cpus", "1", "--pids-limit", "128", "--security-opt", "no-new-privileges",
            "-e", "POSTGRES_PASSWORD=" + self.admin_password,
            "-e", "POSTGRES_INITDB_ARGS=--auth-local=peer --auth-host=scram-sha-256",
            "-e", "POSTGRES_DB=staging", self.settings.dump_postgres_image,
        ])
        # pg_isready can succeed during the image's temporary initialization server;
        # require the final server (PID 1) before provisioning the role.
        while True:
            ready = self.run(["inspect", "--format", "{{.State.Running}}", self.container]).strip()
            if ready != b"true":
                raise RestoreUnavailable("Staging PostgreSQL container exited during startup.")
            try:
                self.run(["exec", "--user", "postgres", self.container, "pg_isready", "-U", "postgres", "-d", "staging"])
                command = self.run(["exec", self.container, "cat", "/proc/1/comm"]).strip()
                if command == b"postgres":
                    break
            except InvalidFile:
                if time.monotonic() >= self.deadline:
                    raise
            time.sleep(0.25)
        self.psql(
            "CREATE ROLE ingest LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '" + self.password + "'; "
            "ALTER DATABASE staging OWNER TO ingest; "
            "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC; "
            "REVOKE CONNECT ON DATABASE template1 FROM PUBLIC; "
            "ALTER SCHEMA public OWNER TO ingest;", role="postgres",
        )
        self.run(["cp", str(path.resolve()), self.container + ":/tmp/upload.dump"])
        self.run(["exec", "--user", "root", self.container, "chmod", "0644", "/tmp/upload.dump"])
        if dump_format == "custom":
            self.run([
                "exec", *self.client_identity(), self.container, "pg_restore",
                "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl",
                "--no-tablespaces", "-h", "127.0.0.1", "-U", "ingest", "-d", "staging", "/tmp/upload.dump",
            ])
        else:
            # psql runs in the sandbox too: SQL dumps may contain client commands.
            self.run([
                "exec", *self.client_identity(), self.container, "psql", "-X", "-q",
                "--single-transaction", "-v", "ON_ERROR_STOP=1", "-U", "ingest",
                "-h", "127.0.0.1", "-d", "staging", "-f", "/tmp/upload.dump",
            ])
        # docker cp creates a root-owned file. /tmp's sticky bit prevents the
        # postgres OS account from removing it even after a successful restore.
        self.run(["exec", "--user", "root", self.container, "rm", "-f", "/tmp/upload.dump"])

    def remove(self) -> None:
        try:
            subprocess.run(["docker", "rm", "-f", "-v", self.container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=True)
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not remove staging container %s", self.container)


class SchemaDiscoveryService:
    def discover(self, restore: PostgresRestoreService) -> dict:
        tables = json.loads(restore.psql(DISCOVERY_SQL))
        for table in tables:
            estimate = table["estimated_rows"]
            table["row_count"] = None
            table["row_count_kind"] = "estimate" if estimate >= 0 else "unknown"
            # Unknown statistics after restore are common. A bounded scan determines
            # whether exact counting is practical, without COUNT(*) on huge tables.
            limit = restore.settings.dump_exact_count_limit
            if limit > 0:
                quote = lambda name: '"' + name.replace('"', '""') + '"'
                name = quote(table["schema"]) + "." + quote(table["name"])
                count = int(restore.psql(
                    f"SELECT count(*) FROM (SELECT 1 FROM {name} LIMIT {limit + 1}) AS bounded;"
                ))
                if count <= limit:
                    table.update(row_count=count, row_count_kind="exact")
        schemas = json.loads(restore.psql("""
            SELECT coalesce(json_agg(nspname ORDER BY nspname), '[]')
            FROM pg_namespace WHERE nspname <> 'information_schema' AND nspname !~ '^pg_';
        """))
        return {"schemas": schemas, "tables": tables}


DISCOVERY_SQL = """
SELECT coalesce(json_agg(t ORDER BY t.schema, t.name), '[]') FROM (
 SELECT n.nspname AS schema, c.relname AS name, c.reltuples::bigint AS estimated_rows,
   (SELECT coalesce(json_agg(col ORDER BY col.position), '[]') FROM (
     SELECT a.attname AS name, a.attnum AS position,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default_expression,
       a.attidentity AS identity, a.attgenerated AS generated
     FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
     WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
   ) col) AS columns,
   (SELECT coalesce(json_agg(con ORDER BY con.name), '[]') FROM (
     SELECT k.conname AS name, k.contype AS kind,
       pg_get_constraintdef(k.oid) AS definition,
       ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY x(num,pos)
         JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.num ORDER BY x.pos) AS columns,
       rn.nspname AS target_schema, rc.relname AS target_table,
       ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY x(num,pos)
         JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.num ORDER BY x.pos) AS target_columns
     FROM pg_constraint k LEFT JOIN pg_class rc ON rc.oid=k.confrelid
     LEFT JOIN pg_namespace rn ON rn.oid=rc.relnamespace WHERE k.conrelid=c.oid
   ) con) AS constraints,
   (SELECT coalesce(json_agg(idx ORDER BY idx.name), '[]') FROM (
     SELECT ic.relname AS name, i.indisunique AS unique, i.indisprimary AS primary,
       i.indisvalid AS valid, pg_get_indexdef(i.indexrelid) AS definition
     FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid WHERE i.indrelid=c.oid
   ) idx) AS indexes
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p') AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
) t;
"""
