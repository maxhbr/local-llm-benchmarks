#!/usr/bin/env bash
# Driver for the tool-eval-bench agentic tool-call benchmark.
#
# Wraps https://github.com/SeraphimSerapis/tool-eval-bench — a tool-call *quality*
# benchmark that runs 69 deterministic multi-turn scenarios (15 with --short,
# 84 with --hardmode) through an OpenAI-compatible /chat/completions endpoint,
# scoring each as pass/partial/fail.  Optionally also runs the integrated
# llama-bench-style throughput sweep (--perf / --perf-only), matching the
# workflow in https://gist.github.com/PierpaoloPernici/f1d1382f8e357b4faffb1a9f584cc1df
#
# tool-eval-bench is installed (and upgraded) on demand via `uv tool install`,
# the same mechanism terminal-bench uses for harbor.  The host environment is
# expected to provide python3, uv, git and curl (the flake's devShell does so).
#
# Unlike the repo's agent_bench.py (which tests structured JSON output), this
# benchmark exercises the real OpenAI `tools` API, so the serving endpoint must
# support tool calls (e.g. vLLM --enable-auto-tool-choice, llama.cpp's tool
# support, LiteLLM with a tool-call parser).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

TOOL_EVAL_BENCH_REPO="https://github.com/SeraphimSerapis/tool-eval-bench.git"
# [perf] bundles llama-benchy so --perf throughput works out of the box.
TOOL_EVAL_BENCH_SPEC="tool-eval-bench[perf] @ ${TOOL_EVAL_BENCH_REPO}"
BENCH_TAG="tool-eval-bench"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run the tool-eval-bench agentic tool-call benchmark against a local
OpenAI-compatible LLM endpoint.  tool-eval-bench is installed via uv on first
run into work/ (shared with terminal-bench's uv-tools dir).

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
                       tool-eval-bench accepts both http://host:port and .../v1 forms.
  --model <name>       Model name as recognized by the endpoint
                       If omitted, lists available models from the endpoint

Common options:
  --api-key <key>           API key (default: EMPTY)
  --output-dir <path>       Root directory for results (default: ./benchmarks)
  --work-dir <path>         Directory for the cached uv tool install (default: ./work)
  --run-name <name>         Name suffix for this run (default: derived from --model)
  --rebuild                 Reinstall tool-eval-bench before running
  --update                  uv tool upgrade tool-eval-bench, then run
  --shell-only              Drop into a shell with tool-eval-bench on PATH; don't run
  --new                     Re-run even if the output symlink already exists
  -h, --help                Show this help message

Scenario selection (tool-call quality suite):
  --short                   Run only the core 15 scenarios (fast, ~minutes)
  --hardmode                Include Hard Mode (84 scenarios total)
  --hardmode-only           Run ONLY Hard Mode (Category P) scenarios
  --full                    Explicitly run the full 69-scenario standard suite (the default)
  --categories <A B ...>    Run only these category letters (A–O, P for hard mode)
  --scenarios <TC-01 ..>    Run only these scenario IDs

Run control:
  --seed <n>                Random seed (default: 42 — deterministic, like the gist)
  --trials <n>              Number of trial runs for statistical rigor (default: 1)
  --parallel <n>            Run N scenarios concurrently (default: 1)
  --temperature <f>         Sampling temperature (default: 0.0)
  --no-think                Disable thinking/reasoning (enable_thinking=false)
  --timeout <s>             Per-request timeout in seconds (default: tool-eval-bench 120)
  --max-turns <n>           Max turns per scenario (default: 8)
  --no-preflight            Skip the strict model availability pre-flight check
  --no-warmup               Skip the server warm-up request
  --backend <name>          Backend label for reports: vllm, litellm, llamacpp, sglang
                            (default: auto-detected via /metrics, /version)
  --no-probe-engine         Skip inference engine probing (no /version, /health calls)
  --fail-on-safety          Exit status 2 when safety-critical scenarios fail
  --weight-by-difficulty   Weight scenario scores by difficulty tier
  --label <text>            Free-form annotation attached to every report

Throughput benchmark (llama-bench-style, via bundled llama-benchy):
  --perf                    Run throughput sweep before tool-call scenarios
  --perf-only               Run ONLY the throughput sweep (skip tool-call scenarios)
  --depth <0,4096,8192>     Context depths, comma separated (default: 0,4096,8192)
  --concurrency <1,2,4>     Concurrency levels, comma separated (default: 1,2,4)
  --pp <n>                  Prompt tokens (default: 2048)
  --tg <n>                  Generation tokens (default: 128)
  --benchy-runs <n>         Measurement runs per test point (default: 3)
  --benchy-latency-mode <m> Latency mode: api, generation, none (default: generation)
  --tokenizer <path>        Local tokenizer.json/dir for prompt construction (--perf)

Passthrough:
  --extra-args <args>       Extra args passed verbatim to tool-eval-bench
                            (e.g. "--gsm8k", "--spec-bench", "--context-pressure 0.75",
                             "--backend-kwargs '{...}'")

Examples:
  # Full 69-scenario standard suite (the default)
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-qwen-model

  # Quick 15-scenario check
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-model --short

  # Quality + throughput (the gist's combined workflow)
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-model --perf

  # Throughput only
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-model --perf-only

  # Safety + tool-selection categories only
  $(basename "$0") -e http://localhost:8080/v1 -m my-model --categories K A
EOF
    exit 0
}

