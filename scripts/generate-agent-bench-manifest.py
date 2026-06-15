#!/usr/bin/env python3
"""Compile every ``_agent-bench-scoreboards/<run>/scoreboard.json`` into a single
``manifest.json`` that the dashboard (``benchmarks/_agent-bench-scoreboards/index.html``)
loads with one fetch.

The scoreboard files come from three generations of the benchmark runner, so the
schema varies. This script normalizes everything to:

    {
      "generated_at": "2026-06-15T...",
      "runs": [
        {
          "run":          "20260613-014122",
          "ts":           "2026-06-13T01:41:22",
          "host":         "gfx1151",
          "model":        "Qwen3.6-35B-A3B-UD-Q3_K_XL",
          "raw_key":      "gfx1151:Qwen3.6-35B-A3B-UD-Q3_K_XL",
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


def build_manifest(root: str) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    skipped: list[str] = []

    for name in sorted(os.listdir(root)):
        run_dir = os.path.join(root, name)
        if not os.path.isdir(run_dir):
            continue
        sb = os.path.join(run_dir, "scoreboard.json")
        if not os.path.isfile(sb):
            skipped.append(f"{name}: no scoreboard.json")
            continue
        try:
            with open(sb, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{name}: {e}")
            continue
        if not isinstance(data, dict) or not data:
            # An empty {} dict — record the run but flag it.
            runs.append({
                "run":   name,
                "ts":    parse_run_timestamp(name),
                "host":  "",
                "model": "",
                "raw_key": "",
                "schema": "empty",
                "errors": [],
                "error_count": 0,
            })
            continue
        for key, raw in data.items():
            if not isinstance(raw, dict):
                skipped.append(f"{name}: non-dict entry for {key}")
                continue
            runs.append(normalize_entry(name, key, raw))

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
