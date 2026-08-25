#!/usr/bin/env python3
"""Compile every ``<folder>/tool-eval-bench/<timestamp>/result.md`` report into a
single ``manifest.json`` that the dashboard (``benchmarks/index.html``) loads with
one fetch.

Each tool-eval-bench run directory contains:

    <folder>/tool-eval-bench/<timestamp>/
        meta.json     # run parameters (host, model, run_name, timestamp, ...)
        result.md     # the human-readable Markdown report (the source of truth)
        run.log       # console log (optional; only the wall-clock duration is read)

This script parses ``result.md`` (header bullets + the Category Scores,
Scenario Results and Performance-by-Difficulty tables) and normalizes it to:

    {
      "generated_at": "2026-08-25T...",
      "count": 1,
      "skipped": [...],
      "runs": [
        {
          "run":   "20260825-152901",
          "folder":"rtx5090-vllm-dockerized-Qwen3.8-27B-NVFP4-CUDA",
          "ts":    "2026-08-25T15:29:01",
          "host":  "thing",                 # machine hostname (meta.json), informational
          "model": "Qwen3.8-27B-NVFP4-RTX5090",
          "report": {
            "run_id": "...", "date": "...", "version": "...",
            "final_score": 88, "total_points_earned": 122, "total_points_max": 138,
            "rating": "★★★★ Good",
            "tool_def_overhead_tokens": 4742, "num_tools": 52,
            "tool_def_overhead_chars": 18970,
            "deployability": 82, "quality": 88, "responsiveness": 68,
            "median_turn_s": 1.8, "duration_s": 470.7,
            "run_context": { "backend": "vllm", ... },
            "inference_engine": { "engine": "vLLM 0.27.1", ... },
            "categories": [ {"category": "...", "earned": 6, "max": 6, "percent": 100}, ... ],
            "scenarios": [ {"id": "TC-01", "title": "...", "diff": 1,
                            "status": "pass", "points_earned": 2, "points_max": 2,
                            "failure": null, "summary": "..."}, ... ],
            "difficulty": [ {"tier": "Trivial", "level": 1, "scenarios": 4,
                            "passed": 4, "rate": 1.0}, ... ]
          },
          "meta": { ...selected meta.json fields... }
        }, ...
      ]
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


def decode_entities(s: str) -> str:
    """Decode the handful of HTML entities tool-eval-bench emits in its tables."""
    return (s.replace("&amp;", "&")
             .replace("&lt;", "<")
             .replace("&gt;", ">")
             .replace("&quot;", '"')
             .replace("&#39;", "'"))


def clean_cell(s: str) -> str:
    """Trim a table cell and decode HTML entities, keeping inner text."""
    return decode_entities(s.strip())


def _int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# result.md parsing
# ---------------------------------------------------------------------------

# A markdown table row: leading "| ... |" with pipe-separated cells.
TableRow = tuple[list[str], list[str]]  # (header_cells, body_cells) — handled inline


def _table_rows(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """Starting at the table header line index ``start``, return all body rows
    (each a list of cell strings) and the index of the first line after the table."""
    header = [c.strip() for c in lines[start].strip().strip("|").split("|")]
    body: list[list[str]] = []
    i = start + 1
    # skip the separator row (---|---|...)
    if i < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i]):
        i += 1
    ncol = len(header)
    while i < len(lines):
        line = lines[i]
        if not line.strip() or not line.lstrip().startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # A summary that contained a literal "|" would over-split; rejoin the
        # trailing cells back into the last column so we always have ncol columns.
        if len(cells) > ncol:
            cells = cells[: ncol - 1] + [" | ".join(cells[ncol - 1:])]
        body.append([decode_entities(c) for c in cells])
        i += 1
    return body, i


def _find_table(lines: list[str], header_re: str) -> list[list[str]] | None:
    """Find the first table whose header row matches ``header_re`` (against the
    raw header line) and return its body rows, or None."""
    rx = re.compile(header_re)
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("|") and rx.search(line):
            body, _ = _table_rows(lines, idx)
            return body
    return None


_HEADER_BULLETS = {
    "Run ID": "run_id",
    "Date": "date",
    "tool-eval-bench": "version",
    "Final Score": "final_score",
    "Total Points": "total_points",
    "Rating": "rating",
    "Tool Definition Overhead": "tool_def_overhead",
    "Deployability": "deployability",
    "Quality": "quality",
    "Responsiveness": "responsiveness",
}


def _parse_header(lines: list[str]) -> dict[str, Any]:
    """Parse the leading ``- **Key**: value`` bullets (up to the first blank line
    or the first table)."""
    out: dict[str, Any] = {}
    for line in lines:
        s = line.strip()
        if not s:
            # a single blank line separates the rating block from the rest;
            # keep scanning until we hit a table or section heading
            continue
        # A section heading ("## Run Context") or a table row ends the header.
        if s.startswith("##") or s.startswith("|"):
            break
        # Skip the H1 title line ("# Tool-Call Benchmark — ...").
        if s.startswith("#"):
            continue
        m = re.match(r"^-\s*\*\*(.+?)\*\*\s*:\s*(.*)$", s)
        if not m:
            continue
        key_raw, val = m.group(1).strip(), m.group(2).strip()
        field = _HEADER_BULLETS.get(key_raw)
        if field is None:
            continue
        # strip surrounding markdown bold/backticks
        val = re.sub(r"^\*\*(.+?)\*\*$", r"\1", val)

        if field == "final_score":
            m2 = re.search(r"(\d+)\s*/\s*100", val)
            out["final_score"] = _int(m2.group(1)) if m2 else None
        elif field == "total_points":
            m2 = re.search(r"(\d+)\s*/\s*(\d+)", val)
            if m2:
                out["total_points_earned"] = _int(m2.group(1))
                out["total_points_max"] = _int(m2.group(2))
        elif field == "rating":
            out["rating"] = val
        elif field == "tool_def_overhead":
            # "~4,742 tokens (52 tools, 18,970 chars)"
            m2 = re.search(r"~?([\d,]+)\s*tokens", val)
            out["tool_def_overhead_tokens"] = (
                int(m2.group(1).replace(",", "")) if m2 else None)
            m3 = re.search(r"(\d+)\s*tools?", val)
            out["num_tools"] = _int(m3.group(1)) if m3 else None
            m4 = re.search(r"([\d,]+)\s*chars", val)
            out["tool_def_overhead_chars"] = (
                int(m4.group(1).replace(",", "")) if m4 else None)
        elif field in ("deployability", "quality", "responsiveness"):
            m2 = re.search(r"(\d+)\s*/\s*100", val)
            out[field] = _int(m2.group(1)) if m2 else None
            if field == "responsiveness":
                m3 = re.search(r"median turn:\s*([\d.]+)s", val)
                out["median_turn_s"] = _float(m3.group(1)) if m3 else None
        else:
            # strip backticks
            out[field] = re.sub(r"^`(.+)`$", r"\1", val)
    return out


def _parse_kv_table(body: list[list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in body:
        if len(row) < 2:
            continue
        k = clean_cell(row[0])
        v = clean_cell(row[1])
        out[k] = re.sub(r"^`(.+)`$", r"\1", v)
    return out


def _parse_categories(body: list[list[str]]) -> list[dict[str, Any]]:
    cats = []
    for row in body:
        if len(row) < 4:
            continue
        cats.append({
            "category": clean_cell(row[0]),
            "earned": _int(row[1]),
            "max": _int(row[2]),
            "percent": _float(row[3].rstrip("%")) if row[3].strip() else None,
        })
    return cats


_STATUS_MAP = {
    "pass": "pass",
    "partial": "partial",
    "fail": "fail",
}


def _parse_scenarios(body: list[list[str]]) -> list[dict[str, Any]]:
    scs = []
    for row in body:
        if len(row) < 7:
            continue
        sid = clean_cell(row[0])
        if not re.match(r"^TC-\d+$", sid):
            continue
        status_raw = clean_cell(row[3]).lower()
        # "✅ pass" / "⚠️ partial" / "❌ fail" — keep the last word
        status = None
        for word in status_raw.split():
            if word in _STATUS_MAP:
                status = _STATUS_MAP[word]
                break
        if status is None:
            status = status_raw or None
        pts = clean_cell(row[4])
        m = re.match(r"(\d+)\s*/\s*(\d+)", pts)
        fail = clean_cell(row[5])
        scs.append({
            "id": sid,
            "title": clean_cell(row[1]),
            "diff": row[2].count("★") or None,
            "status": status,
            "points_earned": _int(m.group(1)) if m else None,
            "points_max": _int(m.group(2)) if m else None,
            "failure": None if fail in ("", "—", "-") else fail,
            "summary": clean_cell(row[6]),
        })
    return scs


def _parse_difficulty(body: list[list[str]]) -> list[dict[str, Any]]:
    tiers = []
    for row in body:
        if len(row) < 4:
            continue
        tier_cell = clean_cell(row[0])
        m = re.search(r"\((\d+)\)", tier_cell)
        rate = clean_cell(row[3]).rstrip("%")
        tiers.append({
            "tier": re.sub(r"\s*\(\d+\)\s*$", "", tier_cell),
            "level": _int(m.group(1)) if m else None,
            "scenarios": _int(row[1]),
            "passed": _int(row[2]),
            "rate": (_float(rate) / 100.0) if rate not in ("", "—") else None,
        })
    return tiers


def parse_result_md(text: str) -> dict[str, Any]:
    """Parse a tool-eval-bench ``result.md`` into a normalized report dict."""
    lines = text.splitlines()
    report: dict[str, Any] = _parse_header(lines)

    rc = _find_table(lines, r"Parameter\s*\|\s*Value") or \
         _find_table(lines, r"^\|\s*Parameter\b")
    if rc is not None:
        report["run_context"] = _parse_kv_table(rc)

    ie = _find_table(lines, r"^\|\s*Property\s*\|\s*Value")
    if ie is not None:
        report["inference_engine"] = _parse_kv_table(ie)

    cats = _find_table(lines, r"^\|\s*Category\s*\|\s*Earned\s*\|\s*Max\s*\|\s*Percent")
    if cats is not None:
        report["categories"] = _parse_categories(cats)
    else:
        report["categories"] = []

    scs = _find_table(lines, r"^\|\s*ID\s*\|\s*Title\s*\|\s*Diff\s*\|\s*Status")
    if scs is not None:
        report["scenarios"] = _parse_scenarios(scs)
    else:
        report["scenarios"] = []

    diff = _find_table(lines, r"^\|\s*Tier\s*\|\s*Scenarios\s*\|\s*Passed\s*\|\s*Rate")
    if diff is not None:
        report["difficulty"] = _parse_difficulty(diff)
    else:
        report["difficulty"] = []

    return report


def parse_duration_s(log_path: str) -> float | None:
    """Best-effort: pull "Completed in 470.7s" from the run.log."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"Completed in\s+([\d.]+)s", line)
                if m:
                    return _float(m.group(1))
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# manifest assembly
# ---------------------------------------------------------------------------

