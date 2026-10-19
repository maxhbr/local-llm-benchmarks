#!/usr/bin/env python3
"""Run a pinned llmprobe release with the benchmark harness's driver interface."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any

from run_benchmarks import slugify_model
from lib.llmprobe_report import PHASES, read_json, validate_report

LLMPROBE_VERSION = "0.6.17"


def positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default=os.environ.get("LLMPROBE_API_KEY", "EMPTY"))
    parser.add_argument("--output-dir", type=Path, default=Path("./benchmarks"))
    parser.add_argument("--work-dir", type=Path, default=Path("./work"))
    parser.add_argument("--run-name", help="Alias used for the model output directory")
    parser.add_argument("--new", action="store_true", help="Ignore a matching completed result")
    depth = parser.add_mutually_exclusive_group()
    depth.add_argument("--quick", action="store_true", help="Short engine compatibility check")
    depth.add_argument("--full", action="store_true", help="Include slow/expanded checks")
    parser.add_argument("--bench", action="store_true", help="Also run upstream performance tests")
    parser.add_argument("--budget", type=positive, help="Upstream reported-token budget (not a billing limit)")
    parser.add_argument("--timeout", type=positive, default=60, help="Upstream request timeout, seconds")
    parser.add_argument("--run-timeout", type=positive, default=3600, help="Whole-process deadline, seconds")
    parser.add_argument("--reasoning", default="medium", help="Upstream reasoning effort")
    parser.add_argument("--concurrency", type=positive, default=1)
    parser.add_argument("--llmprobe-bin", help="Use an existing llmprobe executable instead of the pinned npm install")
    return parser.parse_args(argv)


def write_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def load_report(path: Path) -> dict[str, Any]:
    """Reject missing/truncated output before publishing it as a latest result."""
    report = read_json(path)
    validate_report(report)
    if report["version"] != 2:
        raise ValueError("expected a native llmprobe version 2 report")
    scope = report.get("run", {})
    for key in ("depth", "mode", "startedAt", "phases"):
        if key not in scope:
            raise ValueError(f"report missing run.{key}")
    for phase in PHASES:
        if "status" not in scope["phases"].get(phase, {}):
            raise ValueError(f"report missing run.phases.{phase}.status")
    return report


def report_status(report: dict[str, Any], rc: int) -> str:
    if report.get("incomplete") or report.get("reasoning", {}).get("aborted"):
        return "incomplete"
    scope = report["run"]
    if scope.get("budget", {}).get("exhausted"):
        return "budget-exhausted"
    # "partial" can be intentional scope (quick checks or unprobed features),
    # not an interrupted execution. Preserve that scope in the native report.
    for name, phase in scope["phases"].items():
        status = phase.get("status")
        expected_partial = name == "coverage" or (name == "conformance" and scope["depth"] == "quick")
        if status in ("interrupted", "failed") or (status == "partial" and not expected_partial):
            return "incomplete"
    return {0: "complete", 1: "conformance-failed"}.get(rc, "failed")


def cached_result(latest: Path, signature: dict[str, Any]) -> int | None:
    if not latest.is_file():
        return None
    try:
        report = load_report(latest)
        meta = json.loads((latest.resolve().parent / "meta.json").read_text())
        rc = meta["returncode"]
        if (meta.get("signature") == signature and rc in (0, 1)
                and report["target"]["model"] == signature["model"]
                and meta.get("status") in ("complete", "conformance-failed")
                and report_status(report, rc) in ("complete", "conformance-failed")):
            # Compatibility findings are benchmark results, not execution errors.
            # This also handles histories written before driver rc normalization.
            return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def resolve_tool(args: argparse.Namespace, log: Any) -> list[str]:
    if args.llmprobe_bin:
        executable = shutil.which(args.llmprobe_bin)
        if executable is None:
            raise RuntimeError(f"llmprobe executable not found: {args.llmprobe_bin}")
        return [str(Path(executable).resolve())]
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js >=22.19.0 is required. Use nix develop or install Node.js.")
    version = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
    if tuple(int(v) for v in version.split(".")[:3]) < (22, 19, 0):
        raise RuntimeError(f"Node.js >=22.19.0 is required (found {version}).")
    prefix = args.work_dir.resolve() / "llmprobe" / LLMPROBE_VERSION
    package = prefix / "node_modules" / "llmprobe"
    entry = package / "bin" / "dist" / "llmprobe.mjs"
    installed = False
    try:
        installed = entry.is_file() and json.loads((package / "package.json").read_text())["version"] == LLMPROBE_VERSION
    except (OSError, ValueError, KeyError):
        pass
    if not installed:
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required for the first llmprobe installation.")
        prefix.mkdir(parents=True, exist_ok=True)
        print(f">>> Installing llmprobe@{LLMPROBE_VERSION} in {prefix}", flush=True)
        subprocess.run([npm, "install", "--prefix", str(prefix), "--no-save", "--package-lock=false",
                        "--ignore-scripts", "--no-audit", "--no-fund", f"llmprobe@{LLMPROBE_VERSION}"],
                       check=True, stdout=log, stderr=subprocess.STDOUT, timeout=300)
        if not entry.is_file():
            raise RuntimeError("npm installation did not produce the llmprobe executable")
    return [node, str(entry)]


def stop_process(proc: subprocess.Popen) -> None:
    """Stop the entire process group, including children from an external driver."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run(args: argparse.Namespace) -> int:
    name = args.run_name or args.model
    slug = slugify_model(name)
    if slug in ("", ".", ".."):
        raise ValueError("model or run name must produce a nonempty directory name")
    model_dir = args.output_dir.resolve() / slug
    latest = model_dir / "llmprobe.json"
    signature = {
        "endpoint": args.endpoint, "model": args.model,
        "llmprobe_version": None if args.llmprobe_bin else LLMPROBE_VERSION,
        "llmprobe_bin": args.llmprobe_bin,
        "depth": "quick" if args.quick else "full" if args.full else "default",
        "performance": args.bench, "budget": args.budget, "timeout": args.timeout,
        "run_timeout": args.run_timeout, "reasoning": args.reasoning, "concurrency": args.concurrency,
    }
    cached = None if args.new else cached_result(latest, signature)
    if cached is not None:
        print(f">>> SKIP llmprobe: {latest} matches this configuration (use --new to rerun)")
        return cached
    started = datetime.now(timezone.utc)
    # Microseconds prevent repeated --new invocations in one second from colliding.
    run_dir = model_dir / "llmprobe" / started.strftime("%Y%m%d-%H%M%S-%f")
    run_dir.mkdir(parents=True)
    report_path = run_dir / "result.json"
    meta = {
        "timestamp": started.isoformat(), "host": socket.gethostname(), "bench": "llmprobe",
        "endpoint": args.endpoint, "model": args.model, "run_name": name,
        "llmprobe_version": signature["llmprobe_version"], "depth": signature["depth"],
        "performance": args.bench, "signature": signature, "status": "failed", "returncode": None,
    }
    write_json(run_dir / "meta.json", meta)
    print(f">>> llmprobe {name}: log {run_dir / 'run.log'}", flush=True)
    start = time.monotonic()
    rc = 1
    with (run_dir / "run.log").open("w", encoding="utf-8") as log:
        try:
            argv = resolve_tool(args, log) + [args.endpoint, "--model", args.model, "--no-save", "--no-color",
                    "--save", str(report_path), "--timeout", str(args.timeout),
                    "--reasoning", args.reasoning, "--concurrency", str(args.concurrency)]
            if not args.bench:
                argv.append("--no-bench")
            if args.quick:
                argv.append("--quick")
            if args.full:
                argv.append("--full")
            if args.budget is not None:
                argv.extend(["--budget", str(args.budget)])
            # The key is supplied via the environment, never saved in artifacts.
            replay = '#!/usr/bin/env bash\n# Set LLMPROBE_API_KEY before replaying an authenticated run.\n'
            replay += 'export LLMPROBE_API_KEY="${LLMPROBE_API_KEY:-EMPTY}"\n'
            replay += shlex.join(argv) + "\n"
            (run_dir / "run.cmd").write_text(replay)
            env = dict(os.environ, LLMPROBE_API_KEY=args.api_key)
            with subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True) as proc:
                try:
                    rc = proc.wait(timeout=args.run_timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    stop_process(proc)
                    raise
            meta["returncode"] = rc
            report = load_report(report_path)
            if report["target"]["model"] != args.model:
                raise ValueError("report target.model differs from the requested model")
            meta["status"] = report_status(report, rc)
            if meta["status"] in ("complete", "conformance-failed"):
                # Upstream rc=1 gates on MUST assertions. A validated completed
                # report is still a successful benchmark execution for this harness.
                rc = 0
            elif rc == 0:
                rc = 1
        except subprocess.TimeoutExpired as exc:
            meta.update(status="timeout", error=str(exc))
            rc = 124
        except KeyboardInterrupt:
            meta.update(status="interrupted", error="interrupted")
            rc = 130
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TypeError, AttributeError) as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            rc = rc or 1
        finally:
            meta["driver_returncode"] = rc
            meta["duration_s"] = round(time.monotonic() - start, 3)
            write_json(run_dir / "meta.json", meta)
    if meta["status"] in ("complete", "conformance-failed"):
        # A failed rerun must not replace an earlier complete result.
        temp_link = model_dir / (".llmprobe-" + run_dir.name)
        temp_link.symlink_to(report_path.relative_to(model_dir))
        temp_link.replace(latest)
    print(f">>> llmprobe {meta['status']} (driver rc={rc}, upstream rc={meta['returncode']}): {run_dir}", flush=True)
    if meta.get("error"):
        print(f"Error: {meta['error']}", file=sys.stderr)
    return rc


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
