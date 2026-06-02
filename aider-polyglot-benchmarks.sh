#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

AIDER_REPO="https://github.com/Aider-AI/aider.git"
POLYGLOT_REPO="https://github.com/Aider-AI/polyglot-benchmark.git"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run Aider polyglot benchmarks against a local LLM endpoint inside a container.
Repos are cloned automatically on first run into work/aider/.

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
  --model <name>       Model name as recognized by the endpoint (prefixed with openai/ automatically)
                       If omitted, lists available models from the endpoint

Common options:
  --api-key <key>           API key (default: dummy-key)
  --output-dir <path>       Root directory for results (default: ./benchmarks)
  --work-dir <path>         Directory for cached clones/image (default: ./work)
  --run-name <name>         Name for this benchmark run (default: derived from --model)
  --rebuild                 Rebuild the container image before running
  --shell-only              Drop into the container shell instead of running the benchmark
  -h, --help                Show this help message

aider options:
  --container-runtime <rt>  Container runtime: podman or docker (default: auto-detect)
  --edit-format <fmt>       Edit format for aider (default: whole)
  --num-tests <n>           Number of test exercises to run (default: all)
  --threads <n>             Number of parallel threads (default: 1)
  --request-timeout <secs>  Per-LLM-request HTTP timeout in seconds.  Bump this
                            when you see 'litellm.Timeout' errors (e.g. 1200).

Examples:
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-qwen-model
  $(basename "$0") --endpoint http://172.17.0.1:8080/v1 --model my-model --num-tests 10 --threads 2
EOF
    exit 0
}

ENDPOINT=""
MODEL=""
API_KEY="dummy-key"
OUTPUT_DIR="./benchmarks"
WORK_DIR="./work"
RUN_NAME=""
CONTAINER_RUNTIME=""
EDIT_FORMAT="whole"
NUM_TESTS=""
THREADS=1
REQUEST_TIMEOUT=""
REBUILD=false
SHELL_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint)           ENDPOINT="$2"; shift 2 ;;
        --model)              MODEL="$2"; shift 2 ;;
        --api-key)            API_KEY="$2"; shift 2 ;;
        --output-dir)         OUTPUT_DIR="$2"; shift 2 ;;
        --work-dir)           WORK_DIR="$2"; shift 2 ;;
        --run-name)           RUN_NAME="$2"; shift 2 ;;
        --container-runtime)  CONTAINER_RUNTIME="$2"; shift 2 ;;
        --edit-format)        EDIT_FORMAT="$2"; shift 2 ;;
        --num-tests)          NUM_TESTS="$2"; shift 2 ;;
        --threads)            THREADS="$2"; shift 2 ;;
        --request-timeout)    REQUEST_TIMEOUT="$2"; shift 2 ;;
        --rebuild)            REBUILD=true; shift ;;
        --shell-only)         SHELL_ONLY=true; shift ;;
        -h|--help)            usage ;;
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

CONTAINER_RUNTIME="$(pick_container_runtime "$CONTAINER_RUNTIME")"
echo ">>> Using container runtime: $CONTAINER_RUNTIME"

mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
AIDER_DIR="$WORK_DIR/aider"
POLYGLOT_DIR="$AIDER_DIR/tmp.benchmarks/polyglot-benchmark"

if [[ ! -d "$AIDER_DIR" ]]; then
    echo ">>> Cloning aider repository..."
    git clone "$AIDER_REPO" "$AIDER_DIR"
fi

mkdir -p "$AIDER_DIR/tmp.benchmarks"

if [[ ! -d "$POLYGLOT_DIR" ]]; then
    echo ">>> Cloning polyglot-benchmark repository..."
    git clone "$POLYGLOT_REPO" "$POLYGLOT_DIR"
fi

IMAGE_EXISTS=false
if $CONTAINER_RUNTIME image inspect aider-benchmark &>/dev/null; then
    IMAGE_EXISTS=true
fi

if [[ "$IMAGE_EXISTS" == false || "$REBUILD" == true ]]; then
    echo ">>> Building container image..."
    $CONTAINER_RUNTIME build \
        --file "$AIDER_DIR/benchmark/Dockerfile" \
        -t aider-benchmark \
        "$AIDER_DIR"
fi

