#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

BENCHY_REPO="https://github.com/eugr/llama-benchy.git"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run llama-benchy benchmarks against a local OpenAI-compatible LLM endpoint.
Repo is cloned automatically on first run into work/llama-benchy/.
The host environment is expected to provide python3, uv, git and curl (the
flake's devShell does so; otherwise install them yourself).

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
  --model <name>       Model name as recognized by the endpoint
                       If omitted, lists available models from the endpoint

Common options:
  --api-key <key>           API key (default: EMPTY)
  --output-dir <path>       Root directory for results (default: ./benchmarks)
  --work-dir <path>         Directory for cached clones/venvs (default: ./work)
  --run-name <name>         Name suffix for this run (default: derived from --model)
  --rebuild                 Recreate the venv before running
  --shell-only              Drop into a shell with the venv activated; don't run benchmark
  -h, --help                Show this help message

llama-benchy options:
  --pp <list>               Space-separated prompt processing token counts (default: "2048")
  --tg <list>               Space-separated token generation counts (default: "128")
  --depth <list>            Space-separated context depths (default: "0 8192 16384")
  --runs <n>                Number of runs per test (default: 3)
  --concurrency <list>      Space-separated concurrency levels (default: "1 2 4")
  --latency-mode <mode>     Latency mode: api, generation, none (default: generation)
  --format <fmt>            Output format: md, json, csv (default: md)
  --enable-prefix-caching   Measure prefix-caching benchmarks
  --update                  git pull the llama-benchy repo before running
  --extra-args <args>       Extra args passed verbatim to llama-benchy

Examples:
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-qwen-model
  $(basename "$0") --endpoint http://localhost:8080/v1 --model gpt-oss-120b \\
      --depth "0 4096 8192" --latency-mode generation --enable-prefix-caching
EOF
    exit 0
}

ENDPOINT=""
MODEL=""
API_KEY="EMPTY"
OUTPUT_DIR="./benchmarks"
WORK_DIR="./work"
RUN_NAME=""
PP="2048"
TG="128"
DEPTH="0 8192 16384"
RUNS=3
CONCURRENCY="1 2 4"
LATENCY_MODE="generation"
FORMAT="md"
ENABLE_PREFIX_CACHING=false
REBUILD=false
UPDATE=false
SHELL_ONLY=false
EXTRA_ARGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint)                ENDPOINT="$2"; shift 2 ;;
        --model)                   MODEL="$2"; shift 2 ;;
        --api-key)                 API_KEY="$2"; shift 2 ;;
        --output-dir)              OUTPUT_DIR="$2"; shift 2 ;;
        --work-dir)                WORK_DIR="$2"; shift 2 ;;
        --run-name)                RUN_NAME="$2"; shift 2 ;;
        --pp)                      PP="$2"; shift 2 ;;
        --tg)                      TG="$2"; shift 2 ;;
        --depth)                   DEPTH="$2"; shift 2 ;;
        --runs)                    RUNS="$2"; shift 2 ;;
        --concurrency)             CONCURRENCY="$2"; shift 2 ;;
        --latency-mode)            LATENCY_MODE="$2"; shift 2 ;;
        --format)                  FORMAT="$2"; shift 2 ;;
        --enable-prefix-caching)   ENABLE_PREFIX_CACHING=true; shift ;;
        --rebuild)                 REBUILD=true; shift ;;
        --update)                  UPDATE=true; shift ;;
        --shell-only)              SHELL_ONLY=true; shift ;;
        --extra-args)              EXTRA_ARGS="$2"; shift 2 ;;
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

for bin in uv python3 git curl; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "Error: '$bin' not found in PATH; use the flake devShell or install it." >&2
        exit 1
    fi
done

mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
BENCHY_DIR="$WORK_DIR/llama-benchy"
VENV_DIR="$BENCHY_DIR/.venv"

if [[ ! -d "$BENCHY_DIR" ]]; then
    echo ">>> Cloning llama-benchy repository..."
    git clone "$BENCHY_REPO" "$BENCHY_DIR"
elif [[ "$UPDATE" == true ]]; then
    echo ">>> Updating llama-benchy repository..."
    git -C "$BENCHY_DIR" pull --ff-only
fi

if [[ "$REBUILD" == true && -d "$VENV_DIR" ]]; then
    echo ">>> Removing existing venv at $VENV_DIR"
    rm -rf "$VENV_DIR"
fi

