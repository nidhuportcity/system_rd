from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, JSON, MetaData, String, Table, Text,
)

METADATA_SCHEMA = "structured_ingestion"
DATA_SCHEMA = "structured_data"
metadata = MetaData(schema=METADATA_SCHEMA)

datasets = Table(
    "datasets", metadata,
    Column("id", String(36), primary_key=True),
    Column("table_schema", String(63), nullable=False),
    Column("table_name", String(63), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
dataset_columns = Table(
    "dataset_columns", metadata,
    Column("dataset_id", ForeignKey(datasets.c.id), primary_key=True),
    Column("name", String(63), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("original_name", Text, nullable=False),
    Column("data_type", String(32), nullable=False),
    Column("nullable", Integer, nullable=False),
)
ingestion_jobs = Table(
    "ingestion_jobs", metadata,
    Column("id", String(36), primary_key=True),
    Column("dataset_id", ForeignKey(datasets.c.id)),
    Column("filename", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("action", String(16)),
    Column("rows_received", Integer, nullable=False, default=0),
    Column("rows_inserted", Integer, nullable=False, default=0),
    Column("rows_rejected", Integer, nullable=False, default=0),
    Column("schema_snapshot", JSON),
    Column("profile", JSON),
    Column("message", Text),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)
ingestion_errors = Table(
    "ingestion_errors", metadata,
    Column("id", String(36), primary_key=True),
    Column("ingestion_id", ForeignKey(ingestion_jobs.c.id), nullable=False, index=True),
    Column("row_number", Integer),
    Column("column_name", String(63)),
    Column("stage", String(32), nullable=False),
    Column("message", Text, nullable=False),
)

# Separate tables keep existing installations compatible without ALTER TABLE.
dump_sources = Table(
    "dump_sources", metadata,
    Column("dataset_id", ForeignKey(datasets.c.id), primary_key=True),
    Column("ingestion_id", ForeignKey(ingestion_jobs.c.id), nullable=False, index=True),
    Column("container_name", String(63), nullable=False),
    Column("database_name", String(63), nullable=False),
    Column("source_schema", String(63), nullable=False),
    Column("source_table", String(63), nullable=False),
    Column("schema_metadata", JSON, nullable=False),
)

table_relationships = Table(
    "table_relationships", metadata,
    Column("id", String(36), primary_key=True),
    Column("ingestion_id", ForeignKey(ingestion_jobs.c.id), nullable=False, index=True),
    Column("source_dataset_id", ForeignKey(datasets.c.id), nullable=False),
    Column("target_dataset_id", ForeignKey(datasets.c.id), nullable=False),
    Column("constraint_name", Text, nullable=False),
    Column("source_columns", JSON, nullable=False),
    Column("target_columns", JSON, nullable=False),
    Column("definition", Text, nullable=False),
)
