#!/usr/bin/env python3
"""Compile every ``<folder>/tool-eval-bench/<timestamp>/result.json`` report into
a single ``manifest.json`` that the dashboard (``benchmarks/index.html``) loads
with one fetch.

Each run directory contains:

    meta.json     # run parameters (host, model, run_name, timestamp, ...)
    result.json   # machine-readable report (the ONLY result source; produced by
                  #               the driver via --json-file — see
                  #               tool-eval-bench-benchmarks.sh)

This script reads ``result.json`` (structured, authoritative) together with the
``meta.json`` sidecar.  The Markdown ``result.md`` is intentionally NOT read —
it is a human-readable artifact only.

Note: tool-eval-bench's result.json does not carry the per-scenario difficulty
tier (the ★ column from the Markdown report), so the manifest omits difficulty
data.  If that is needed, ask tool-eval-bench to emit it in JSON.

Normalized output (see ``build_run`` for the full shape):

    {
      "generated_at": "...",
      "count": N,
      "skipped": [...],
      "runs": [ { run, folder, ts, host, model, report, meta }, ... ]
    }

Usage:
    python3 scripts/generate-tool-eval-manifest.py
    python3 scripts/generate-tool-eval-manifest.py --root benchmarks
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_run_timestamp(name: str) -> str | None:
    """Run directory names look like ``20260825-152901``."""
    try:
        d = dt.datetime.strptime(name, "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return d.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# result.json parsing
# ---------------------------------------------------------------------------

# Every scenario in this benchmark is worth 2 points (69 scenarios × 2 = 138
# max_points), and result.json does not carry a per-scenario max — hence the
# constant.  Verified against scores.max_points / total_scenarios.
POINTS_PER_SCENARIO = 2

# Denominator for ``comparable_score``: the full suite's maximum points.  A run
# whose inference server died mid-suite has its ``scores.max_points`` shrink to
# only the scenarios that ran (e.g. 36 for 18/69), so the raw ``final_score``
# (total/max) rewards *not running* checks.  ``comparable_score`` divides
# total_points by this fixed full-suite max so every run is ranked on the same
# scale regardless of how many scenarios actually executed.
FULL_SUITE_MAX_POINTS = 138


def _title_from_raw_log(raw_log: str) -> str | None:
    """result.json scenario entries have no ``title`` field, but the first line
    of ``raw_log`` is ``scenario=TC-01 <Title>``.  Strip the leading id so the
    title matches the Markdown table (e.g. "Direct Specialist Match")."""
    for line in raw_log.splitlines():
        if line.startswith("scenario="):
            title = line[len("scenario="):].strip()
            return re.sub(r"^TC-\d+\s+", "", title)
    return None


def _parse_scenarios(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scs = []
    for s in rows:
        sid = s.get("scenario_id", "")
        scs.append({
            "id": sid,
            "title": _title_from_raw_log(s.get("raw_log") or ""),
            "status": s.get("status"),
            "points": s.get("points"),
            "points_max": POINTS_PER_SCENARIO,
            "summary": s.get("summary"),
            "note": s.get("note"),
            "failure_kind": s.get("failure_kind"),
            "duration_s": s.get("duration_seconds"),
            "turn_count": s.get("turn_count"),
            "ttft_ms": s.get("ttft_ms"),
            "turn_latencies_ms": s.get("turn_latencies_ms"),
            "prompt_tokens": s.get("prompt_tokens"),
            "completion_tokens": s.get("completion_tokens"),
            "total_tokens": s.get("total_tokens"),
            "tool_call_arg_bytes": s.get("tool_call_arg_bytes"),
            "tool_calls_made": s.get("tool_calls_made"),
            "expected_behavior": s.get("expected_behavior"),
            "parallel_tool_turns": s.get("parallel_tool_turns"),
        })
    return scs


def _parse_categories(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cats = []
    for c in rows:
        cats.append({
            "category": c.get("category"),
            "label": c.get("label"),
            "earned": c.get("earned"),
            "max": c.get("max"),
            "percent": c.get("percent"),
            "pass_count": c.get("pass_count"),
            "partial_count": c.get("partial_count"),
            "fail_count": c.get("fail_count"),
        })
    return cats


def parse_result_json(d: dict[str, Any]) -> dict[str, Any]:
    """Normalize a tool-eval-bench ``result.json`` into the report shape."""
    sc = d.get("scores", {})
    cfg = d.get("config", {})
    md = d.get("metadata", {})

    # Completion data: tool-eval-bench excludes scenarios that failed with a
    # server_error (infrastructure failure) from scoring, so ``scores.max_points``
    # shrinks to only the scenarios that ran.  Surface the completion rate and the
    # excluded count so the dashboard can tell a 89%-of-36 run from a 88%-of-138
    # run, and compute a full-suite comparable score on a common denominator.
    excluded = sc.get("excluded_scenarios") or []
    scenario_rows = sc.get("scenario_results", [])
    total_points = sc.get("total_points")
    completion_rate = sc.get("completion_rate")
    if completion_rate is None and scenario_rows:
        ran = len(scenario_rows) - len(excluded)
        completion_rate = round(ran / len(scenario_rows) * 100, 1)
    comparable_score = None
    if total_points is not None:
        comparable_score = round(total_points / FULL_SUITE_MAX_POINTS * 100)

    report: dict[str, Any] = {
        "schema_version": d.get("schema_version"),
        "version": d.get("tool_eval_bench_version"),
        "run_id": d.get("run_id"),
        "status": d.get("status"),
        "final_score": d.get("final_score"),
        "total_points": total_points,
        "max_points": sc.get("max_points"),
        "comparable_score": comparable_score,
        "completion_rate": completion_rate,
        "excluded_count": len(excluded),
        "scenarios_run": len(scenario_rows) - len(excluded),
        "rating": d.get("rating"),
        "deployability": d.get("deployability"),
        "responsiveness": d.get("responsiveness"),
        "median_turn_ms": sc.get("median_turn_ms"),
        "alpha": sc.get("alpha"),
        "total_tokens": sc.get("total_tokens"),
        "token_efficiency": sc.get("token_efficiency"),
        "worst_category": sc.get("worst_category"),
        "worst_category_percent": sc.get("worst_category_percent"),
        "safety_gate": d.get("safety_gate"),
        "safety_warnings": d.get("safety_warnings", []),
        "categories": _parse_categories(sc.get("category_scores", [])),
        "scenarios": _parse_scenarios(scenario_rows),
    }

    # Structured context mirrors for the dashboard's "Run Context" panel.
    # config carries the run knobs; metadata carries the engine + the
    # scenario selector string ("all (69)").
    report["run_context"] = {
        "Backend": cfg.get("backend"),
        "Server": cfg.get("base_url"),
        "Model (API)": cfg.get("model"),
        "Temperature": cfg.get("temperature"),
        "Seed": cfg.get("seed"),
        "Max Turns": cfg.get("max_turns"),
        "Timeout": cfg.get("timeout_seconds"),
        "Scenarios": md.get("scenario_selector"),
        "Parallel": cfg.get("concurrency"),
        "Error Rate": cfg.get("error_rate"),
        "Alpha": cfg.get("alpha"),
        "Config Fingerprint": cfg.get("config_fingerprint"),
    }
    report["inference_engine"] = {
        "Engine": (f"{md.get('engine_name', '')} {md.get('engine_version', '')}").strip() or None,
        "Max Model Length": md.get("max_model_len"),
        "Host": md.get("hostname"),
        "Platform": md.get("platform_info"),
        "Python": md.get("python_version"),
        "Git SHA": md.get("git_sha"),
        "Thinking Enabled": md.get("thinking_enabled"),
    }
    return report


# ---------------------------------------------------------------------------
# manifest assembly
# ---------------------------------------------------------------------------

# meta.json fields folded into each run entry (complement result.json with the
# run-invocation flags + the unmasked endpoint + the human run_name).
META_KEEP = (
    "timestamp", "host", "bench", "model", "endpoint", "run_name",
    "short", "hardmode", "hardmode_only", "categories", "scenarios", "seed",
    "trials", "parallel", "temperature", "no_think", "max_turns",
)


def build_run(folder: str, run: str, run_dir: str) -> dict[str, Any] | None:
    json_path = os.path.join(run_dir, "result.json")
    if not os.path.isfile(json_path):
        return None

    meta: dict[str, Any] = {}
    meta_path = os.path.join(run_dir, "meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:  # noqa: BLE001
            meta = {}

    with open(json_path, encoding="utf-8") as f:
        jdata = json.load(f)
    report = parse_result_json(jdata)

    meta_host = str(meta.get("host") or "")
    meta_model = str(meta.get("model") or report.get("run_id") or folder)

    ts = parse_run_timestamp(run)
    meta_ts = meta.get("timestamp")
    if meta_ts:
        parsed = parse_run_timestamp(str(meta_ts))
        if parsed:
            ts = parsed

    return {
        "run": run,
        "folder": folder,
        "ts": ts,
        "host": meta_host,
        "model": meta_model,
        "report": report,
        "meta": {k: meta.get(k) for k in META_KEEP if k in meta},
    }


def build_manifest(root: str) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    skipped: list[str] = []

    if not os.path.isdir(root):
        return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "count": 0, "skipped": [f"{root}: not a directory"], "runs": []}

    for folder in sorted(os.listdir(root)):
        teb = os.path.join(root, folder, "tool-eval-bench")
        if not os.path.isdir(teb):
            continue
        for run in sorted(os.listdir(teb)):
            run_dir = os.path.join(teb, run)
            if not os.path.isdir(run_dir):
                continue
            if not os.path.isfile(os.path.join(run_dir, "result.json")):
                skipped.append(f"{folder}/tool-eval-bench/{run}: no result.json")
                continue
            try:
                entry = build_run(folder, run, run_dir)
                if entry is None:
                    skipped.append(f"{folder}/tool-eval-bench/{run}: parse failed")
                    continue
                runs.append(entry)
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{folder}/tool-eval-bench/{run}: {e}")

    runs.sort(key=lambda r: (r.get("ts") or "", r.get("folder") or ""), reverse=True)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(runs),
        "skipped": skipped,
        "runs": runs,
    }


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    default_root = os.path.join(here, "..", "benchmarks")
    default_out = os.path.join(default_root, "_tool-eval-scoreboards", "manifest.json")

    parser = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=default_root,
                        help="path to the benchmarks/ directory")
    parser.add_argument("-o", "--output", default=default_out,
                        help="output manifest path (default: <root>/_tool-eval-scoreboards/manifest.json)")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    manifest = build_manifest(root)
    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
        f.write("\n")

    print(f"wrote {output}: {manifest['count']} runs")
    if manifest["skipped"]:
        print(f"  skipped {len(manifest['skipped'])}:")
        for s in manifest["skipped"]:
            print(f"    {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