META_KEEP = (
    "timestamp", "host", "bench", "model", "endpoint", "run_name",
    "short", "hardmode", "hardmode_only", "categories", "scenarios", "seed",
    "trials", "parallel", "temperature", "no_think", "max_turns",
)


def build_run(folder: str, run: str, run_dir: str) -> dict[str, Any] | None:
    meta_path = os.path.join(run_dir, "meta.json")
    md_path = os.path.join(run_dir, "result.md")
    if not os.path.isfile(md_path):
        return None
    meta: dict[str, Any] = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:  # noqa: BLE001
            meta = {}

    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    report = parse_result_md(text)

    # duration from run.log (optional)
    dur = parse_duration_s(os.path.join(run_dir, "run.log"))
    if dur is not None:
        report["duration_s"] = dur

    meta_host = str(meta.get("host") or "")
    meta_model = str(meta.get("model") or report.get("run_id") or folder)

    # ts: prefer meta timestamp, fall back to the dir name.
    ts = parse_run_timestamp(run)
    meta_ts = meta.get("timestamp")
    if meta_ts:
        # meta "20260825-152901" -> same format; keep the parsed iso form.
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
            if not os.path.isfile(os.path.join(run_dir, "result.md")):
                skipped.append(f"{folder}/tool-eval-bench/{run}: no result.md")
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
