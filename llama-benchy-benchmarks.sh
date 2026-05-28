#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$SCRIPT_DIR/work"
BENCHY_DIR="$WORK_DIR/llama-benchy"
BENCHY_REPO="https://github.com/eugr/llama-benchy.git"
VENV_DIR="$BENCHY_DIR/.venv"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run llama-benchy benchmarks against a local OpenAI-compatible LLM endpoint
inside a nix-shell with a Python venv.
Repo is cloned automatically on first run into work/llama-benchy/.

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
  --model <name>       Model name as recognized by the endpoint
                       If omitted, lists available models from the endpoint

Options:
  --api-key <key>           API key (default: EMPTY)
  --pp <list>               Space-separated prompt processing token counts (default: "2048")
  --tg <list>               Space-separated token generation counts (default: "32")
  --depth <list>            Space-separated context depths (default: "0")
  --runs <n>                Number of runs per test (default: 3)
  --concurrency <list>      Space-separated concurrency levels (default: "1")
  --latency-mode <mode>     Latency mode: api, generation, none (default: generation)
  --format <fmt>            Output format: md, json, csv (default: md)
  --enable-prefix-caching   Measure prefix-caching benchmarks
  --rebuild                 Recreate the venv before running
  --update                  git pull the llama-benchy repo before running
  --shell-only              Drop into the nix-shell with venv activated; don't run benchmark
  --run-name <name>         Name for this benchmark run (default: derived from --model)
  --extra-args <args>       Extra args passed verbatim to llama-benchy
  -h, --help                Show this help message

Examples:
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-qwen-model
  $(basename "$0") --endpoint http://localhost:8080/v1 --model gpt-oss-120b \\
      --depth "0 4096 8192" --latency-mode generation --enable-prefix-caching
  $(basename "$0") --endpoint http://litellm.thing.wg0.maxhbr.local/v1 \\
      --model "rtx5090:Qwen3.5-9B-Q5_K_M" --format json
EOF
    exit 0
}

ENDPOINT=""
MODEL=""
API_KEY="EMPTY"
PP="2048"
TG="32"
DEPTH="0"
RUNS=3
CONCURRENCY="1"
LATENCY_MODE="generation"
FORMAT="md"
ENABLE_PREFIX_CACHING=false
REBUILD=false
UPDATE=false
SHELL_ONLY=false
RUN_NAME=""
EXTRA_ARGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint)                ENDPOINT="$2"; shift 2 ;;
        --model)                   MODEL="$2"; shift 2 ;;
        --api-key)                 API_KEY="$2"; shift 2 ;;
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
        --run-name)                RUN_NAME="$2"; shift 2 ;;
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
    MODELS_URL="${ENDPOINT%/v1}/v1/models"
    if command -v jq &>/dev/null; then
        curl -sf -H "Authorization: Bearer $API_KEY" "$MODELS_URL" | jq -r '.data[].id' 2>/dev/null \
            || curl -sf -H "Authorization: Bearer $API_KEY" "$MODELS_URL"
    else
        curl -sf -H "Authorization: Bearer $API_KEY" "$MODELS_URL" \
            | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]" 2>/dev/null \
            || curl -sf -H "Authorization: Bearer $API_KEY" "$MODELS_URL"
    fi
    echo ""
    echo "Re-run with --model <name> to start the benchmark."
    exit 0
fi

if [[ -z "$RUN_NAME" ]]; then
    RUN_NAME="$(echo "$MODEL" | sed 's|/|_|g; s|:|-|g; s|[^a-zA-Z0-9._-]|-|g')"
fi

if ! command -v nix-shell &>/dev/null; then
    echo "Error: nix-shell not found in PATH" >&2
    exit 1
fi

mkdir -p "$WORK_DIR"

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

LOG_DIR="$WORK_DIR/logs"
RESULTS_DIR="$WORK_DIR/results"
mkdir -p "$LOG_DIR" "$RESULTS_DIR"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/llama-benchy-${RUN_NAME}-${TIMESTAMP}.log"
RESULT_FILE="$RESULTS_DIR/llama-benchy-${RUN_NAME}-${TIMESTAMP}.${FORMAT}"

# Build the llama-benchy CLI invocation
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

# Quote args for the bash -c invocation inside nix-shell
PRINTF_ARGS=$(printf ' %q' "${BENCHY_ARGS[@]}")

# Setup commands executed inside nix-shell
SETUP_CMDS=$(cat <<EOF
set -euo pipefail
cd "$BENCHY_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
    echo ">>> Creating venv at $VENV_DIR (via uv)"
    uv venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
if [[ ! -f "$VENV_DIR/.installed" ]] || [[ "$REBUILD" == "true" ]] || [[ "$UPDATE" == "true" ]]; then
    echo ">>> Installing llama-benchy into venv (editable, via uv)..."
    uv pip install -e .
    touch "$VENV_DIR/.installed"
fi
EOF
)

# Build a nix-shell expression that pulls in the needed runtime libraries
# (libstdc++, libz, etc.) so that pip/uv-installed pre-built wheels (numpy,
# tokenizers, ...) can find their shared library dependencies.
NIX_SHELL_EXPR='with import <nixpkgs> {}; mkShell {
  buildInputs = [ python3 uv git curl ];
  LD_LIBRARY_PATH = lib.makeLibraryPath [ stdenv.cc.cc.lib zlib ];
}'

if [[ "$SHELL_ONLY" == true ]]; then
    echo ">>> Launching nix-shell with venv activated (benchmark will NOT run automatically)..."
    echo ">>> When ready, run:"
    echo "    llama-benchy${PRINTF_ARGS}"
    echo ""
    exec nix-shell -E "$NIX_SHELL_EXPR" --run "bash --rcfile <(cat <<'RCEOF'
$SETUP_CMDS
echo ''
echo '>>> venv activated. Suggested command:'
echo '    llama-benchy${PRINTF_ARGS}'
echo ''
RCEOF
)"
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
echo "    log file:       $LOG_FILE"
echo "    result file:    $RESULT_FILE"
echo ""

RUN_CMDS=$(cat <<EOF
$SETUP_CMDS
echo '--- Verifying API connectivity ---'
curl -sf -H "Authorization: Bearer $API_KEY" "${ENDPOINT%/v1}/v1/models" >/dev/null \\
    && echo 'OK' \\
    || { echo 'FAILED to reach ${ENDPOINT%/v1}/v1/models' >&2; exit 1; }
echo ''
echo '--- Starting benchmark ---'
llama-benchy${PRINTF_ARGS}
EOF
)

nix-shell -E "$NIX_SHELL_EXPR" --run "$RUN_CMDS" 2>&1 | tee "$LOG_FILE"

echo ""
echo ">>> Benchmark complete."
echo ">>> Log saved to:    $LOG_FILE"
if [[ -f "$RESULT_FILE" ]]; then
    echo ">>> Result saved to: $RESULT_FILE"
fi
