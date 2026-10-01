"""Benchmark result schema, version 1 (RFC-0001 §6.2).

Results are JSON Lines, one record per measurement. Provenance fields (git SHA, date,
run ID) never affect comparability; compatibility fields decide whether two results may
be compared. `topology` is RFC-0003's structural identity, or null before fixtures land;
null is compatible only with null. Tools reject schema versions they do not know.
"""

import json
import math
from pathlib import Path

SUPPORTED = (1,)
PROVENANCE = ("git_sha", "date", "run_id")
COMPAT = ("gpu", "driver", "cuda", "nic", "topology", "build_level", "compiler", "deps")
REQUIRED = (
    "schema",
    "provenance",
    "compat",
    "bench",
    "params",
    "unit",
    "higher_is_better",
    "samples",
)


class SchemaError(ValueError):
    pass


def validate(record: dict) -> None:
    version = record.get("schema")
    if version not in SUPPORTED:
        raise SchemaError(
            f"schema version {version} is not supported by this tool "
            f"(supported: {', '.join(map(str, SUPPORTED))}); update ostia-dev bench or re-record"
        )
    for f in REQUIRED:
        if f not in record:
            raise SchemaError(f"missing field '{f}'")
    for f in PROVENANCE:
        if f not in record["provenance"]:
            raise SchemaError(f"missing provenance field '{f}'")
    for f in COMPAT:
        if f not in record["compat"]:
            raise SchemaError(f"missing compat field '{f}'")
    samples = record["samples"]
    if not isinstance(samples, list) or not samples:
        raise SchemaError("'samples' must be a non-empty list")
    if not all(isinstance(s, (int, float)) and math.isfinite(s) for s in samples):
        raise SchemaError("'samples' contains non-finite or non-numeric values")


def case_key(record: dict) -> tuple:
    """Which measurement a record is: benchmark name and parameters."""
    return (record["bench"], json.dumps(record["params"], sort_keys=True))


def compat_key(record: dict) -> tuple:
    """Records compare only when every compatibility field is equal."""
    return tuple((f, record["compat"].get(f)) for f in COMPAT)


def load(path: Path) -> list[dict]:
    """Every record of a JSON Lines file, validated."""
    records = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise SchemaError(f"{path}:{number}: malformed JSON: {e}") from e
        try:
            validate(record)
        except SchemaError as e:
            raise SchemaError(f"{path}:{number}: {e}") from e
        records.append(record)
    return records
