#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIDER_DIR="$SCRIPT_DIR/aider"
POLYGLOT_DIR="$SCRIPT_DIR/polyglot-benchmark"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run Aider polyglot benchmarks against a local LLM endpoint inside a container.

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
  --model <name>       Model name as recognized by the endpoint (prefixed with openai/ automatically)

Options:
  --container-runtime <rt>  Container runtime: podman or docker (default: podman)
  --edit-format <fmt>       Edit format for aider (default: whole)
  --num-tests <n>           Number of test exercises to run (default: 5)
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
NUM_TESTS=5
THREADS=1
REBUILD=false
SHELL_ONLY=false
RUN_NAME="local-model-run"

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
    echo "Error: --model is required" >&2
    usage
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

if [[ ! -d "$AIDER_DIR" ]]; then
    echo "Error: aider submodule not found at $AIDER_DIR" >&2
    echo "Run: git submodule update --init" >&2
    exit 1
fi
if [[ ! -d "$POLYGLOT_DIR" ]]; then
    echo "Error: polyglot-benchmark submodule not found at $POLYGLOT_DIR" >&2
    echo "Run: git submodule update --init" >&2
    exit 1
fi

mkdir -p "$AIDER_DIR/tmp.benchmarks"

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
if [[ "$HOST_ENDPOINT" =~ ^http://localhost(:[0-9]+)? ]]; then
    CONTAINER_ENDPOINT="${HOST_ENDPOINT/localhost/host.containers.internal}"
elif [[ "$HOST_ENDPOINT" =~ ^http://127\.0\.0\.1(:[0-9]+)? ]]; then
    CONTAINER_ENDPOINT="${HOST_ENDPOINT/127.0.0.1/host.containers.internal}"
fi

HOST_GATEWAY_FLAG="--add-host=host.containers.internal:host-gateway"
if [[ "$CONTAINER_RUNTIME" == "docker" ]]; then
    HOST_GATEWAY_FLAG="--add-host=host.docker.internal:host-gateway"
fi

BENCHMARK_CMD="benchmark/benchmark.py $RUN_NAME \
  --model openai/$MODEL \
  --openai-api-base $CONTAINER_ENDPOINT \
  --openai-api-key dummy-key \
  --edit-format $EDIT_FORMAT \
  --exercises-dir polyglot-benchmark \
  --threads $THREADS \
  --num-tests $NUM_TESTS"

RUN_ARGS=(
    -it --rm
    --memory=12g
    --memory-swap=12g
    $HOST_GATEWAY_FLAG
    -v "$AIDER_DIR":/aider
    -v "$AIDER_DIR/tmp.benchmarks":/benchmarks
    -e OPENAI_API_KEY=dummy-key
    -e AIDER_DOCKER=1
    -e AIDER_BENCHMARK_DIR=/benchmarks
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
    $CONTAINER_RUNTIME run "${RUN_ARGS[@]}" aider-benchmark bash
else
    echo ">>> Running benchmark..."
    echo "    Runtime:      $CONTAINER_RUNTIME"
    echo "    Model:        openai/$MODEL"
    echo "    Endpoint:     $CONTAINER_ENDPOINT"
    echo "    Edit format:  $EDIT_FORMAT"
    echo "    Tests:        $NUM_TESTS"
    echo "    Threads:      $THREADS"
    echo "    Run name:     $RUN_NAME"
    echo ""

    $CONTAINER_RUNTIME run "${RUN_ARGS[@]}" \
        -w /aider \
        aider-benchmark \
        bash -c "pip install -e '.[dev]' 2>/dev/null && echo '--- Verifying API connectivity ---' && curl -sf ${CONTAINER_ENDPOINT%/v1}/v1/models && echo '' && echo '--- Starting benchmark ---' && python3 $BENCHMARK_CMD"

    echo ""
    echo ">>> Benchmark complete. Results are in: $AIDER_DIR/tmp.benchmarks/"
    echo ">>> To view a report, re-run with --shell-only and execute:"
    echo "    python3 benchmark/benchmark_report.py tmp.benchmarks/<result-dir>"
fi
