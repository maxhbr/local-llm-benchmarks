#!/usr/bin/env python3
"""Compile agent-bench histories and legacy scoreboards into ``manifest.json``.

``--root`` is the legacy ``_agent-bench-scoreboards`` directory. Its parent is
also scanned for ``<folder>/agent-bench/<run>/{meta,metrics}.json`` histories.
These histories identify the actual output folder, even when different models
share an API model name. They replace scoreboard rows only for the exact
``(run, raw_key)`` pair; different folders are always kept separately. Unmatched
legacy scoreboard rows remain available without a guessed output alias.

History entries add ``folder`` (the actual directory name), ``run_name`` (the
metadata alias, falling back to folder), and ``source_path`` (a safe path relative
to the benchmark root, pointing to metrics.json). Legacy entries have a
source_path pointing to scoreboard.json instead. Only model and run_name are
read from metadata; the raw sidecar, including credentials, is never published.
Missing or corrupt metadata is diagnosed in ``skipped`` but valid metrics are
kept with folder identity and an empty raw_key/model when the model is unknown.
Symlink aliases and unsafe path components are ignored.

The metrics come from three generations of the runner, so the schema varies.
This script preserves the existing normalized fields and adds history identity:

    {
      "generated_at": "2026-06-15T...",
      "runs": [
        {
          "run":          "20260613-014122",
          "ts":           "2026-06-13T01:41:22",
          "host":         "gfx1151",
          "model":        "Qwen3.6-35B-A3B-UD-Q3_K_XL",
          "raw_key":      "gfx1151:Qwen3.6-35B-A3B-UD-Q3_K_XL",
          "folder":       "gfx1151-Qwen3.6-35B-A3B-UD-Q3_K_XL",
          "run_name":     "gfx1151:Qwen3.6-35B-A3B-UD-Q3_K_XL",
          "source_path":  "gfx1151-Qwen3.6-35B-A3B-UD-Q3_K_XL/agent-bench/20260613-014122/metrics.json",
          "runs_n":       10,
          "turn_1_tps":   0.0,
          "turn_2_tps":   0.0,
          "json_valid_rate":      0.0,
          "tool_name_ok_rate":    0.0,
          "math_conversion_rate": 0.0,
          "final_success_rate":   0.0,
          "deterministic":        null,
          "errors":               ["api: Connection error.", ...],
          "error_count":          10,
          "schema":               "v3"
        },
        ...
      ]
    }

Usage:
    python3 scripts/generate-agent-bench-manifest.py
    python3 scripts/generate-agent-bench-manifest.py --root benchmarks/_agent-bench-scoreboards
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any


def parse_run_timestamp(name: str) -> str | None:
    """Run directory names look like ``20260613-014122``."""
    try:
        d = dt.datetime.strptime(name, "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return d.isoformat()


def split_host_model(key: str) -> tuple[str, str]:
    """Scoreboard keys are ``host:model`` but model strings may themselves
    contain colons (e.g. ``gfx1151:ROCm0:gemma-4-26B-A4B-it-qat-q4_0``).

    Heuristic: the first colon-segment is the host; the remainder is the model.
    """
    if ":" not in key:
        return "", key
    host, _, model = key.partition(":")
    return host, model


def _as_rate(v: Any) -> float | None:
    """Coerce booleans/numbers to a 0..1 rate. ``None`` if missing/garbage."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return None


def normalize_entry(run: str, key: str, raw: dict[str, Any]) -> dict[str, Any]:
    host, model = split_host_model(key)

    # Detect schema version.
    if not raw:
        schema = "empty"
    elif "final_success_rate" in raw:
        schema = "v3" if "temp0_final_response" in raw else "v2"
    elif "final_success" in raw:
        schema = "v1"
    else:
        schema = "unknown"

    errors_field = raw.get("errors")
    if errors_field is None and raw.get("error"):
        errors_field = [raw["error"]]
    errors = errors_field if isinstance(errors_field, list) else []

    return {
        "run":   run,
        "ts":    parse_run_timestamp(run),
        "host":  host,
        "model": model,
        "raw_key": key,
        "runs_n": raw.get("runs"),
        "turn_1_tps": raw.get("turn_1_tps"),
        "turn_2_tps": raw.get("turn_2_tps"),
        "json_valid_rate":      _as_rate(raw.get("json_valid_rate", raw.get("json_valid"))),
        "tool_name_ok_rate":    _as_rate(raw.get("tool_name_ok_rate")),
        "math_conversion_rate": _as_rate(
            raw.get("math_conversion_rate", raw.get("math_conversion_correct"))
        ),
        "final_success_rate":   _as_rate(
            raw.get("final_success_rate", raw.get("final_success"))
        ),
        "deterministic": raw.get("deterministic"),
        "errors": errors,
        "error_count": len(errors),
        "temp0_final_response": raw.get("temp0_final_response"),
        "schema": schema,
    }