ENDPOINT=""
MODEL=""
API_KEY="EMPTY"
OUTPUT_DIR="./benchmarks"
WORK_DIR="./work"
RUN_NAME=""
SHORT=false
HARDMODE=false
HARDMODE_ONLY=false
FULL=false
CATEGORIES=""
SCENARIOS=""
SEED=42
TRIALS=""
PARALLEL=""
TEMPERATURE=""
NO_THINK=false
TIMEOUT=""
MAX_TURNS=""
NO_PREFLIGHT=false
NO_WARMUP=false
BACKEND=""
NO_PROBE_ENGINE=false
FAIL_ON_SAFETY=false
WEIGHT_BY_DIFFICULTY=false
LABEL=""
PERF=false
PERF_ONLY=false
DEPTH=""
CONCURRENCY=""
PP=""
TG=""
BENCHY_RUNS=""
BENCHY_LATENCY_MODE=""
TOKENIZER=""
EXTRA_ARGS=""
REBUILD=false
UPDATE=false
SHELL_ONLY=false
NEW=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint)                ENDPOINT="$2"; shift 2 ;;
        --model)                   MODEL="$2"; shift 2 ;;
        --api-key)                 API_KEY="$2"; shift 2 ;;
        --output-dir)              OUTPUT_DIR="$2"; shift 2 ;;
        --work-dir)                WORK_DIR="$2"; shift 2 ;;
        --run-name)                RUN_NAME="$2"; shift 2 ;;
        --short)                   SHORT=true; shift ;;
        --hardmode)                HARDMODE=true; shift ;;
        --hardmode-only)           HARDMODE_ONLY=true; shift ;;
        --full)                    FULL=true; shift ;;
        --categories)              CATEGORIES="$2"; shift 2 ;;
        --scenarios)               SCENARIOS="$2"; shift 2 ;;
        --seed)                    SEED="$2"; shift 2 ;;
        --trials)                  TRIALS="$2"; shift 2 ;;
        --parallel)                PARALLEL="$2"; shift 2 ;;
        --temperature)             TEMPERATURE="$2"; shift 2 ;;
        --no-think)                NO_THINK=true; shift ;;
        --timeout)                 TIMEOUT="$2"; shift 2 ;;
        --max-turns)               MAX_TURNS="$2"; shift 2 ;;
        --no-preflight)            NO_PREFLIGHT=true; shift ;;
        --no-warmup)               NO_WARMUP=true; shift ;;
        --backend)                 BACKEND="$2"; shift 2 ;;
        --no-probe-engine)         NO_PROBE_ENGINE=true; shift ;;
        --fail-on-safety)          FAIL_ON_SAFETY=true; shift ;;
        --weight-by-difficulty)    WEIGHT_BY_DIFFICULTY=true; shift ;;
        --label)                   LABEL="$2"; shift 2 ;;
        --perf)                    PERF=true; shift ;;
        --perf-only)               PERF_ONLY=true; shift ;;
        --depth)                   DEPTH="$2"; shift 2 ;;
        --concurrency)             CONCURRENCY="$2"; shift 2 ;;
        --pp)                      PP="$2"; shift 2 ;;
        --tg)                      TG="$2"; shift 2 ;;
        --benchy-runs)             BENCHY_RUNS="$2"; shift 2 ;;
        --benchy-latency-mode)     BENCHY_LATENCY_MODE="$2"; shift 2 ;;
        --tokenizer)               TOKENIZER="$2"; shift 2 ;;
        --extra-args)              EXTRA_ARGS="$2"; shift 2 ;;
        --rebuild)                 REBUILD=true; shift ;;
        --update)                  UPDATE=true; shift ;;
        --shell-only)              SHELL_ONLY=true; shift ;;
        --new)                     NEW=true; shift ;;
        -h|--help)                 usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$ENDPOINT" ]]; then
    echo "Error: --endpoint is required" >&2
    usage
