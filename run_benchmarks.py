#!/usr/bin/env python3
"""Single entrypoint that runs a configured matrix of (endpoint, model, benchmark).

Reads a TOML config that lists the OpenAI-compatible endpoints, the models to
test on each endpoint, and which benchmarks to run for each model.  Each
benchmark is invoked as a subprocess (one of the existing driver scripts in
this repo) and run in continue-on-failure mode.  A final summary table is
printed and persisted to ./benchmarks/_run-summaries/<ts>/summary.{json,txt}.

Available benchmarks (matches the flake apps and the *.sh / *.py drivers):
    llama-benchy   -> llama-benchy-benchmarks.sh
    aider          -> aider-polyglot-benchmarks.sh
    terminal-bench -> terminal-bench-benchmarks.sh
    agent-bench    -> agent_bench.py

Usage:
    run_benchmarks.py [--config benchmarks.toml] [--dry-run] [--new]
                      [--only bench[,bench...]] [--endpoint NAME] [--model NAME]

If --config is omitted, the script looks for ./benchmarks.toml next to itself.

Config schema (TOML):

    output_dir = "./benchmarks"   # optional, default "./benchmarks"

    [[endpoints]]
    name       = "local"
    url        = "http://localhost:8080/v1"
    api_key    = "EMPTY"           # optional
    benchmarks = ["llama-benchy", "agent-bench"]  # default for this endpoint

      [[endpoints.models]]
      name       = "qwen2.5-coder-32b"
      benchmarks = ["llama-benchy", "aider", "agent-bench"]  # override

      [[endpoints.models]]
      name = "gpt-oss-120b"
      # inherits endpoint-level benchmarks
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


# Map of logical benchmark id -> driver script (relative to this file's dir).
# We resolve the driver per call so the script also works when run from the
# nix-built flake (where SCRIPT_DIR contains the wrappers).
BENCHMARKS: dict[str, str] = {
    "llama-benchy":   "llama-benchy-benchmarks.sh",
    "aider":          "aider-polyglot-benchmarks.sh",
    "terminal-bench": "terminal-bench-benchmarks.sh",
    "agent-bench":    "agent_bench.py",
}


@dataclass
class Job:
    endpoint_name: str
    endpoint_url: str
    api_key: str
    model: str
    bench: str
    # Result fields populated after run():
    returncode: int | None = None
    duration_s: float | None = None
    error: str | None = None


@dataclass
class Plan:
    output_dir: Path
    jobs: list[Job] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def build_plan(
    cfg: dict[str, Any],
    only_benches: set[str] | None,
    only_endpoint: str | None,
    only_model: str | None,
) -> Plan:
    output_dir = Path(cfg.get("output_dir", "./benchmarks")).resolve()
    endpoints = cfg.get("endpoints", [])
    if not endpoints:
        raise SystemExit("Config error: no [[endpoints]] defined")

    plan = Plan(output_dir=output_dir)

    for ep in endpoints:
        ep_name = ep.get("name") or ep.get("url")
        if not ep_name:
            raise SystemExit(f"Config error: endpoint missing both name and url: {ep!r}")
        if only_endpoint and ep_name != only_endpoint:
            continue

        url = ep.get("url")
        if not url:
            raise SystemExit(f"Config error: endpoint {ep_name!r} missing url")
        api_key = ep.get("api_key", "EMPTY")
        default_benches = ep.get("benchmarks", [])
        models = ep.get("models", [])
        if not models:
            raise SystemExit(f"Config error: endpoint {ep_name!r} has no [[endpoints.models]]")

        for mdl in models:
            mname = mdl.get("name")
            if not mname:
                raise SystemExit(f"Config error: endpoint {ep_name!r} has model without name")
            if only_model and mname != only_model:
                continue
            benches = mdl.get("benchmarks", default_benches)
            if not benches:
                print(
                    f"!!! warning: {ep_name}/{mname} has no benchmarks selected; skipping",
                    file=sys.stderr,
                )
                continue
            for b in benches:
                if b not in BENCHMARKS:
                    raise SystemExit(
                        f"Config error: unknown benchmark {b!r} for {ep_name}/{mname}. "
                        f"Valid: {sorted(BENCHMARKS)}"
                    )
                if only_benches and b not in only_benches:
                    continue
                plan.jobs.append(
                    Job(
                        endpoint_name=ep_name,
                        endpoint_url=url,
                        api_key=api_key,
                        model=mname,
                        bench=b,
                    )
                )
    return plan


# ---------------------------------------------------------------------------
# Driver resolution + execution
# ---------------------------------------------------------------------------
# Logical bench id -> flake wrapper name (provides correct runtime deps:
# `uv`/`python3` for llama-benchy, the `pyEnv` with openai+pydantic for
# agent-bench, podman/docker for aider, etc).
WRAPPERS: dict[str, str] = {
    "llama-benchy":   "llama-benchy-bench",
    "aider":          "aider-bench",
    "terminal-bench": "terminal-bench",
    "agent-bench":    "agent-bench",
}


def resolve_driver(bench: str, script_dir: Path) -> list[str]:
    """Return the argv prefix that invokes the driver for `bench`.

    Prefers the flake-installed wrapper on PATH (`aider-bench`,
    `llama-benchy-bench`, `terminal-bench`, `agent-bench`) because those
    wrappers carry the correct runtime dependencies (uv, python with
    openai+pydantic, podman/docker, LD_LIBRARY_PATH).  Falls back to the raw
    scripts in `script_dir` only when no wrapper is on PATH, e.g. when running
    directly out of a git checkout inside the devShell.
    """
    wrapper = WRAPPERS[bench]
    on_path = shutil.which(wrapper)
    if on_path:
        return [on_path]

    rel = BENCHMARKS[bench]
    local = script_dir / rel
    if local.exists():
        if rel.endswith(".py"):
            return [sys.executable, str(local)]
        return ["bash", str(local)]

    # Last resort: hope the wrapper is on PATH at exec time (will produce a
    # clear FileNotFoundError if not).
    return [wrapper]


def run_job(job: Job, output_dir: Path, script_dir: Path, dry_run: bool, force_new: bool = False) -> None:
    driver = resolve_driver(job.bench, script_dir)
    argv = driver + [
        "--endpoint", job.endpoint_url,
        "--api-key", job.api_key,
        "--model", job.model,
        "--output-dir", str(output_dir),
    ]
    if force_new:
        argv.append("--new")
    pretty = " ".join(argv)
    print(f"\n>>> [{job.endpoint_name}] {job.model} :: {job.bench}")
    print(f"    cmd: {pretty}")

    if dry_run:
        job.returncode = 0
        job.duration_s = 0.0
        return

    start = datetime.now()
    try:
        proc = subprocess.run(argv, check=False)
        job.returncode = proc.returncode
    except FileNotFoundError as e:
        job.returncode = 127
        job.error = f"driver not found: {e}"
        print(f"    !!! {job.error}", file=sys.stderr)
    except KeyboardInterrupt:
        job.returncode = 130
        job.error = "interrupted"
        raise
    except Exception as e:  # noqa: BLE001
        job.returncode = 1
        job.error = f"{type(e).__name__}: {e}"
        print(f"    !!! {job.error}", file=sys.stderr)
    finally:
        job.duration_s = (datetime.now() - start).total_seconds()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def write_summary(plan: Plan, ts: str) -> Path:
    board_dir = plan.output_dir / "_run-summaries" / ts
    board_dir.mkdir(parents=True, exist_ok=True)

    json_path = board_dir / "summary.json"
    txt_path = board_dir / "summary.txt"

    payload = {
        "timestamp": ts,
        "output_dir": str(plan.output_dir),
        "jobs": [asdict(j) for j in plan.jobs],
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n")

    lines: list[str] = []
    lines.append("=" * 90)
    lines.append(f"RUN SUMMARY  ({ts})")
    lines.append("=" * 90)
    lines.append(
        f"{'Endpoint':<20} | {'Model':<35} | {'Benchmark':<14} | {'rc':<3} | {'dur(s)':<8} | status"
    )
    lines.append("-" * 90)
    ok = 0
    fail = 0
    for j in plan.jobs:
        status = "OK" if j.returncode == 0 else f"FAIL ({j.error or 'rc!=0'})"
        if j.returncode == 0:
            ok += 1
        else:
            fail += 1
        dur = f"{j.duration_s:.1f}" if j.duration_s is not None else "-"
        rc = "-" if j.returncode is None else str(j.returncode)
        lines.append(
            f"{j.endpoint_name:<20.20} | {j.model:<35.35} | {j.bench:<14} | {rc:<3} | {dur:<8} | {status}"
        )
    lines.append("-" * 90)
    lines.append(f"Total: {len(plan.jobs)}   OK: {ok}   FAIL: {fail}")
    text = "\n".join(lines) + "\n"
    txt_path.write_text(text)
    print("\n" + text)
    print(f">>> Summary dir: {board_dir}")
    return board_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Single entrypoint that runs configured benchmarks across endpoints/models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "-c", "--config",
        default=None,
        help="Path to TOML config (default: ./benchmarks.toml next to this script or CWD)",
    )
    p.add_argument(
        "--only",
        default=None,
        help="Comma-separated subset of benchmarks to run (e.g. 'llama-benchy,agent-bench')",
    )
    p.add_argument("--endpoint", default=None, help="Run jobs only for this endpoint name")
    p.add_argument("--model", default=None, help="Run jobs only for this model name")
    p.add_argument("--dry-run", action="store_true", help="Print the plan; don't execute")
    p.add_argument(
        "--new",
        action="store_true",
        help="Re-run benchmarks even if an output symlink already exists (default: skip if symlink present)",
    )
    p.add_argument(
        "--list-benchmarks", action="store_true", help="Print supported benchmark ids and exit"
    )
    return p.parse_args()


def find_default_config(script_dir: Path) -> Path | None:
    for candidate in (Path.cwd() / "benchmarks.toml", script_dir / "benchmarks.toml"):
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    args = parse_args()
    if args.list_benchmarks:
        for k, v in BENCHMARKS.items():
            print(f"{k:<15}  ->  {v}")
        return 0

    script_dir = Path(__file__).resolve().parent
    cfg_path = Path(args.config).resolve() if args.config else find_default_config(script_dir)
    if not cfg_path or not cfg_path.is_file():
        print(
            "Error: no config file found.  Pass --config <path> or create benchmarks.toml.",
            file=sys.stderr,
        )
        return 2

    print(f">>> config: {cfg_path}")
    cfg = load_config(cfg_path)

    only_benches = (
        {b.strip() for b in args.only.split(",") if b.strip()} if args.only else None
    )
    if only_benches:
        unknown = only_benches - set(BENCHMARKS)
        if unknown:
            print(f"Error: --only contains unknown benchmarks: {sorted(unknown)}", file=sys.stderr)
            return 2

    plan = build_plan(cfg, only_benches, args.endpoint, args.model)
    if not plan.jobs:
        print("No jobs to run (after filters).", file=sys.stderr)
        return 1

    plan.output_dir.mkdir(parents=True, exist_ok=True)

    print(f">>> output dir: {plan.output_dir}")
    print(f">>> planned jobs: {len(plan.jobs)}")
    for j in plan.jobs:
        print(f"    - [{j.endpoint_name}] {j.model} :: {j.bench}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        for job in plan.jobs:
            run_job(job, plan.output_dir, script_dir, args.dry_run, force_new=args.new)
    except KeyboardInterrupt:
        print("\n!!! interrupted; writing partial summary", file=sys.stderr)

    write_summary(plan, ts)

    any_fail = any((j.returncode or 0) != 0 for j in plan.jobs)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