def safe_component(name: str) -> bool:
    """Reject traversal, control characters, and URL/path separators."""
    return bool(name) and name not in (".", "..") and not any(
        char in "/\\:?#%" or ord(char) < 32 or ord(char) == 127 for char in name
    )


def build_manifest(root: str | Path) -> dict[str, Any]:
    # Do not resolve symlinks: aliases must not become extra histories.
    root = Path(os.path.abspath(root))
    benchmark_root = root.parent
    runs: list[dict[str, Any]] = []
    skipped: list[str] = []
    history_ids: set[tuple[str, str]] = set()

    def relative(path: Path) -> str:
        return path.relative_to(benchmark_root).as_posix()

    def directories(parent: Path):
        try:
            children = sorted(parent.iterdir())
        except OSError as error:
            skipped.append(f"{relative(parent)}: {error}")
            return
        for path in children:
            if path.is_symlink():
                skipped.append(f"{relative(path)}: symlink ignored")
            elif path.is_dir():
                if safe_component(path.name):
                    yield path
                else:
                    skipped.append(f"{relative(path)}: unsafe path component")

    def read_object(path: Path) -> dict[str, Any] | None:
        if path.is_symlink():
            skipped.append(f"{relative(path)}: symlink ignored")
            return None
        try:
            if not path.is_file():
                raise ValueError(f"missing regular {path.name}")
            with path.open(encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            skipped.append(f"{relative(path)}: {error}")
            return None

    for folder in directories(benchmark_root):
        if folder == root:
            continue
        bench = folder / "agent-bench"
        if bench.is_symlink():
            skipped.append(f"{relative(bench)}: symlink ignored")
            continue
        if not bench.is_dir():
            continue
        for run_dir in directories(bench):
            metrics_path = run_dir / "metrics.json"
            metrics = read_object(metrics_path)
            if metrics is None:
                continue
            meta_path = run_dir / "meta.json"
            meta = read_object(meta_path)
            key = meta.get("model") if meta is not None else None
            if not isinstance(key, str) or not key.strip():
                if meta is not None:
                    skipped.append(f"{relative(meta_path)}: missing or invalid model")
                key = ""
            run_name = meta.get("run_name") if meta is not None else None
            if not isinstance(run_name, str) or not run_name.strip():
                run_name = folder.name
            entry = normalize_entry(run_dir.name, key, metrics)
            entry.update(folder=folder.name, run_name=run_name,
                         source_path=relative(metrics_path))
            runs.append(entry)
            if key:
                history_ids.add((run_dir.name, key))

    if root.is_symlink() or not safe_component(root.name):
        skipped.append(f"{root.name}: symlink or unsafe scoreboard root ignored")
    else:
        for run_dir in directories(root):
            sb = run_dir / "scoreboard.json"
            data = read_object(sb)
            if data is None:
                continue
            if not data:
                # Keep the historical empty-run placeholder.
                entry = normalize_entry(run_dir.name, "", {})
                entry["source_path"] = relative(sb)
                runs.append(entry)
                continue
            for key, raw in data.items():
                if not isinstance(raw, dict):
                    skipped.append(f"{relative(sb)}: non-dict entry for {key}")
                    continue
                if (run_dir.name, key) in history_ids:
                    continue
                entry = normalize_entry(run_dir.name, key, raw)
                entry["source_path"] = relative(sb)
                runs.append(entry)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(runs),
        "skipped": skipped,
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "benchmarks",
            "_agent-bench-scoreboards",
        ),
        help="path to the _agent-bench-scoreboards directory",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="output path (default: <root>/manifest.json)",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    output = args.output or os.path.join(root, "manifest.json")
    manifest = build_manifest(root)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
        f.write("\n")

    print(f"wrote {output}: {manifest['count']} entries")
    if manifest["skipped"]:
        print(f"  skipped {len(manifest['skipped'])}:")
        for s in manifest["skipped"]:
            print(f"    {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
