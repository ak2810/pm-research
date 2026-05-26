"""JSONL.gz → Parquet conversion with strict schema enforcement.

Parse failures are counted, not aborted. Count goes into S3 object metadata.
Polars reads JSONL natively; we post-cast to strict schema.
"""
import gzip
import json
from pathlib import Path
from typing import Any

import polars as pl

from pm_research.logging import get_logger

log = get_logger(__name__)


def convert_file(
    src: Path,
    dst: Path,
    schema: dict[str, pl.PolarsDataType],
    *,
    compression: str = "zstd",
    compression_level: int = 6,
) -> int:
    """Convert a single .jsonl.gz file to Parquet.

    Returns number of parse failures (lines that couldn't be read).
    Raises on I/O errors.
    """
    rows: list[dict[str, Any]] = []
    failures = 0

    with gzip.open(src, "rt", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                failures += 1
                log.warning("jsonl_parse_error", src=str(src), line=lineno, error=str(exc))

    if not rows:
        log.info("jsonl_empty", src=str(src))
        return failures

    df = pl.DataFrame(rows, infer_schema_length=len(rows))
    df = _cast_schema(df, schema)

    df.write_parquet(
        dst,
        compression=compression,  # type: ignore[arg-type]
        compression_level=compression_level,
        use_pyarrow=True,
    )
    log.info("parquet_written", src=str(src), dst=str(dst), rows=len(df), failures=failures)
    return failures


def _cast_schema(
    df: pl.DataFrame, schema: dict[str, pl.PolarsDataType]
) -> pl.DataFrame:
    """Cast known columns to target types; drop columns not in schema; fill missing with null."""
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