fi

if [[ -z "$MODEL" ]]; then
    echo ">>> No --model specified. Available models from $ENDPOINT/models:"
    echo ""
    list_models "$ENDPOINT" "$API_KEY"
    echo ""
    echo "Re-run with --model <name> to start the benchmark."
    exit 0
fi

if [[ -z "$RUN_NAME" ]]; then
    RUN_NAME="$(slugify_model "$MODEL")"
fi

# Skip if the output symlink already exists (unless --new was passed).
if [[ "$NEW" == false && "$SHELL_ONLY" == false ]]; then
    _slug="$(slugify_model "$RUN_NAME")"
    _output_abs="$(cd "$OUTPUT_DIR" 2>/dev/null && pwd || echo "$OUTPUT_DIR")"
    _symlink="$_output_abs/$_slug/${BENCH_TAG}.md"
    if [[ -L "$_symlink" ]]; then
        echo ">>> SKIP [$MODEL] ${BENCH_TAG}: output symlink already exists at $_symlink"
        echo "    (use --new to force a fresh run)"
        exit 0
    fi
fi

# --full is an explicit no-op (it is the default); kept so configs/scripts can
# state their intent.  --short / --hardmode* take precedence when given.
if [[ "$FULL" == true && "$SHORT" == true ]]; then
    echo "Error: --full and --short are mutually exclusive" >&2
    exit 2
fi

for bin in uv python3 git curl; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "Error: '$bin' not found in PATH; use the flake devShell or install it." >&2
        exit 1
    fi
done

mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

# Use a per-project uv tool location (shared with terminal-bench's harbor) so
# the script does not silently depend on whatever the host's ~/.local/bin has.
export UV_TOOL_BIN_DIR="$WORK_DIR/uv-tools/bin"
export UV_TOOL_DIR="$WORK_DIR/uv-tools"
mkdir -p "$UV_TOOL_BIN_DIR"
export PATH="$UV_TOOL_BIN_DIR:$PATH"

TEB_BIN="$UV_TOOL_BIN_DIR/tool-eval-bench"

install_tool() {
    if [[ "$REBUILD" == true ]]; then
        echo ">>> Reinstalling tool-eval-bench via uv..."
        uv tool uninstall tool-eval-bench 2>/dev/null || true
    fi
    if [[ ! -x "$TEB_BIN" ]] || [[ "$REBUILD" == true ]] || [[ "$UPDATE" == true ]]; then
        if [[ "$UPDATE" == true && "$REBUILD" == false ]]; then
            echo ">>> Upgrading tool-eval-bench via uv..."
            uv tool upgrade tool-eval-bench || {
                echo "    upgrade failed (not installed yet?) — installing fresh..."
                uv tool install "$TOOL_EVAL_BENCH_SPEC"
            }
        else
            echo ">>> Installing tool-eval-bench via uv..."
            uv tool install "$TOOL_EVAL_BENCH_SPEC"
        fi
    fi
    if [[ ! -x "$TEB_BIN" ]]; then
        echo "Error: tool-eval-bench not found at $TEB_BIN after install" >&2
        exit 1
    fi
}

install_tool

# Build the tool-eval-bench CLI invocation.  We use the flat legacy form (no
# subcommand) so scenario + throughput flags compose uniformly:
#   default      -> tool-call scenarios (69; --short -> 15; --hardmode -> 84)
#   --perf       -> throughput sweep then tool-call scenarios
#   --perf-only  -> throughput sweep only
TEB_ARGS=(
    --base-url "$ENDPOINT"
    --model    "$MODEL"
    --api-key  "$API_KEY"
    --seed     "$SEED"
    # The driver always pipes through tee (for the run.log), so the Rich
    # live display cannot do cursor-based updates.  --no-live selects the
    # plain one-line-per-scenario path, which is both loggable and readable
    # in a terminal.  Pass --extra-args '--json' / '--json-file <path>' for
    # machine-readable output.
    --no-live
)

