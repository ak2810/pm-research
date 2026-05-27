"""JSONL.gz → Parquet conversion with strict schema enforcement.

Parse failures are counted, not aborted. Count goes into S3 object metadata.
Two-pass streaming for inferred-schema feeds: pass 1 discovers unified schema
(no data held), pass 2 writes row groups. Memory bounded to one chunk at a time.
"""
import gzip
import json
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from pm_research.logging import get_logger

log = get_logger(__name__)

_CHUNK_ROWS = 50_000  # rows per batch — smaller = less RAM per chunk


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Stringify nested list/dict values so all columns are scalar (Parquet-safe)."""
    return {
        k: json.dumps(v) if isinstance(v, (list, dict)) else v
        for k, v in row.items()
    }


def convert_file(
    src: Path,
    dst: Path,
    schema: dict[str, pl.PolarsDataType],
    *,
    compression: str = "zstd",
    compression_level: int = 6,
) -> int:
    """Convert a single .jsonl.gz file to Parquet using chunked streaming.

    Returns number of parse failures (lines that couldn't be read).
    Raises on I/O errors.
    """
    if not schema:
        return _convert_inferred(src, dst, compression, compression_level)

    failures = 0
    total_rows = 0
    writer: pq.ParquetWriter | None = None
    pa_schema: pa.Schema | None = None

    chunk: list[dict[str, Any]] = []

    def _flush(rows: list[dict[str, Any]]) -> None:
        nonlocal writer, pa_schema, total_rows
        if not rows:
            return
        df = pl.DataFrame(rows, infer_schema_length=len(rows))
        df = _cast_schema(df, schema)
        table = df.to_arrow()
        if writer is None:
            pa_schema = table.schema
            writer = pq.ParquetWriter(
                str(dst),
                pa_schema,
                compression=compression,
                compression_level=compression_level,
            )
        else:
            table = table.cast(pa_schema)
        writer.write_table(table)
        total_rows += len(rows)

    with gzip.open(src, "rt", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                chunk.append(_normalize_row(json.loads(line)))
            except json.JSONDecodeError as exc:
                failures += 1
                log.warning("jsonl_parse_error", src=str(src), line=lineno, error=str(exc))
                continue

            if len(chunk) >= _CHUNK_ROWS:
                _flush(chunk)
                chunk = []

    _flush(chunk)  # final partial chunk

    if writer is not None:
        writer.close()
        log.info("parquet_written", src=str(src), dst=str(dst), rows=total_rows, failures=failures)
    else:
        log.info("jsonl_empty", src=str(src))

    return failures


def _convert_inferred(
    src: Path,
    dst: Path,
    compression: str,
    compression_level: int,
) -> int:
    """Two-pass streaming conversion for mixed-schema files.

    Pass 1: read in chunks, accumulate only Arrow schemas (data discarded immediately).
             Unify schemas so all columns from all event types are captured.
    Pass 2: open ParquetWriter with unified schema, stream rows → write row groups.
             Memory is bounded to one chunk (~_CHUNK_ROWS rows) at all times.
    """
    failures = 0

    # --- Pass 1: schema discovery (no data retained) ---
    schemas: list[pa.Schema] = []
    chunk: list[dict[str, Any]] = []

    with gzip.open(src, "rt", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                chunk.append(_normalize_row(json.loads(line)))
            except json.JSONDecodeError as exc:
                failures += 1
                log.warning("jsonl_parse_error", src=str(src), line=lineno, error=str(exc))
                continue
            if len(chunk) >= _CHUNK_ROWS:
                schemas.append(
                    pl.DataFrame(chunk, infer_schema_length=len(chunk)).to_arrow().schema
                )
                chunk = []

    if chunk:
        schemas.append(
            pl.DataFrame(chunk, infer_schema_length=len(chunk)).to_arrow().schema
        )
        chunk = []

    if not schemas:
        log.info("jsonl_empty", src=str(src))
        return failures

    try:
        unified_schema = pa.unify_schemas(schemas)
    except pa.ArrowInvalid:
        # Fall back: cast everything to string on type conflict
        all_names: dict[str, pa.DataType] = {}
        for s in schemas:
            for field in s:
                if field.name not in all_names:
                    all_names[field.name] = field.type
                elif all_names[field.name] != field.type:
                    all_names[field.name] = pa.large_utf8()
        unified_schema = pa.schema(
            [pa.field(n, t) for n, t in all_names.items()]
        )

    # --- Pass 2: streaming write ---
    total_rows = 0
    writer = pq.ParquetWriter(
        str(dst),
        unified_schema,
        compression=compression,
        compression_level=compression_level,
    )

    with gzip.open(src, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                chunk.append(_normalize_row(json.loads(line)))
            except json.JSONDecodeError:
                continue  # already counted in pass 1
            if len(chunk) >= _CHUNK_ROWS:
                writer.write_table(_cast_to_unified(chunk, unified_schema))
                total_rows += len(chunk)
                chunk = []

    if chunk:
        writer.write_table(_cast_to_unified(chunk, unified_schema))
        total_rows += len(chunk)

    writer.close()
    log.info("parquet_written", src=str(src), dst=str(dst), rows=total_rows, failures=failures)
    return failures


def _cast_to_unified(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    """Normalize a chunk to the unified schema: add null columns for missing fields,
    cast existing columns to their target types."""
    df = pl.DataFrame(rows, infer_schema_length=len(rows))
    tbl = df.to_arrow()
    arrays: list[pa.Array] = []
    for field in schema:
        if field.name in tbl.schema.names:
            col = tbl.column(field.name)
            if col.type != field.type:
                try:
                    col = col.cast(field.type, safe=False)
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                    col = pa.nulls(len(tbl), type=field.type)
        else:
            col = pa.nulls(len(tbl), type=field.type)
        arrays.append(col)
    return pa.table(
        {field.name: arrays[i] for i, field in enumerate(schema)},
        schema=schema,
    )


def _cast_schema(
    df: pl.DataFrame, schema: dict[str, pl.PolarsDataType]
) -> pl.DataFrame:
    """Cast known columns to target types; drop columns not in schema; fill missing with null.
    Empty schema → return df as-is (inferred types kept)."""
    if not schema:
        return df
    exprs: list[pl.Expr] = []
    for col, dtype in schema.items():
        if col in df.columns:
            if isinstance(dtype, pl.Decimal):
                # Cast via string → Decimal
                exprs.append(
                    pl.col(col).cast(pl.String).cast(dtype).alias(col)
                )
            else:
                exprs.append(pl.col(col).cast(dtype, strict=False).alias(col))
        else:
            exprs.append(pl.lit(None, dtype=dtype).alias(col))

    return df.select(exprs)
