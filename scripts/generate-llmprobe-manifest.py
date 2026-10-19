#!/usr/bin/env python3
"""Collect native llmprobe reports from <root>/<folder>/llmprobe/<run>/result.json.

Usage: python3 scripts/generate-llmprobe-manifest.py [--root benchmarks] [-o FILE]
The default output is <root>/_llmprobe-scoreboards/manifest.json, including when
--root is supplied. Only real historical directories are scanned; ``latest``
and all symlink aliases are excluded. No input file is changed.

Manifest v1: {schema_version, generated_at, count, skipped, warnings, runs}.
``skipped`` contains path-prefixed diagnostics for excluded runs/aliases;
``warnings`` records unusable optional sidecars without discarding their report.
Each run has {folder, run, ts, host, model, report, meta, path, artifacts, status,
depth, mode, performance, returncode, complete, rankable, completion_reasons}.
``report`` is the native v1/v2 JSON object, unchanged, not a flattened score.
``meta`` is the entire sidecar object, or {} when absent/unreadable. ``ts`` is
UTC ISO time (metadata, then native startedAt, then directory-name fallback),
or null. Missing depth/mode/performance/returncode stay null; host defaults to
"" and model falls back to report.target.model. ``path`` and artifact paths are
POSIX filesystem paths relative to root, NOT relative to the manifest. A UI
must URL-encode their components when constructing links.

``status`` preserves known driver statuses, except native evidence of failure
can override a successful status (the original remains in meta). Without driver
status it is unknown unless native evidence proves an early stop. ``complete``
requires a successful driver status, compatible return code, recorded native
scope, all six phase statuses, and no unexpected partial/failed/interrupted
phase, exhausted budget, or incomplete report. Native partial coverage and
quick-depth partial conformance are intentional scope limits, not execution
failures. An unavailable/not-run phase is not a failure. ``rankable`` additionally
requires a default/full probe with nonempty coverage/conformance/capability,
measured conformance/capability, and measured or partial coverage. Quick and
non-probe runs are never rankable. It is NOT a claim that every optional card is
rankable, or that different models/hosts/depths are comparable. Conformance
failures may be completed measurements; failed/budget-limited runs never are.
Unknown scope is never promoted to a full measurement. Native optional cards
and unknown extension fields are retained, never synthesized.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


# Direct invocation puts scripts/, not the repository root, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.llmprobe_report import (  # noqa: E402; keep public schema aliases for callers
    DEPTHS, PHASES, PHASE_STATUSES, REPORT, read_json, require, validate_report,
)


STATUSES = ("complete", "conformance-failed", "budget-exhausted", "incomplete",
            "failed", "timeout", "interrupted")
SUCCESS = ("complete", "conformance-failed")


def timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def run_timestamp(name: str) -> str | None:
    match = re.match(r"^(\d{8}-\d{6})(?:$|[-_.])", name)
    if match:
        try:
            return timestamp(dt.datetime.strptime(match[1], "%Y%m%d-%H%M%S").isoformat())
        except ValueError:
            pass
    return None


def completion(report: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    scope = report.get("run", {})
    phases = scope.get("phases", {})
    reasons: list[str] = []
    status = meta.get("status") if meta.get("status") in STATUSES else "unknown"
    if status not in SUCCESS:
        reasons.append(f"driver status: {status}")
    rc = meta.get("returncode")
    if rc is not None and (type(rc) is not int or
                          rc not in ((0, 1) if status == "conformance-failed" else (0,))):
        reasons.append("returncode does not confirm successful completion")
    native_status = None
    if report.get("incomplete"):
        native_status = "incomplete"
        reasons.append(f"report incomplete: {report['incomplete']}")
    if scope.get("budget", {}).get("exhausted"):
        native_status = native_status or "budget-exhausted"
        reasons.append("token budget exhausted")
    if report.get("reasoning", {}).get("aborted"):
        native_status = native_status or "incomplete"
        reasons.append("reasoning aborted")
    if "run" not in report:
        reasons.append("native run scope missing")
    if "depth" not in scope or "mode" not in scope:
        reasons.append("native depth/mode unknown")
    for name in (*PHASES, *(("reasoning",) if "reasoning" in phases else ())):
        phase_status = phases.get(name, {}).get("status")
        if phase_status is None:
            reasons.append(f"{name} phase status unknown")
        elif phase_status in ("partial", "failed", "interrupted"):
            # runScope marks unprobed coverage partial at any depth, and quick
            # conformance partial because slow checks are intentionally omitted.
            if phase_status == "partial" and (
                name == "coverage" or (name == "conformance" and scope.get("depth") == "quick")
            ):
                continue
            reasons.append(f"{name} phase: {phase_status}")
            if phase_status != "partial":
                native_status = native_status or phase_status
    if native_status and status in (*SUCCESS, "unknown"):
        status = native_status
    depth = scope.get("depth", meta.get("depth"))
    complete = not reasons
    core_rankable = (phases.get("coverage", {}).get("status") in ("measured", "partial")
                     and all(phases.get(name, {}).get("status") == "measured"
                             for name in ("conformance", "capability")))
    core_nonempty = (any(tier["total"] > 0 for tier in report["coverage"]["byTier"])
                     and report["conformance"]["total"] > 0
                     and any(cat["total"] > 0 for cat in report["capability"]["categories"]))
    performance = meta.get("performance")
    if type(performance) is not bool:
        performance = True if "bench" in report else (
            False if phases.get("performance", {}).get("status") == "not-run" else None)
    return {
        "status": status, "depth": depth if depth in DEPTHS else None,
        "mode": scope.get("mode"), "performance": performance,
        "returncode": rc if type(rc) is int else None,
        "complete": complete,
        "rankable": bool(complete and scope.get("mode") == "probe" and depth in ("default", "full")
                         and core_rankable and core_nonempty),
        "completion_reasons": reasons,
    }


def safe_component(name: str) -> bool:
    # Backslashes/colons can acquire path or URL semantics in browser consumers.
    return not any(char in name for char in "\\:") and all(ord(char) >= 32 for char in name)


def real_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def build_run(root: Path, run_dir: Path, warnings: list[str]) -> dict[str, Any]:
    relative = run_dir.relative_to(root).as_posix()
    report = read_json(run_dir / "result.json")
    validate_report(report)
    artifacts = {"report": f"{relative}/result.json"}
    meta: dict[str, Any] = {}
    meta_path = run_dir / "meta.json"
    if meta_path.is_symlink():
        warnings.append(f"{relative}/meta.json: symlink ignored")
    elif meta_path.exists():
        if real_file(meta_path):
            artifacts["meta"] = f"{relative}/meta.json"
            try:
                meta = read_json(meta_path)
                require(isinstance(meta, dict), "meta", "object")
            except (OSError, ValueError, UnicodeError, RecursionError) as error:
                meta = {}
                warnings.append(f"{relative}/meta.json: {error}")
        else:
            warnings.append(f"{relative}/meta.json: not a regular file")
    # Drivers may use either name. Never publish a guessed or metadata-supplied path.
    for name in ("run.log", "llmprobe.log", "output.log", "log.txt"):
        if real_file(run_dir / name):
            artifacts["log"] = f"{relative}/{name}"
            break
    scope = report.get("run", {})
    return {
        "folder": run_dir.parent.parent.name, "run": run_dir.name,
        "ts": timestamp(meta.get("timestamp")) or timestamp(scope.get("startedAt"))
              or run_timestamp(run_dir.name),
        "host": meta.get("host") if isinstance(meta.get("host"), str) else "",
        "model": meta.get("model") if isinstance(meta.get("model"), str) and meta["model"]
                 else report["target"]["model"],
        "report": report, "meta": meta, "path": relative, "artifacts": artifacts,
        **completion(report, meta),
    }


def build_manifest(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    runs: list[dict[str, Any]] = []
    skipped: list[str] = []
    warnings: list[str] = []

    def directories(parent: Path):
        for path in sorted(parent.iterdir()):
            relative = path.relative_to(root).as_posix()
            if path.name == "latest":
                skipped.append(f"{relative}: latest alias ignored")
            elif path.is_symlink():
                skipped.append(f"{relative}: symlink ignored")
            elif not safe_component(path.name):
                skipped.append(f"{relative}: unsafe path component")
            elif path.is_dir():
                yield path

    if not root.is_dir():
        skipped.append("root: not a directory")
    else:
        for folder in directories(root):
            bench = folder / "llmprobe"
            if bench.is_symlink():
                skipped.append(f"{folder.name}/llmprobe: symlink ignored")
                continue
            if not bench.is_dir():
                continue
            for run_dir in directories(bench):
                relative = run_dir.relative_to(root).as_posix()
                result = run_dir / "result.json"
                if not real_file(result):
                    reason = "symlink ignored" if result.is_symlink() else "missing regular result.json"
                    skipped.append(f"{relative}: {reason}")
                    continue
                try:
                    runs.append(build_run(root, run_dir, warnings))
                except (OSError, ValueError, UnicodeError, RecursionError) as error:
                    skipped.append(f"{relative}: {error}")
    runs.sort(key=lambda run: (dt.datetime.fromisoformat(run["ts"].replace("Z", "+00:00")).timestamp()
                               if run["ts"] else float("-inf"), run["folder"], run["run"]), reverse=True)
    return {"schema_version": 1, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "count": len(runs), "skipped": skipped, "warnings": warnings, "runs": runs}


def write_manifest(output: Path, manifest: dict[str, Any]) -> None:
    """Replace in the destination directory, so readers never see partial JSON."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=f".{output.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parent.parent / "benchmarks"
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=default_root, help="benchmarks directory")
    parser.add_argument("-o", "--output", type=Path,
                        help="output path (default: <root>/_llmprobe-scoreboards/manifest.json)")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2
    output = args.output if args.output is not None else args.root / "_llmprobe-scoreboards" / "manifest.json"
    try:
        manifest = build_manifest(args.root)
        write_manifest(output, manifest)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"wrote {output}: {manifest['count']} runs; {len(manifest['skipped'])} skipped; "
          f"{len(manifest['warnings'])} warnings")
    for kind in ("skipped", "warnings"):
        for diagnostic in manifest[kind]:
            print(f"  {kind}: {diagnostic}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