[[ "$SHORT" == true ]]          && TEB_ARGS+=( --short )
[[ "$HARDMODE" == true ]]       && TEB_ARGS+=( --hardmode )
[[ "$HARDMODE_ONLY" == true ]]  && TEB_ARGS+=( --hardmode-only )
[[ "$NO_THINK" == true ]]       && TEB_ARGS+=( --no-think )
[[ "$NO_PREFLIGHT" == true ]]   && TEB_ARGS+=( --no-preflight )
[[ "$NO_WARMUP" == true ]]      && TEB_ARGS+=( --no-warmup )
[[ "$FAIL_ON_SAFETY" == true ]] && TEB_ARGS+=( --fail-on-safety )
[[ "$WEIGHT_BY_DIFFICULTY" == true ]] && TEB_ARGS+=( --weight-by-difficulty )
[[ "$NO_PROBE_ENGINE" == true ]]&& TEB_ARGS+=( --no-probe-engine )
[[ "$PERF" == true ]]           && TEB_ARGS+=( --perf )
[[ "$PERF_ONLY" == true ]]      && TEB_ARGS+=( --perf-only )

# --categories / --scenarios are nargs="*" in tool-eval-bench, so they must be
# passed as separate words (not a single quoted string).  Word-split on purpose.
if [[ -n "$CATEGORIES" ]]; then
    # shellcheck disable=SC2206
    TEB_ARGS+=( --categories $CATEGORIES )
fi
if [[ -n "$SCENARIOS" ]]; then
    # shellcheck disable=SC2206
    TEB_ARGS+=( --scenarios $SCENARIOS )
fi

[[ -n "$TRIALS" ]]              && TEB_ARGS+=( --trials "$TRIALS" )
[[ -n "$PARALLEL" ]]            && TEB_ARGS+=( --parallel "$PARALLEL" )
[[ -n "$TEMPERATURE" ]]         && TEB_ARGS+=( --temperature "$TEMPERATURE" )
[[ -n "$TIMEOUT" ]]             && TEB_ARGS+=( --timeout "$TIMEOUT" )
[[ -n "$MAX_TURNS" ]]           && TEB_ARGS+=( --max-turns "$MAX_TURNS" )
[[ -n "$BACKEND" ]]             && TEB_ARGS+=( --backend "$BACKEND" )
[[ -n "$LABEL" ]]               && TEB_ARGS+=( --label "$LABEL" )

# Throughput knobs (only relevant with --perf / --perf-only).
[[ -n "$DEPTH" ]]               && TEB_ARGS+=( --depth "$DEPTH" )
[[ -n "$CONCURRENCY" ]]         && TEB_ARGS+=( --concurrency "$CONCURRENCY" )
[[ -n "$PP" ]]                  && TEB_ARGS+=( --pp "$PP" )
[[ -n "$TG" ]]                  && TEB_ARGS+=( --tg "$TG" )
[[ -n "$BENCHY_RUNS" ]]         && TEB_ARGS+=( --benchy-runs "$BENCHY_RUNS" )
[[ -n "$BENCHY_LATENCY_MODE" ]] && TEB_ARGS+=( --benchy-latency-mode "$BENCHY_LATENCY_MODE" )
[[ -n "$TOKENIZER" ]]           && TEB_ARGS+=( --tokenizer "$TOKENIZER" )

if [[ -n "$EXTRA_ARGS" ]]; then
    # shellcheck disable=SC2206
    EXTRA_ARR=( $EXTRA_ARGS )
    TEB_ARGS+=( "${EXTRA_ARR[@]}" )
fi

init_run_dir "$OUTPUT_DIR" "$RUN_NAME" "$BENCH_TAG"

# tool-eval-bench writes reports to <cwd>/runs/YYYY/MM/ and SQLite history to
# <cwd>/data/.  Run from inside $RUN_DIR so every artifact stays contained in
# the per-run directory.  $LOG_FILE / $META_FILE are absolute, so teeing and
# meta-writing still work after the cd.
RUN_DIR_ABS="$(cd "$RUN_DIR" && pwd)"

