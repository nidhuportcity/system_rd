# CSV and Excel ingestion

Install the updated `requirements.txt`, start PostgreSQL with
`docker compose up -d postgres`, then run the existing FastAPI app:

```powershell
.venv1/Scripts/python.exe -m pip install -r requirements.txt
.venv1/Scripts/python.exe -m uvicorn app.main:app --reload
curl.exe -X POST http://localhost:8000/structured/ingest -F "file=@employees.csv"
```

The endpoint is also available in `/docs`. It returns the ingestion ID, target
table, CREATE/APPEND action, received/inserted/rejected counts, inferred schema,
per-column profiles, and up to 100 row errors. `COMPLETED` can include rejected
rows; `REJECTED` means no rows were inserted. An entirely invalid file returns
422 with its failed ingestion ID. Empty, unsupported, or oversized uploads are
rejected before a job starts. Infrastructure failures return 500; details are
logged on the server, with a FAILED audit record when the database is available.

Retrieve a saved job and its paginated errors:

```text
GET /structured/ingestions/{ingestion_id}?offset=0&limit=100
```

## Configuration

Local defaults use PostgreSQL at `localhost:5433`, database/user `rag`, and the
development password from Compose. Configure `POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, or override all with
`DATABASE_URL=postgresql+psycopg://...`. Export these variables before starting
the app; the existing configuration reads the process environment, not `.env`
directly. Compose reads `.env` and passes credentials and the internal database
address (`postgres:5432`) to the API container. Use
`docker compose --profile full up -d --build` for the containerized API.

Defaults: 20 MiB upload, 100,000 rows, 200 columns, and 200 MiB total expanded
XLSX content. Set `MAX_STRUCTURED_BYTES`, `MAX_STRUCTURED_ROWS`, and
`MAX_STRUCTURED_COLUMNS` to adjust the first three limits. Ingestion is
synchronous and runs in FastAPI's threadpool; files are processed in memory
within these limits.

## Deterministic pipeline and policies

- UTF-8 CSV uses a header row and comma delimiter. XLSX uses the first worksheet
  and its first row as the header. CSV values stay lexical until inference.
  Excel formulas and error cells reject their rows; formulas are never evaluated.
- Headers become ASCII snake_case, capped at PostgreSQL's 63-character limit.
  Blank/colliding headers get deterministic names/suffixes. SQLAlchemy quotes
  identifiers and binds values; filenames are never interpolated into SQL.
- Structural validation rejects CSV rows of the wrong width. Strings are trimmed
  and blanks become NULL. Literal strings such as `NA` and `null` remain text.
  Nulls are allowed because requiredness cannot be inferred from a sample alone.
- ID columns (`id`, `*_id`), phone/postal columns, and numeric strings with leading
  zeros remain TEXT. Excel numeric cells with a simple `000` format retain their
  displayed padding. Zeros already lost in an unformatted numeric Excel cell
  cannot be reconstructed.
- Inference uses BOOLEAN, INTEGER, BIGINT, arbitrary-precision NUMERIC, ISO DATE,
  naive ISO TIMESTAMP, or TEXT. At least 95% of non-null values must fit a type;
  minority conversion failures reject rows. Valid large/fractional numbers widen
  numeric types. More heterogeneous columns fall back to TEXT. Timezone-aware
  timestamps stay TEXT rather than silently changing their timezone.
- Profiles describe trimmed input: null counts, distinct non-null counts, five
  sample values, and numeric min/max/mean. Pandera validates lexical input against
  inferred types before cleaning, then re-validates converted Python objects.
  Decimal values avoid pandas floating-point coercion. No LLM is used;
  DuckDB is unnecessary for this bounded, deterministic inference pipeline.
- Data tables live in `structured_data`. Metadata tables `datasets`,
  `dataset_columns`, `ingestion_jobs`, and `ingestion_errors` live in
  `structured_ingestion`. They are created lazily on first ingestion. The database
  user needs permission to create schemas/tables.
- Matching considers only registered ingestion tables. It requires identical
  normalized column sets and lossless type compatibility. A unique match appends;
  no match or multiple matches creates a new table named from the filename, with
  a random suffix on collision. Existing public/application tables are excluded.
  There is no fuzzy matching, ALTER, DROP, automatic upsert, or deduplication.
- Bulk inserts use batches of 1,000. Constraint/data errors roll back a batch to
  a savepoint and retry rows individually. Other database failures roll back the
  whole data transaction. Successful table creation, inserts, schema metadata,
  errors, and final audit status commit together. A separate transaction records
  failures. Advisory locks serialize matching/creation across API workers.
- Retries are separate ingestions and may append duplicate rows unless the target
  has a uniqueness constraint. A process crash can leave a RUNNING audit record.
  Profiles include sample values; apply the same access controls as the data.

## Tests

```powershell
.venv1/Scripts/python.exe -m pytest -q
$env:STRUCTURED_TEST_DATABASE_URL = 'postgresql+psycopg://rag:rag_dev_password@localhost:5433/rag'
.venv1/Scripts/python.exe -m pytest tests/test_structured_postgres.py -q
```

PostgreSQL tests use an outer transaction: all test DDL, constraints, audit records,
and rows roll back. They do not drop any existing tables.
