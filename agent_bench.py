#!/usr/bin/env python3
"""Agent benchmark: structured tool-call + unit-conversion + summary.

Each --model is run independently.  Per-model metrics land in
   ./benchmarks/<model_slug>/agent-bench/<ts>/metrics.json
plus a meta.json sidecar, run.log mirror and an aggregated
   ./benchmarks/_agent-bench-scoreboards/<ts>/scoreboard.{json,txt}
that summarises every model in this invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI, APIStatusError
from pydantic import BaseModel, Field, ValidationError


# =====================================================================
# Structured output schema (the "tool" definition the model must emit)
# =====================================================================
class ToolCallSchema(BaseModel):
    tool_name: str = Field(description="Must be exactly 'calculate_density'")
    mass_kg: float = Field(description="The mass extracted from text, converted to kg")
    volume_m3: float = Field(description="The volume extracted from text, converted to cubic meters")


# =====================================================================
# Mock environment / tools
# =====================================================================
def mock_calculate_density_tool(mass: float, volume: float) -> str:
    if volume <= 0:
        return "Error: Volume must be greater than 0."
    density = mass / volume
    return f"Success: Calculated density is {density:.2f} kg/m3."


# =====================================================================
# Helpers
# =====================================================================
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]")


def slugify_model(model: str) -> str:
    return _SLUG_RE.sub("-", model.replace("/", "_").replace(":", "-"))


def git_rev(repo_root: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return None


@dataclass
class Metrics:
    turn_1_tps: float = 0.0
    json_valid_rate: float = 0.0        # fraction of runs where JSON parsed successfully (0.0–1.0)
    tool_name_ok_rate: float = 0.0      # fraction of runs where tool_name == 'calculate_density'
    math_conversion_rate: float = 0.0   # fraction of runs with correct unit conversions
    turn_2_tps: float = 0.0
    final_success_rate: float = 0.0     # fraction of runs with correct final summary
    deterministic: bool = True          # True when every binary metric is all-pass or all-fail (zero variance)
    runs: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


# =====================================================================
# Benchmark runner — single trial
# =====================================================================
_TrialResult = dict[str, Any]  # keys: turn_1_tps, json_valid, tool_name_ok, math_ok, turn_2_tps, final_ok, error


def _run_single_trial(client: OpenAI, model_name: str, trial: int, out, temperature: float = 0.3) -> _TrialResult:
    """Run one trial and return a dict with per-trial measurements."""
    result: _TrialResult = {
        "turn_1_tps": 0.0,
        "json_valid": False,
        "tool_name_ok": False,
        "math_ok": False,
        "turn_2_tps": 0.0,
        "final_ok": False,
        "error": None,
    }

    system_prompt = (
        "You are an agentic core framework. You must ONLY respond in a raw, valid JSON object matching "
        "the provided schema. Do not include markdown code blocks like ```json. Do not talk. "
        "Schema:\n" + json.dumps(ToolCallSchema.model_json_schema(), indent=2)
    )
    user_prompt = (
        "We need to find the density of the new alloy sample. The lab report states "
        "the mass is 4500 grams, and it displaces exactly 0.5 liters of water. "
        "Trigger the calculate_density tool with the correct SI units (kg and m3)."
    )

    history: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # -- Turn 1 -------------------------------------------------------
    out(f"  [Trial {trial}][Turn 1] Requesting Structured Tool Call...")
    start = time.time()
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=history,
            temperature=temperature,
            max_tokens=200,
        )
        duration = time.time() - start
        raw_output = (response.choices[0].message.content or "").strip()
        tokens_gen = response.usage.completion_tokens if response.usage else 0
        result["turn_1_tps"] = round(tokens_gen / duration, 2) if duration > 0 else 0.0

        out(f"   -> {duration:.2f}s ({result['turn_1_tps']} tok/s) | Raw: {raw_output[:120]}")

        cleaned = raw_output.replace("```json", "").replace("```", "").strip()
        tool_call = ToolCallSchema.model_validate_json(cleaned)
        result["json_valid"] = True
        out("   -> Step 1 OK: Valid JSON.")

        if tool_call.tool_name == "calculate_density":
            result["tool_name_ok"] = True
            out("   -> Step 1b OK: Correct tool_name.")
        else:
            out(f"   -> Step 1b FAIL: tool_name='{tool_call.tool_name}' (expected 'calculate_density')")

        # Tolerances: ±0.0001 kg for mass (4500 g → 4.5 kg),
        # ±0.0000001 m³ for volume (0.5 L → 0.0005 m³).  Both are generous
        # enough to absorb float round-trip noise from any JSON serialisation.
        if abs(tool_call.mass_kg - 4.5) < 1e-4 and abs(tool_call.volume_m3 - 0.0005) < 1e-7:
            result["math_ok"] = True
            out("   -> Step 2 OK: Correct unit conversions.")
        else:
            out(
                f"   -> Step 2 FAIL: mass={tool_call.mass_kg}, vol={tool_call.volume_m3}"
            )
    except ValidationError as e:
        result["error"] = f"validation: {e}"
        out(f"   -> Step 1 FAIL: {e}")
        return result
    except APIStatusError as e:
        if e.status_code == 500:
            raise
        result["error"] = f"api: {e}"
        out(f"   -> API error: {e}")
        return result
    except Exception as e:
        result["error"] = f"api: {e}"
        out(f"   -> API error: {e}")
        return result

    # -- Turn 2 -------------------------------------------------------
    tool_observation = mock_calculate_density_tool(tool_call.mass_kg, tool_call.volume_m3)
    history.append({"role": "assistant", "content": cleaned})
    history.append(
        {
            "role": "user",
            "content": f"Tool Observation: {tool_observation}. Summarize the findings for the engineering team.",
        }
    )

    out(f"  [Trial {trial}][Turn 2] Feeding observation back...")
    start = time.time()
    try:
        response_t2 = client.chat.completions.create(
            model=model_name,
            messages=history,
            temperature=temperature,
            max_tokens=300,
        )
        duration = time.time() - start
        tokens_gen = response_t2.usage.completion_tokens if response_t2.usage else 0
        result["turn_2_tps"] = round(tokens_gen / duration, 2) if duration > 0 else 0.0
        choice = response_t2.choices[0]
        final_summary = (choice.message.content or "").strip()
        finish_reason = choice.finish_reason
        # Qwen3 thinking models put reasoning in reasoning_content; log it for diagnosis
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        out(f"   -> {result['turn_2_tps']} tok/s | finish={finish_reason} | thinking={len(reasoning)}chars | Summary: {final_summary[:120]}")

        # Extract all numbers from the summary (strip commas used as thousand-separators)
        # and check whether any of them equals the expected density of 9000 kg/m³.
        found_nums = [
            float(n.replace(",", ""))
            for n in re.findall(r"[\d,]+(?:\.\d+)?", final_summary)
            if n.replace(",", "").replace(".", "").isdigit()
        ]
        if any(abs(v - 9000.0) < 0.5 for v in found_nums):
            result["final_ok"] = True
            out("   -> Step 3 OK: Final answer correct (9000 kg/m³ found).")
        else:
            out(f"   -> Step 3 FAIL: 9000 not found in summary numbers {found_nums[:10]}.")
    except APIStatusError as e:
        if e.status_code == 500:
            raise
        result["error"] = (result["error"] + " | " if result["error"] else "") + f"turn2: {e}"
        out(f"   -> Turn 2 failed: {e}")
    except Exception as e:
        result["error"] = (result["error"] + " | " if result["error"] else "") + f"turn2: {e}"
        out(f"   -> Turn 2 failed: {e}")

    return result


# =====================================================================
# Benchmark runner — 10-trial aggregator
# =====================================================================
NUM_TRIALS = 10


def run_agent_benchmark(client: OpenAI, model_name: str, log, num_trials: int = NUM_TRIALS) -> Metrics:
    def out(msg: str = "") -> None:
        print(msg)
        log.write(msg + "\n")
        log.flush()

    out(f"\n{'=' * 60}\nRUNNING BENCHMARK: {model_name} ({num_trials} trials)\n{'=' * 60}")

    trials: list[_TrialResult] = []
    for i in range(1, num_trials + 1):
        temp = 0.0 if i == 1 else 0.3
        out(f"\n--- Trial {i}/{num_trials} (temp={temp}) ---")
        t = _run_single_trial(client, model_name, i, out, temperature=temp)
        t["temperature"] = temp
        trials.append(t)

    # Aggregate — only include trials that actually produced tokens in TPS averages
    # so that API failures don't suppress the reported speed.
    n = len(trials)
    t1_with_tokens = [t for t in trials if t["turn_1_tps"] > 0]
    t2_with_tokens = [t for t in trials if t["turn_2_tps"] > 0]

    # A metric is "variant" if it is neither all-True nor all-False across trials.
    # If every binary metric is trivially uniform the backend is likely deterministic
    # at temp=0 and the rates carry no more information than a single trial would.
    binary_keys = ("json_valid", "tool_name_ok", "math_ok", "final_ok")
    is_deterministic = all(
        len(set(t[k] for t in trials)) == 1
        for k in binary_keys
    )

    m = Metrics(
        turn_1_tps=round(
            sum(t["turn_1_tps"] for t in t1_with_tokens) / max(1, len(t1_with_tokens)), 2
        ),
        json_valid_rate=round(sum(1 for t in trials if t["json_valid"]) / n, 3),
        tool_name_ok_rate=round(sum(1 for t in trials if t["tool_name_ok"]) / n, 3),
        math_conversion_rate=round(sum(1 for t in trials if t["math_ok"]) / n, 3),
        turn_2_tps=round(
            sum(t["turn_2_tps"] for t in t2_with_tokens) / max(1, len(t2_with_tokens)), 2
        ),
        final_success_rate=round(sum(1 for t in trials if t["final_ok"]) / n, 3),
        deterministic=is_deterministic,
        runs=n,
        errors=[t["error"] for t in trials if t["error"]],
    )

    out(f"\n--- Aggregated over {n} trials ---")
    out(f"  Avg Turn-1 TPS        : {m.turn_1_tps}  (over {len(t1_with_tokens)} trials with tokens)")
    out(f"  JSON valid rate       : {m.json_valid_rate * 100:.0f}%")
    out(f"  Tool-name correct rate: {m.tool_name_ok_rate * 100:.0f}%")
    out(f"  Math conversion rate  : {m.math_conversion_rate * 100:.0f}%")
    out(f"  Avg Turn-2 TPS        : {m.turn_2_tps}  (over {len(t2_with_tokens)} trials with tokens)")
    out(f"  Final success rate    : {m.final_success_rate * 100:.0f}%")
    if m.deterministic:
        out("  Variance              : none — backend appears deterministic at temp=0 (rates are binary)")
    else:
        out("  Variance              : detected — backend is non-deterministic despite temp=0")
    if m.errors:
        out(f"  Errors ({len(m.errors)}): {m.errors}")

    return m


# =====================================================================
# CLI
# =====================================================================
def list_models_via_api(endpoint: str, api_key: str) -> None:
    import urllib.request

    url = endpoint.rstrip("/").removesuffix("/v1") + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        for m in data.get("data", []):
            print(m.get("id", "?"))
    except Exception as e:
        print(f"Error fetching models from {url}: {e}", file=sys.stderr)
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a simple agentic structured-output benchmark against one or more models."
    )
    p.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL ending in /v1")
    p.add_argument(
        "--model",
        action="append",
        default=[],
        help="Model id to test (repeatable).  If omitted, lists models from --endpoint.",
    )
    p.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", "EMPTY"))
    p.add_argument("--output-dir", default="./benchmarks", help="Root output directory (default: ./benchmarks)")
    p.add_argument("--run-name", default=None, help="Suffix for per-model run dirs (default: model slug)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not args.model:
        print(f">>> No --model specified. Available models from {args.endpoint}/models:\n")
        list_models_via_api(args.endpoint, args.api_key)
        print("\nRe-run with --model <name> (repeatable) to start the benchmark.")
        return 0

    repo_root = Path(__file__).resolve().parent
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rev = git_rev(repo_root)
    host = os.uname().nodename

    client = OpenAI(base_url=args.endpoint, api_key=args.api_key)

    aggregated: dict[str, dict[str, Any]] = {}

    for model in args.model:
        slug = slugify_model(model)
        run_dir = output_root / slug / "agent-bench" / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "run.log"
        meta_path = run_dir / "meta.json"
        metrics_path = run_dir / "metrics.json"
        cmd_path = run_dir / "run.cmd"

        meta = {
            "timestamp": ts,
            "host": host,
            "git_rev": rev,
            "bench": "agent-bench",
            "model": model,
            "endpoint": args.endpoint,
            "run_name": args.run_name or slug,
        }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")

        # Record a replayable single-model invocation of this script.
        cmd_argv = [
            sys.executable, str(Path(__file__).resolve()),
            "--endpoint", args.endpoint,
            "--api-key", args.api_key,
            "--output-dir", str(output_root),
            "--model", model,
        ]
        if args.run_name:
            cmd_argv += ["--run-name", args.run_name]
        cmd_path.write_text(
            "#!/usr/bin/env bash\n"
            f"# Replayable command for run {ts}\n"
            + shlex.join(cmd_argv) + "\n"
        )
        cmd_path.chmod(0o755)

        try:
            with log_path.open("w") as log:
                m = run_agent_benchmark(client, model, log)
        except APIStatusError as e:
            print(f"!!! endpoint returned HTTP {e.status_code} for model {model}: {e.message}", file=sys.stderr)
            return 1
        metrics_path.write_text(json.dumps(asdict(m), indent=2) + "\n")
        aggregated[model] = asdict(m)
        print(f">>> {model}: run dir {run_dir}")

        # On a successful run (no API/validation failure tripping `error`),
        # symlink <output_root>/<slug>/agent-bench.json -> this run's metrics
        # so the per-model dir always exposes the latest metrics.
        if not m.errors:
            latest = output_root / slug / "agent-bench.json"
            try:
                if latest.is_symlink() or latest.exists():
                    latest.unlink()
                latest.symlink_to(metrics_path)
            except OSError as e:
                print(f"!!! warning: failed to update {latest}: {e}", file=sys.stderr)

    # Aggregated scoreboard for this invocation.
    board_dir = output_root / "_agent-bench-scoreboards" / ts
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "scoreboard.json").write_text(json.dumps(aggregated, indent=2) + "\n")

    lines = []
    lines.append("=" * 90)
    lines.append("FINAL BENCHMARK SCOREBOARD")
    lines.append("=" * 90)
    lines.append(
        f"{'Model Name':<40} | {'T1 TPS':<7} | {'JSON%':<6} | {'Tool%':<6} | {'Math%':<6} | {'Final%':<7} | {'Runs'}"
    )
    lines.append("-" * 90)
    for model, m in aggregated.items():
        json_pct = f"{m['json_valid_rate'] * 100:.0f}%"
        tool_pct = f"{m['tool_name_ok_rate'] * 100:.0f}%"
        math_pct = f"{m['math_conversion_rate'] * 100:.0f}%"
        final_pct = f"{m['final_success_rate'] * 100:.0f}%"
        det_flag = " [det]" if m.get("deterministic", True) else " [var]"
        lines.append(
            f"{model:<40} | {m['turn_1_tps']:<7} | {json_pct:<6} | {tool_pct:<6} | {math_pct:<6} | {final_pct:<7} | {m['runs']}{det_flag}"
        )
    text = "\n".join(lines) + "\n"
    (board_dir / "scoreboard.txt").write_text(text)
    print("\n" + text)
    print(f">>> Scoreboard: {board_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