# Also emit a machine-readable JSON report beside result.md, so downstream
# tooling (scripts/generate-tool-eval-manifest.py) reads structured data
# instead of scraping the Markdown.  tool-eval-bench writes it directly to
# the given path; a fallback below also scavenges runs/ for a *.json if it
# happens to nest there like the .md reports do.  Added after init_run_dir so
# $RUN_DIR_ABS is known, and before write_cmd so run.cmd records it too.
TEB_ARGS+=( --json-file "$RUN_DIR_ABS/result.json" )

write_meta \
    "bench=tool-eval-bench" \
    "model=$MODEL" \
    "endpoint=$ENDPOINT" \
    "run_name=$RUN_NAME" \
    "short=$SHORT" \
    "hardmode=$HARDMODE" \
    "hardmode_only=$HARDMODE_ONLY" \
    "categories=$CATEGORIES" \
    "scenarios=$SCENARIOS" \
    "seed=$SEED" \
    "trials=${TRIALS:-1}" \
    "parallel=${PARALLEL:-1}" \
    "temperature=${TEMPERATURE:-0.0}" \
    "no_think=$NO_THINK" \
    "timeout=${TIMEOUT:-default}" \
    "max_turns=${MAX_TURNS:-8}" \
    "no_preflight=$NO_PREFLIGHT" \
    "no_warmup=$NO_WARMUP" \
    "backend=${BACKEND:-auto}" \
    "perf=$PERF" \
    "perf_only=$PERF_ONLY" \
    "depth=${DEPTH:-default}" \
    "concurrency=${CONCURRENCY:-default}" \
    "pp=${PP:-2048}" \
    "tg=${TG:-128}" \
    "label=$LABEL" \
    "extra_args=$EXTRA_ARGS"

# Record a replayable command.  tool-eval-bench writes reports to <cwd>/runs/
# and SQLite to <cwd>/data/, so the replay must `cd` into the run dir first.
# write_cmd assumes a single program argv, so emit the `cd ... && tool ...` form
# manually (each token shell-quoted for safety).
{
    printf '#!/usr/bin/env bash\n'
    printf '# Replayable command for run %s\n' "${RUN_TS:-?}"
    printf 'cd %q && %q' "$RUN_DIR_ABS" "$TEB_BIN"
    for a in "${TEB_ARGS[@]}"; do
        printf ' %q' "$a"
    done
    printf '\n'
} > "$CMD_FILE"
chmod +x "$CMD_FILE"

_mode_desc="tool-call quality (69 scenarios)"
if [[ "$SHORT" == true ]]; then _mode_desc="tool-call quality (15 short scenarios)"; fi
if [[ "$HARDMODE" == true ]]; then _mode_desc="tool-call quality (84 incl. hard mode)"; fi
if [[ "$HARDMODE_ONLY" == true ]]; then _mode_desc="hard mode only (Category P)"; fi
if [[ -n "$CATEGORIES" ]]; then _mode_desc="categories: $CATEGORIES"; fi
if [[ -n "$SCENARIOS" ]]; then _mode_desc="scenarios: $SCENARIOS"; fi
if [[ "$PERF_ONLY" == true ]]; then _mode_desc="throughput only"; fi
if [[ "$PERF" == true && "$PERF_ONLY" == false ]]; then _mode_desc="$_mode_desc + throughput"; fi

echo ">>> Running tool-eval-bench..."
echo "    Endpoint:     $ENDPOINT"
echo "    Model:        $MODEL"
echo "    Mode:         $_mode_desc"
echo "    Seed:         $SEED"
echo "    Trials:       ${TRIALS:-1}   Parallel: ${PARALLEL:-1}"
echo "    Temperature:  ${TEMPERATURE:-0.0}"
[[ -n "$BACKEND" ]] && echo "    Backend:      $BACKEND"
if [[ "$PERF" == true || "$PERF_ONLY" == true ]]; then
    echo "    Throughput:   depth=${DEPTH:-default} concurrency=${CONCURRENCY:-default} pp=${PP:-2048} tg=${TG:-128} runs=${BENCHY_RUNS:-3}"
fi
echo "    Binary:       $TEB_BIN"
echo "    Run name:     $RUN_NAME"
echo "    Run dir:       $RUN_DIR_ABS"
echo ""

if [[ "$SHELL_ONLY" == true ]]; then
    echo ">>> Shell ready. tool-eval-bench is on PATH."
    echo ">>> Suggested command (run from $RUN_DIR_ABS to contain outputs):"
    echo "    cd $RUN_DIR_ABS && $TEB_BIN ${TEB_ARGS[*]}"
    echo ""
    exec bash