CONTAINER_ENDPOINT="$ENDPOINT"
if [[ "$ENDPOINT" =~ ^http://localhost(:[0-9]+)? ]]; then
    CONTAINER_ENDPOINT="${ENDPOINT/localhost/host.containers.internal}"
elif [[ "$ENDPOINT" =~ ^http://127\.0\.0\.1(:[0-9]+)? ]]; then
    CONTAINER_ENDPOINT="${ENDPOINT/127.0.0.1/host.containers.internal}"
fi

HOST_GATEWAY_FLAG="--add-host=host.containers.internal:host-gateway"
if [[ "$CONTAINER_RUNTIME" == "docker" ]]; then
    HOST_GATEWAY_FLAG="--add-host=host.docker.internal:host-gateway"
fi

NUM_TESTS_FLAG=""
if [[ -n "$NUM_TESTS" ]]; then
    NUM_TESTS_FLAG="--num-tests $NUM_TESTS"
fi

# For --request-timeout, use aider's official config mechanism instead of
# patching: a model-settings YAML with the `aider/extra_params` entry, which
# aider deep-merges into every model's extra_params (see aider/models.py).
EXTRA_MODEL_SETTINGS_FLAG=""
if [[ -n "$REQUEST_TIMEOUT" ]]; then
    EXTRA_MODEL_SETTINGS_FILE="$AIDER_DIR/aider-extra-model-settings.yml"
    cat > "$EXTRA_MODEL_SETTINGS_FILE" <<EOF
- name: aider/extra_params
  extra_params:
    timeout: $REQUEST_TIMEOUT
EOF
    echo ">>> Wrote $EXTRA_MODEL_SETTINGS_FILE (timeout: ${REQUEST_TIMEOUT}s)"
    EXTRA_MODEL_SETTINGS_FLAG="--read-model-settings /aider/aider-extra-model-settings.yml"
fi

BENCHMARK_CMD="benchmark/benchmark.py $RUN_NAME \
  --model openai/$MODEL \
  --edit-format $EDIT_FORMAT \
  --exercises-dir polyglot-benchmark \
  --threads $THREADS \
  $NUM_TESTS_FLAG \
  $EXTRA_MODEL_SETTINGS_FLAG"

init_run_dir "$OUTPUT_DIR" "$MODEL" "aider"
# Absolute path needed for the bind-mount.
RUN_DIR_ABS="$(cd "$RUN_DIR" && pwd)"
STATS_FILE="$RUN_DIR/stats.txt"

write_meta \
    "bench=aider" \
    "model=$MODEL" \
    "endpoint=$ENDPOINT" \
    "container_endpoint=$CONTAINER_ENDPOINT" \
    "run_name=$RUN_NAME" \
    "edit_format=$EDIT_FORMAT" \
    "num_tests=${NUM_TESTS:-all}" \
    "threads=$THREADS" \
    "request_timeout=${REQUEST_TIMEOUT:-default}" \
    "container_runtime=$CONTAINER_RUNTIME"

RUN_ARGS=(
    -it --rm
    --memory=12g
    --memory-swap=12g
    $HOST_GATEWAY_FLAG
    -v "$AIDER_DIR":/aider
    -v "$AIDER_DIR/tmp.benchmarks":/benchmarks
    -v "$RUN_DIR_ABS":/run
    -e OPENAI_API_KEY="$API_KEY"
    -e OPENAI_API_BASE="$CONTAINER_ENDPOINT"
    -e AIDER_DOCKER=1
    -e AIDER_BENCHMARK_DIR=/benchmarks
    -e RUN_NAME="$RUN_NAME"
)

if [[ "$SHELL_ONLY" == true ]]; then
    RUN_ARGS+=(
        -e HISTFILE=/aider/.bash_history
        -e PROMPT_COMMAND='history -a'
        -e HISTCONTROL=ignoredups
        -e HISTSIZE=10000
        -e HISTFILESIZE=20000
    )
    echo ">>> Launching container shell (benchmark command will NOT run automatically)..."
    echo ">>> When ready, run inside the container:"
    echo "    $BENCHMARK_CMD"
    echo "    python3 benchmark/benchmark.py \$RUN_NAME --stats --exercises-dir polyglot-benchmark"
    $CONTAINER_RUNTIME run "${RUN_ARGS[@]}" aider-benchmark bash
    exit 0
fi

echo ">>> Running benchmark..."
echo "    Runtime:      $CONTAINER_RUNTIME"
echo "    Model:        openai/$MODEL"
echo "    Endpoint:     $CONTAINER_ENDPOINT"
echo "    Edit format:  $EDIT_FORMAT"
echo "    Tests:        ${NUM_TESTS:-all}"
echo "    Threads:      $THREADS"
echo "    Req timeout:  ${REQUEST_TIMEOUT:+${REQUEST_TIMEOUT}s }${REQUEST_TIMEOUT:-aider default (600s)}"
echo "    Run name:     $RUN_NAME"
echo "    Run dir:      $RUN_DIR"
echo ""

INNER_CMD="pip install -e '.[dev]' 2>/dev/null && echo '--- Verifying API connectivity ---' && curl -sf ${CONTAINER_ENDPOINT%/v1}/v1/models && echo '' && echo '--- Starting benchmark ---' && python3 $BENCHMARK_CMD && echo '' && echo '--- Generating report ---' && python3 benchmark/benchmark.py \$RUN_NAME --stats --exercises-dir polyglot-benchmark 2>&1 | tee /run/stats.txt"

write_cmd "$CONTAINER_RUNTIME" run "${RUN_ARGS[@]}" \
    -w /aider \
    -e STATS_FILE=/run/stats.txt \
    aider-benchmark \
    bash -c "$INNER_CMD"

$CONTAINER_RUNTIME run "${RUN_ARGS[@]}" \
    -w /aider \
    -e STATS_FILE=/run/stats.txt \
    aider-benchmark \
    bash -c "$INNER_CMD" \
    2>&1 | tee "$LOG_FILE"

# Copy aider's per-run artifact directory into the result dir for archival.
AIDER_RUN_ARTIFACTS="$AIDER_DIR/tmp.benchmarks/$RUN_NAME"
if [[ -d "$AIDER_RUN_ARTIFACTS" ]]; then
    mkdir -p "$RUN_DIR/tmp.benchmarks"
    cp -r "$AIDER_RUN_ARTIFACTS" "$RUN_DIR/tmp.benchmarks/" 2>/dev/null || true
fi

echo ""
echo ">>> Benchmark complete."
echo ">>> Run dir: $RUN_DIR"
echo ">>> Log:     $LOG_FILE"
if [[ -f "$STATS_FILE" ]]; then
    echo ">>> Stats:   $STATS_FILE"
    link_latest_result "$STATS_FILE" "aider"
fi
echo ">>> Meta:    $META_FILE"
echo ">>> Cmd:     $CMD_FILE"
