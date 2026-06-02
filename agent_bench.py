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

from openai import OpenAI
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
    json_valid: bool = False
    math_conversion_correct: bool = False
    turn_2_tps: float = 0.0
    final_success: bool = False
    error: str | None = None


# =====================================================================
# Benchmark runner
# =====================================================================
def run_agent_benchmark(client: OpenAI, model_name: str, log) -> Metrics:
    def out(msg: str = "") -> None:
        print(msg)
        log.write(msg + "\n")
        log.flush()

    out(f"\n{'=' * 60}\nRUNNING BENCHMARK: {model_name}\n{'=' * 60}")

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
    m = Metrics()

    # -- Turn 1 -------------------------------------------------------
    out("[Turn 1] Requesting Structured Tool Call...")
    start = time.time()
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=history,
            temperature=0.0,
            max_tokens=200,
        )
        duration = time.time() - start
        raw_output = (response.choices[0].message.content or "").strip()
        tokens_gen = response.usage.completion_tokens if response.usage else 0
        m.turn_1_tps = round(tokens_gen / duration, 2) if duration > 0 else 0.0

        out(f" -> Received raw response in {duration:.2f}s ({m.turn_1_tps} tokens/sec)")
        out(f" -> Raw Output: {raw_output}")

        cleaned = raw_output.replace("```json", "").replace("```", "").strip()
        tool_call = ToolCallSchema.model_validate_json(cleaned)
        m.json_valid = True
        out(" -> Step 1 Success: Valid JSON generated.")

        if abs(tool_call.mass_kg - 4.5) < 1e-4 and abs(tool_call.volume_m3 - 0.0005) < 1e-6:
            m.math_conversion_correct = True
            out(" -> Step 2 Success: Accurate unit conversions.")
        else:
            out(
                f" -> Step 2 Fail: Incorrect parameters parsed "
                f"(Mass: {tool_call.mass_kg}, Vol: {tool_call.volume_m3})"
            )
    except ValidationError as e:
        m.error = f"validation: {e}"
        out(f" -> Step 1 Fail: Broken JSON or missing fields.\nError: {e}")
        return m
    except Exception as e:
        m.error = f"api: {e}"
        out(f" -> API connection error: {e}")
        return m

    # -- Turn 2 -------------------------------------------------------
    out("\n[Turn 2] Executing tool locally and feeding observation back to model...")
    tool_observation = mock_calculate_density_tool(tool_call.mass_kg, tool_call.volume_m3)
    out(f" -> Tool Output: {tool_observation}")

    history.append({"role": "assistant", "content": cleaned})
    history.append(
        {
            "role": "user",
            "content": f"Tool Observation: {tool_observation}. Summarize the findings for the engineering team.",
        }
    )

    start = time.time()
    try:
        response_t2 = client.chat.completions.create(
            model=model_name,
            messages=history,
            temperature=0.3,
            max_tokens=300,
        )
        duration = time.time() - start
        tokens_gen = response_t2.usage.completion_tokens if response_t2.usage else 0
        m.turn_2_tps = round(tokens_gen / duration, 2) if duration > 0 else 0.0
        final_summary = (response_t2.choices[0].message.content or "").strip()
        out(f" -> Final Summary Received ({m.turn_2_tps} tokens/sec):")
        out(f"\n{final_summary}\n")

        if "9000" in final_summary:
            m.final_success = True
            out(" -> Step 3 Success: State maintained through final calculation summary.")
        else:
            out(" -> Step 3 Fail: Final answer didn't contextualize the tool results correctly.")
    except Exception as e:
        m.error = (m.error + " | " if m.error else "") + f"turn2: {e}"
        out(f" -> Turn 2 failed: {e}")

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

        with log_path.open("w") as log:
            m = run_agent_benchmark(client, model, log)
        metrics_path.write_text(json.dumps(asdict(m), indent=2) + "\n")
        aggregated[model] = asdict(m)
        print(f">>> {model}: run dir {run_dir}")

        # On a successful run (no API/validation failure tripping `error`),
        # symlink <output_root>/<slug>/agent-bench.json -> this run's metrics
        # so the per-model dir always exposes the latest metrics.
        if m.error is None:
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
    lines.append("=" * 75)
    lines.append("FINAL BENCHMARK SCOREBOARD")
    lines.append("=" * 75)
    lines.append(f"{'Model Name':<40} | {'T1 TPS':<7} | {'JSON?':<5} | {'Math?':<5} | {'Final?':<6}")
    lines.append("-" * 75)
    for model, m in aggregated.items():
        lines.append(
            f"{model:<40} | {m['turn_1_tps']:<7} | {str(m['json_valid']):<5} | "
            f"{str(m['math_conversion_correct']):<5} | {str(m['final_success']):<6}"
        )
    text = "\n".join(lines) + "\n"
    (board_dir / "scoreboard.txt").write_text(text)
    print("\n" + text)
    print(f">>> Scoreboard: {board_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
