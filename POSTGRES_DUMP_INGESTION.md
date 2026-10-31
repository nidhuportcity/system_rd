# PostgreSQL dump ingestion

`POST /structured/ingest-dump` accepts multipart `file` with `.sql`, `.dump`, or
`.backup` extensions. Content starting with `PGDMP` is restored with `pg_restore`;
UTF-8 text candidates use `psql`, regardless of extension. Other binary formats,
compressed/tar archives, directory dumps and cluster-wide dumps are unsupported.

```sh
docker pull postgres:18.6
curl -F "file=@company.dump" http://localhost:8000/structured/ingest-dump
```

The API host needs Docker CLI access to a Linux Docker engine. The existing
Compose API image does not include this access; run the API on a suitably
configured host/worker. Docker daemon access is privileged: keep this endpoint
behind your deployment's authentication and concurrency/rate limits. No Docker
socket mount or extra privilege is added to the existing Compose services.

Each ingestion starts a separate container, with no network, published ports,
host bind mounts, or application credentials. Memory, CPU and PID limits apply.
Both restore clients run as OS user `nobody`; SQL runs as a non-superuser role
with no role/database creation privileges. Local sockets use peer authentication,
so dump client commands cannot authenticate as the database administrator.
TCP password authentication is used only on the isolated container's loopback.
Dump code can modify its own staging database, never application tables.
Use a current patched PostgreSQL image; custom archives from newer major versions
and dumps requiring unavailable extensions can fail.

Custom restores ignore original ownership, ACLs and tablespaces. Plain SQL is
executed unchanged in a single transaction: dumps containing ownership changes,
CREATE DATABASE, reconnect commands, privileged SQL or unavailable roles may fail.
Generate portable plain dumps using `pg_dump --no-owner --no-acl` without `--create`.
Restore failures are atomic at registration time and remove the staging container
and its anonymous data volume. The upload is streamed to a temporary file and deleted
on completion. Oversized/empty/unsupported uploads fail before a job is created.

Completed containers intentionally remain as the registered data storage, including
across API restarts. Their names are recorded in `dump_sources` and the job profile.
Use `GET /structured/ingestions/{id}` for schema snapshots, locations and audit
errors. The table names in the response are schema-qualified. Metadata includes
schemas (including empty schemas), tables, full PostgreSQL types, nullability,
defaults, identities, generated columns, primary/unique/check/foreign constraints,
ordered composite FK columns, index definitions and row counts.

The existing datasets registry has globally unique table names and 32-character
column types. Dump datasets therefore use unique registry names; actual table
names and complete column/type metadata live in `dump_sources.schema_metadata`
and the audit snapshot, avoiding truncation or a breaking schema migration.
Columns also populate `dataset_columns`; type names longer than its existing
32-character limit use `USER-DEFINED` there, with the full type in the snapshot.
`table_relationships` links registered dataset IDs. `metadata.create_all` creates
these new tables lazily through the existing initialization lock.

Future SQL agents must resolve dump datasets through `dump_sources`, rather than
treating the datasets registry name as a table in the application's database.
There is no network SQL endpoint for these containers. An operator can inspect
one using `docker exec --user postgres <container> psql -U postgres -d staging`.
A future agent execution service should provide its own read-only credentials and
query limits. This change registers data; it does not add an SQL-agent executor.

Configuration (see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| MAX_DUMP_BYTES | 104857600 | Maximum uploaded bytes |
| DUMP_TIMEOUT_SECONDS | 600 | Total restore and discovery deadline |
| DUMP_POSTGRES_IMAGE | postgres:18.6 | Isolated server and client image |
| DUMP_MEMORY | 1g | Container memory limit |
| DUMP_EXACT_COUNT_LIMIT | 10000 | Exact counts up to this many rows; 0 disables |

Larger tables retain catalog estimates, or an explicitly unknown count when
statistics are absent. A bounded scan stops after limit + 1 rows. Disk space is
not bounded by the upload limit: provision a Docker storage quota for expanded
data. Completed containers need an operator retention/backup policy. To retire
a source, first remove its registry records/relationships and then use
`docker rm -f -v <container>`. Abrupt worker termination can leave RUNNING audits
and orphan containers; reconcile containers labeled `structured.dump=true`
against job profiles before deleting anything. This synchronous endpoint requires
proxy request timeouts compatible with the configured ingestion deadline.