init_run_dir "$OUTPUT_DIR" "$MODEL" "llama-benchy"
RESULT_FILE="$RUN_DIR/result.$FORMAT"

write_meta \
    "bench=llama-benchy" \
    "model=$MODEL" \
    "endpoint=$ENDPOINT" \
    "run_name=$RUN_NAME" \
    "pp=$PP" \
    "tg=$TG" \
    "depth=$DEPTH" \
    "runs=$RUNS" \
    "concurrency=$CONCURRENCY" \
    "latency_mode=$LATENCY_MODE" \
    "format=$FORMAT" \
    "enable_prefix_caching=$ENABLE_PREFIX_CACHING"

# Build the llama-benchy CLI invocation
# shellcheck disable=SC2206
BENCHY_ARGS=(
    --base-url "$ENDPOINT"
    --api-key  "$API_KEY"
    --model    "$MODEL"
    --pp       $PP
    --tg       $TG
    --depth    $DEPTH
    --runs     "$RUNS"
    --concurrency $CONCURRENCY
    --latency-mode "$LATENCY_MODE"
    --format   "$FORMAT"
    --save-result "$RESULT_FILE"
)

if [[ "$ENABLE_PREFIX_CACHING" == true ]]; then
    BENCHY_ARGS+=( --enable-prefix-caching )
fi

if [[ -n "$EXTRA_ARGS" ]]; then
    # shellcheck disable=SC2206
    EXTRA_ARR=( $EXTRA_ARGS )
    BENCHY_ARGS+=( "${EXTRA_ARR[@]}" )
fi

PRINTF_ARGS=$(printf ' %q' "${BENCHY_ARGS[@]}")
write_cmd llama-benchy "${BENCHY_ARGS[@]}"

setup_venv() {
    cd "$BENCHY_DIR"
    if [[ ! -d "$VENV_DIR" ]]; then
        echo ">>> Creating venv at $VENV_DIR (via uv)"
        uv venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    if [[ ! -f "$VENV_DIR/.installed" || "$REBUILD" == "true" || "$UPDATE" == "true" ]]; then
        echo ">>> Installing llama-benchy into venv (editable, via uv)..."
        uv pip install -e .
        touch "$VENV_DIR/.installed"
    fi
}

if [[ "$SHELL_ONLY" == true ]]; then
    setup_venv
    echo ""
    echo ">>> venv activated.  Suggested command:"
    echo "    llama-benchy${PRINTF_ARGS}"
    echo ""
    exec bash
fi

echo ">>> Running llama-benchy..."
echo "    Endpoint:       $ENDPOINT"
echo "    Model:          $MODEL"
echo "    pp:             $PP"
echo "    tg:             $TG"
echo "    depth:          $DEPTH"
echo "    runs:           $RUNS"
echo "    concurrency:    $CONCURRENCY"
echo "    latency-mode:   $LATENCY_MODE"
echo "    format:         $FORMAT"
echo "    prefix-cache:   $ENABLE_PREFIX_CACHING"
echo "    run name:       $RUN_NAME"
echo "    run dir:        $RUN_DIR"
echo ""

{
    setup_venv
    echo '--- Verifying API connectivity ---'
    if curl -sf -H "Authorization: Bearer $API_KEY" "${ENDPOINT%/v1}/v1/models" >/dev/null; then
        echo 'OK'
    else
        echo "FAILED to reach ${ENDPOINT%/v1}/v1/models" >&2
        exit 1
    fi
    echo ''
    echo '--- Starting benchmark ---'
    # shellcheck disable=SC2086
    llama-benchy "${BENCHY_ARGS[@]}"
} 2>&1 | tee "$LOG_FILE"

# Reaching this point means the piped block above exited 0 (set -e + pipefail).
echo ""
echo ">>> Benchmark complete."
echo ">>> Run dir:     $RUN_DIR"
echo ">>> Log:         $LOG_FILE"
if [[ -f "$RESULT_FILE" ]]; then
    echo ">>> Result:      $RESULT_FILE"
    link_latest_result "$RESULT_FILE" "llama-benchy"
fi
echo ">>> Meta:        $META_FILE"
echo ">>> Cmd:         $CMD_FILE"

if [[ "$FORMAT" == "md" && -f "$RESULT_FILE" ]]; then
    echo ""
    echo "--- Result ($RESULT_FILE) ---"
    cat "$RESULT_FILE"
fi