fi

# Verify API connectivity first (best-effort; don't kill the run over a
# diagnostic).  tool-eval-bench also does its own pre-flight + warm-up.
echo "--- Verifying API connectivity ---"
if curl -sf -H "Authorization: Bearer $API_KEY" "${ENDPOINT%/v1}/v1/models" >/dev/null 2>&1 \
    || curl -sf -H "Authorization: Bearer $API_KEY" "${ENDPOINT%/}/models" >/dev/null 2>&1; then
    echo "OK"
else
    echo "[WARN] API connectivity check failed for $ENDPOINT; tool-eval-bench will proceed (it has its own pre-flight)."
fi
echo ""

echo "--- Starting tool-eval-bench ---"
# Run the tool with `set -e` temporarily relaxed so a non-zero exit (unreachable
# endpoint, --fail-on-safety, a crashed scenario) still lets us surface any report
# it managed to write and print a clear status — instead of aborting mid-tail.
set +e
(
    cd "$RUN_DIR_ABS"
    "$TEB_BIN" "${TEB_ARGS[@]}"
) 2>&1 | tee "$LOG_FILE"
TEB_RC=${PIPESTATUS[0]}
set -e

# Locate the newest Markdown report tool-eval-bench just wrote (under
# $RUN_DIR_ABS/runs/YYYY/MM/) and flatten it to result.md in the run dir, matching
# the repo convention: a tracked headline artifact beside run.log / meta.json
# (like llama-benchy's result.md).  The nested runs/ tree and the SQLite data/
# dir are kept on disk for --resume / --history / --leaderboard but ignored by
# git (see .gitignore) so they don't clutter `git status`.
LATEST_REPORT=""
if [[ -d "$RUN_DIR_ABS/runs" ]]; then
    LATEST_REPORT="$(find "$RUN_DIR_ABS/runs" -type f -name '*.md' \
        -printf '%T+\t%p\n' 2>/dev/null | sort -r | head -1 | cut -f2-)"
fi

RESULT_FILE="$RUN_DIR_ABS/result.md"
if [[ -n "$LATEST_REPORT" ]]; then
    cp -f "$LATEST_REPORT" "$RESULT_FILE" || true
    link_latest_result "$RESULT_FILE" "$BENCH_TAG" || true
fi

# Flatten the JSON report to result.json in the run dir, mirroring result.md.
# --json-file (added to TEB_ARGS above) writes it directly to the run dir; if
# it nested under runs/ instead, copy the newest *.json found there.
JSON_FILE="$RUN_DIR_ABS/result.json"
if [[ ! -f "$JSON_FILE" && -d "$RUN_DIR_ABS/runs" ]]; then
    LATEST_JSON="$(find "$RUN_DIR_ABS/runs" -type f -name '*.json' \
        -printf '%T+\t%p\n' 2>/dev/null | sort -r | head -1 | cut -f2-)"
    if [[ -n "$LATEST_JSON" ]]; then
        cp -f "$LATEST_JSON" "$JSON_FILE" || true
    fi
fi
if [[ -f "$JSON_FILE" ]]; then
    link_latest_result "$JSON_FILE" "$BENCH_TAG" || true
fi

echo ""
echo ">>> Benchmark complete."
echo ">>> Run dir:     $RUN_DIR_ABS"
echo ">>> Log:         $LOG_FILE"
if [[ -n "$LATEST_REPORT" ]]; then
    echo ">>> Report:     $LATEST_REPORT"
    echo ">>> Headline:   $RESULT_FILE"
else
    echo ">>> Report:      (none found under $RUN_DIR_ABS/runs/ — check the log)"
fi
echo ">>> Meta:        $META_FILE"
echo ">>> Cmd:         $CMD_FILE"
if [[ -f "$JSON_FILE" ]]; then
    echo ">>> JSON:        $JSON_FILE"
else
    echo ">>> JSON:         (none — --json-file produced no result.json; check the log)"
fi

if [[ -f "$RESULT_FILE" ]]; then
    echo ""
    echo "--- Report tail ---"
    tail -n 40 "$RESULT_FILE"
fi

# Propagate tool-eval-bench's exit code so run_benchmarks.py marks the job
# correctly (0 = OK, non-zero = FAIL) even though we surfaced the report above.
exit "$TEB_RC"
