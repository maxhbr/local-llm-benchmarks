#!/usr/bin/env python3
"""Single entrypoint that runs a configured matrix of (endpoint, model, benchmark).

Reads a TOML config that lists the OpenAI-compatible endpoints, the models to
test on each endpoint, and which benchmarks to run for each model.  Each
benchmark is invoked as a subprocess (one of the existing driver scripts in
this repo) and run in continue-on-failure mode.  A final summary table is
printed and persisted to ./benchmarks/_run-summaries/<ts>/summary.{json,txt}.

When a [[endpoints]] section has no [[endpoints.models]], the script queries
the endpoint's ``/v1/models`` and runs benchmarks for **every** model that
the endpoint reports.

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
      alias      = "qwen25-coder-32b-q4"   # optional: friendlier name used for
                                           # output dirs + summary display.  The
                                           # endpoint still receives `name` as
                                           # the model id.
      benchmarks = ["llama-benchy", "aider", "agent-bench"]  # override

      [[endpoints.models]]
      name = "gpt-oss-120b"
      # inherits endpoint-level benchmarks

    # If [[endpoints.models]] is omitted, the script queries the endpoint's
    # /v1/models and benchmarks every model it reports:
    #
    #   [[endpoints]]
    #   name       = "my-endpoint"
    #   url        = "http://localhost:22548/v1"
    #   api_key    = "EMPTY"
    #   benchmarks = ["llama-benchy", "agent-bench"]
    #   edit_format = "diff"     # optional: aider edit format (default: "whole")
    #   # no [[endpoints.models]] — fetches all models from the endpoint
    #
    # An endpoint-level `edit_format` can be overridden per-model:
    #
    #   [[endpoints.models]]
    #   name       = "my-coder-model"
    #   edit_format = "diff"     # override for this model only
    #
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


# Map of logical benchmark id -> driver script (relative to this file's dir).
# We resolve the driver per call so the script also works when run from the
# nix-built flake (where SCRIPT_DIR contains the wrappers).
# Sentinel value None marks "handled inline" (no subprocess driver).
BENCHMARKS: dict[str, str | None] = {
    "llama-benchy":   "llama-benchy-benchmarks.sh",
    "aider":          "aider-polyglot-benchmarks.sh",
    "terminal-bench": "terminal-bench-benchmarks.sh",
    "agent-bench":    "agent_bench.py",
    "smoke":          None,  # inline: simple liveness check, writes smoke.ok
}


# Slug helper (mirrors agent_bench.slugify_model) so output dirs line up with
# the other benchmarks' per-model directory naming.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]")


def slugify_model(model: str) -> str:
    return _SLUG_RE.sub("-", model.replace("/", "_").replace(":", "-"))


@dataclass
class Job:
    endpoint_name: str
    endpoint_url: str
    api_key: str
    model: str          # model id sent to the endpoint (the real API name)
    bench: str
    alias: str | None = None  # friendly name used for output dirs + display
    edit_format: str = "whole"  # aider edit format (whole, diff, etc.)
    # Result fields populated after run():
    returncode: int | None = None
    duration_s: float | None = None
    error: str | None = None

    @property
    def display_name(self) -> str:
        """Name to use for output dirs, summary table, and run-name."""
        return self.alias or self.model


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


def fetch_models_from_endpoint(endpoint_url: str, api_key: str) -> list[str]:
    """Fetch the list of model ids from an OpenAI-compatible /models endpoint."""
    url = endpoint_url.rstrip("/").removesuffix("/v1") + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        models = [m["id"] for m in data.get("data", [])]
        return models
    except Exception as e:
        print(
            f"!!! Error fetching models from {url}: {e}",
            file=sys.stderr,
        )
        return []


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
        ep_edit_format = ep.get("edit_format", "whole")
        if not models:
            # No [[endpoints.models]] defined — fetch from the endpoint's
            # /v1/models endpoint and run for every model that is configured.
            print(
                f">>> No [[endpoints.models]] for {ep_name!r}; fetching models from endpoint...",
                file=sys.stderr,
            )
            models = fetch_models_from_endpoint(url, api_key)
            if not models:
                raise SystemExit(
                    f"Config error: endpoint {ep_name!r} returned no models (or fetch failed). "
                    f"Either populate [[endpoints.models]] in the TOML or ensure the endpoint is reachable."
                )
            print(
                f"    Found {len(models)} model(s) from {url}",
                file=sys.stderr,
            )

        # Normalize models so each entry is a dict with at least a "name" key.
        # TOML [[endpoints.models]] entries are already dicts; API-fetched models
        # come as plain strings.
        def _normalize_model(m):
            if isinstance(m, str):
                return {"name": m}
            return m

        for mdl in models:
            mdl = _normalize_model(mdl)
            mname = mdl.get("name")
            if not mname:
                raise SystemExit(f"Config error: endpoint {ep_name!r} has model without name")
            malias = mdl.get("alias") or None
            # --model filter matches either the real name or the alias
            if only_model and only_model not in (mname, malias):
                continue
            benches = mdl.get("benchmarks", default_benches)
            model_edit_format = mdl.get("edit_format", ep_edit_format)
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
                        alias=malias,
                        bench=b,
                        edit_format=model_edit_format,
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
    # "smoke" is intentionally absent — handled inline by run_smoke_job().
}


# ---------------------------------------------------------------------------
# Smoke test (inline, no subprocess driver)
# ---------------------------------------------------------------------------
def run_smoke_job(job: Job, output_dir: Path, force_new: bool) -> None:
    """Issue a single tiny chat-completion request and assert a non-empty reply.

    On success, writes ``<output_dir>/<slug>/smoke.ok`` (which also acts as the
    skip marker for subsequent runs unless ``--new`` is passed).  On failure,
    writes ``smoke.fail`` containing the error so the cause is visible without
    digging through logs.
    """
    slug = slugify_model(job.display_name)
    model_dir = output_dir / slug
    model_dir.mkdir(parents=True, exist_ok=True)
    ok_path = model_dir / "smoke.ok"
    fail_path = model_dir / "smoke.fail"

    if not force_new and ok_path.exists():
        print(f"    SKIP smoke: {ok_path} already exists (use --new to re-run)")
        job.returncode = 0
        job.duration_s = 0.0
        return

    # Best-effort: clear any stale failure marker from a previous attempt.
    if fail_path.exists():
        try:
            fail_path.unlink()
        except OSError:
            pass

    url = job.endpoint_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": job.model,
        "messages": [
            {"role": "user", "content": "Say 'pong' and nothing else."},
        ],
        "max_tokens": 16,
        "temperature": 0.0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {job.api_key}",
        },
    )

    start = datetime.now()
    err: str | None = None
    reply: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
        data = json.loads(body)
        choices = data.get("choices") or []
        if not choices:
            err = f"no choices in response: {body[:200]!r}"
        else:
            msg = choices[0].get("message") or {}
            reply = (msg.get("content") or "").strip()
            if not reply:
                err = f"empty reply (raw={body[:200]!r})"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        err = f"HTTP {e.code}: {e.reason}{(' — ' + detail) if detail else ''}"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    job.duration_s = (datetime.now() - start).total_seconds()

    if err:
        job.returncode = 1
        job.error = err
        fail_path.write_text(
            json.dumps({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "endpoint": job.endpoint_url,
                "model": job.model,
                "error": err,
                "duration_s": job.duration_s,
            }, indent=2) + "\n",
        )
        print(f"    !!! smoke FAIL: {err}", file=sys.stderr)
        return

    job.returncode = 0
    ok_path.write_text(
        json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "endpoint": job.endpoint_url,
            "model": job.model,
            "reply": reply,
            "duration_s": job.duration_s,
        }, indent=2) + "\n",
    )
    print(f"    OK smoke: reply={reply!r} ({job.duration_s:.2f}s) -> {ok_path}")


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
    # Inline benches: skip the subprocess-driver path entirely.
    if job.bench == "smoke":
        label = f"{job.alias} ({job.model})" if job.alias else job.model
        print(f"\n>>> [{job.endpoint_name}] {label} :: smoke")
        print(f"    POST {job.endpoint_url.rstrip('/')}/chat/completions  model={job.model}")
        if dry_run:
            job.returncode = 0
            job.duration_s = 0.0
            return
        try:
            run_smoke_job(job, output_dir, force_new=force_new)
        except KeyboardInterrupt:
            job.returncode = 130
            job.error = "interrupted"
            raise
        return

    driver = resolve_driver(job.bench, script_dir)
    argv = driver + [
        "--endpoint", job.endpoint_url,
        "--api-key", job.api_key,
        "--model", job.model,
        "--output-dir", str(output_dir),
    ]
    # When an alias is configured, drive the per-model output directory name
    # off the alias instead of the (often ugly) real model id.  All four
    # driver scripts accept --run-name and use it as the dir suffix.
    if job.alias:
        argv.extend(["--run-name", job.alias])
    if force_new:
        argv.append("--new")
    if job.bench == "aider" and job.edit_format != "whole":
        argv.extend(["--edit-format", job.edit_format])
    pretty = " ".join(argv)
    label = f"{job.alias} ({job.model})" if job.alias else job.model
    print(f"\n>>> [{job.endpoint_name}] {label} :: {job.bench}")
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
            f"{j.endpoint_name:<20.20} | {j.display_name:<35.35} | {j.bench:<14} | {rc:<3} | {dur:<8} | {status}"
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
        "--edit-format",
        default="whole",
        help="Edit format for aider benchmark (default: whole). When not 'whole', the format is appended to the output file name.",
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
        label = f"{j.alias} ({j.model})" if j.alias else j.model
        print(f"    - [{j.endpoint_name}] {label} :: {j.bench}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # CLI --edit-format overrides the TOML default for all aider jobs.
    if args.edit_format != "whole":
        for job in plan.jobs:
            if job.bench == "aider":
                job.edit_format = args.edit_format

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
