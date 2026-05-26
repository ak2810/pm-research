"""JSONL.gz → Parquet conversion with strict schema enforcement.

Parse failures are counted, not aborted. Count goes into S3 object metadata.
Processes in chunks to avoid OOM on large files (4M+ rows).
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

_CHUNK_ROWS = 100_000  # rows per batch — ~50MB RAM per chunk


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
            # Align schema — cast to established schema to handle column order/type drift
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
