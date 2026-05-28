#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$SCRIPT_DIR/work"
AIDER_DIR="$WORK_DIR/aider"
AIDER_REPO="https://github.com/Aider-AI/aider.git"
POLYGLOT_DIR="$AIDER_DIR/tmp.benchmarks/polyglot-benchmark"
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

Options:
  --container-runtime <rt>  Container runtime: podman or docker (default: podman)
  --edit-format <fmt>       Edit format for aider (default: whole)
  --num-tests <n>           Number of test exercises to run (default: all)
  --threads <n>             Number of parallel threads (default: 1)
  --rebuild                 Rebuild the container image before running
  --shell-only              Drop into the container shell instead of running the benchmark
  --run-name <name>         Name for this benchmark run (default: local-model-run)
  -h, --help                Show this help message

Examples:
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-qwen-model
  $(basename "$0") --endpoint http://172.17.0.1:8080/v1 --model my-model --num-tests 10 --threads 2
  $(basename "$0") --endpoint http://litellm.thing.wg0.maxhbr.local/v1 --model "rtx5090:Qwen3.5-9B-Q5_K_M" --container-runtime docker
EOF
    exit 0
}

ENDPOINT=""
MODEL=""
CONTAINER_RUNTIME=""
EDIT_FORMAT="whole"
NUM_TESTS=""
THREADS=1
REBUILD=false
SHELL_ONLY=false
RUN_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint)           ENDPOINT="$2"; shift 2 ;;
        --model)              MODEL="$2"; shift 2 ;;
        --container-runtime)  CONTAINER_RUNTIME="$2"; shift 2 ;;
        --edit-format)        EDIT_FORMAT="$2"; shift 2 ;;
        --num-tests)          NUM_TESTS="$2"; shift 2 ;;
        --threads)            THREADS="$2"; shift 2 ;;
        --rebuild)            REBUILD=true; shift ;;
        --shell-only)         SHELL_ONLY=true; shift ;;
        --run-name)           RUN_NAME="$2"; shift 2 ;;
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
    MODELS_URL="${ENDPOINT%/v1}/v1/models"
    if command -v jq &>/dev/null; then
        curl -sf "$MODELS_URL" | jq -r '.data[].id' 2>/dev/null || curl -sf "$MODELS_URL"
    else
        curl -sf "$MODELS_URL" | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]" 2>/dev/null || curl -sf "$MODELS_URL"
    fi
    echo ""
    echo "Re-run with --model <name> to start the benchmark."
    exit 0
fi

if [[ -z "$RUN_NAME" ]]; then
    RUN_NAME="$(echo "$MODEL" | sed 's|/|_|g; s|:|-|g; s|[^a-zA-Z0-9._-]|-|g')"
fi

if [[ -z "$CONTAINER_RUNTIME" ]]; then
    if command -v podman &>/dev/null; then
        CONTAINER_RUNTIME=podman
    elif command -v docker &>/dev/null; then
        CONTAINER_RUNTIME=docker
    else
        echo "Error: neither podman nor docker found" >&2
        exit 1
    fi
elif [[ "$CONTAINER_RUNTIME" != "podman" && "$CONTAINER_RUNTIME" != "docker" ]]; then
    echo "Error: --container-runtime must be 'podman' or 'docker'" >&2
    exit 1
fi

echo ">>> Using container runtime: $CONTAINER_RUNTIME"

mkdir -p "$WORK_DIR"

if [[ ! -d "$AIDER_DIR" ]]; then
    echo ">>> Cloning aider repository..."
    git clone "$AIDER_REPO" "$AIDER_DIR"
fi

mkdir -p "$AIDER_DIR/tmp.benchmarks"

if [[ ! -d "$POLYGLOT_DIR" ]]; then
    echo ">>> Cloning polyglot-benchmark repository..."
    git clone "$POLYGLOT_REPO" "$POLYGLOT_DIR"
fi

if [[ "$CONTAINER_RUNTIME" == "podman" ]]; then
    IMAGE_EXISTS=false
    if podman image inspect aider-benchmark &>/dev/null; then
        IMAGE_EXISTS=true
    fi
else
    IMAGE_EXISTS=false
    if docker image inspect aider-benchmark &>/dev/null; then
        IMAGE_EXISTS=true
    fi
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

BENCHMARK_CMD="benchmark/benchmark.py $RUN_NAME \
  --model openai/$MODEL \
  --edit-format $EDIT_FORMAT \
  --exercises-dir polyglot-benchmark \
  --threads $THREADS \
  $NUM_TESTS_FLAG"

RUN_ARGS=(
    -it --rm
    --memory=12g
    --memory-swap=12g
    $HOST_GATEWAY_FLAG
    -v "$AIDER_DIR":/aider
    -v "$AIDER_DIR/tmp.benchmarks":/benchmarks
    -e OPENAI_API_KEY=dummy-key
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
else
    echo ">>> Running benchmark..."
    echo "    Runtime:      $CONTAINER_RUNTIME"
    echo "    Model:        openai/$MODEL"
    echo "    Endpoint:     $CONTAINER_ENDPOINT"
    echo "    Edit format:  $EDIT_FORMAT"
    echo "    Tests:        ${NUM_TESTS:-all}"
    echo "    Threads:      $THREADS"
    echo "    Run name:     $RUN_NAME"
    echo ""

    LOG_DIR="$WORK_DIR/logs"
    mkdir -p "$LOG_DIR"
    TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    LOG_FILE="$LOG_DIR/${RUN_NAME}-${TIMESTAMP}.log"
    STATS_FILE="$LOG_DIR/${RUN_NAME}-${TIMESTAMP}-stats.txt"
    STATS_FILE_IN_CONTAINER="/benchmarks/.stats-${RUN_NAME}-${TIMESTAMP}.txt"
    echo ">>> Logging to $LOG_FILE"
    echo ">>> Stats will be saved to $STATS_FILE"

    $CONTAINER_RUNTIME run "${RUN_ARGS[@]}" \
        -w /aider \
        -e STATS_FILE="$STATS_FILE_IN_CONTAINER" \
        aider-benchmark \
        bash -c "pip install -e '.[dev]' 2>/dev/null && echo '--- Verifying API connectivity ---' && curl -sf ${CONTAINER_ENDPOINT%/v1}/v1/models && echo '' && echo '--- Starting benchmark ---' && python3 $BENCHMARK_CMD && echo '' && echo '--- Generating report ---' && python3 benchmark/benchmark.py \$RUN_NAME --stats --exercises-dir polyglot-benchmark 2>&1 | tee \$STATS_FILE" \
        2>&1 | tee "$LOG_FILE"

    HOST_STATS_FILE="$AIDER_DIR/tmp.benchmarks/.stats-${RUN_NAME}-${TIMESTAMP}.txt"
    if [[ -f "$HOST_STATS_FILE" ]]; then
        mv "$HOST_STATS_FILE" "$STATS_FILE"
    fi

    echo ""
    echo ">>> Benchmark complete. Results are in: $AIDER_DIR/tmp.benchmarks/"
    echo ">>> Log saved to:   $LOG_FILE"
    echo ">>> Stats saved to: $STATS_FILE"
fi
