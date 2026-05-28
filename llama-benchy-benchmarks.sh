#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$SCRIPT_DIR/work"
LLAMA_BENCHY_DIR="$SCRIPT_DIR/llama-benchy"
IMAGE_NAME="llama-benchy-benchmark"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run llama-benchy benchmarks against a local LLM endpoint inside a container.
Uses the llama-benchy subtree at llama-benchy/ (installed from source).

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
  --model <name>       Model name as recognized by the endpoint (HF format recommended, e.g. org/model)
                       If omitted, lists available models from the endpoint

Options:
  --container-runtime <rt>  Container runtime: podman or docker (default: auto-detected)
  --pp <n ...>              Prompt processing token counts (default: 2048)
  --tg <n ...>              Token generation counts (default: 32)
  --depth <n ...>           Context depths to test at (default: 0)
  --runs <n>                Number of runs per test (default: 3)
  --latency-mode <mode>     Latency measurement: api, generation, or none (default: generation)
  --concurrency <n ...>     Concurrency levels (default: 1)
  --format <fmt>            Output format: md, json, or csv (default: md)
  --enable-prefix-caching   Enable prefix caching measurement
  --no-cache                Add noise to requests to avoid prefix caching
  --no-warmup               Skip warmup phase
  --skip-coherence          Skip coherence test after warmup
  --no-adapt-prompt         Disable prompt size adaptation
  --tokenizer <name>        HuggingFace tokenizer name or local path (default: model name)
  --post-run-cmd <cmd>      Command to execute after each test run
  --save-result <file>      Custom filename for saved results (default: <run-name>-<timestamp>.json in work/results/)
  --no-save-result          Disable automatic result saving
  --rebuild                 Rebuild the container image before running
  --shell-only              Drop into the container shell instead of running the benchmark
  --run-name <name>         Name for this benchmark run (default: derived from model name)
  -h, --help                Show this help message

Examples:
  $(basename "$0") --endpoint http://localhost:8080/v1 --model Qwen/Qwen3-8B
  $(basename "$0") --endpoint http://172.17.0.1:8080/v1 --model my-model --depth 0 4096 8192 --latency-mode generation
  $(basename "$0") --endpoint http://litellm.thing.wg0.maxhbr.local/v1 --model "rtx5090:Qwen3.5-9B-Q5_K_M" --container-runtime docker
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-model --enable-prefix-caching --concurrency 1 2 4 --format json --save-result results.json
EOF
    exit 0
}

ENDPOINT=""
MODEL=""
CONTAINER_RUNTIME=""
PP_VALS=""
TG_VALS=""
DEPTH_VALS=""
RUNS=""
LATENCY_MODE=""
CONCURRENCY_VALS=""
RESULT_FORMAT=""
ENABLE_PREFIX_CACHING=false
NO_CACHE=false
NO_WARMUP=false
SKIP_COHERENCE=false
NO_ADAPT_PROMPT=false
TOKENIZER=""
POST_RUN_CMD=""
SAVE_RESULT=""
NO_SAVE_RESULT=false
REBUILD=false
SHELL_ONLY=false
RUN_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint)              ENDPOINT="$2"; shift 2 ;;
        --model)                 MODEL="$2"; shift 2 ;;
        --container-runtime)     CONTAINER_RUNTIME="$2"; shift 2 ;;
        --pp)                    shift; PP_VALS=(); while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do PP_VALS+=("$1"); shift; done ;;
        --tg)                    shift; TG_VALS=(); while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do TG_VALS+=("$1"); shift; done ;;
        --depth)                 shift; DEPTH_VALS=(); while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do DEPTH_VALS+=("$1"); shift; done ;;
        --runs)                  RUNS="$2"; shift 2 ;;
        --latency-mode)          LATENCY_MODE="$2"; shift 2 ;;
        --concurrency)           shift; CONCURRENCY_VALS=(); while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do CONCURRENCY_VALS+=("$1"); shift; done ;;
        --format)                RESULT_FORMAT="$2"; shift 2 ;;
        --enable-prefix-caching) ENABLE_PREFIX_CACHING=true; shift ;;
        --no-cache)              NO_CACHE=true; shift ;;
        --no-warmup)             NO_WARMUP=true; shift ;;
        --skip-coherence)        SKIP_COHERENCE=true; shift ;;
        --no-adapt-prompt)       NO_ADAPT_PROMPT=true; shift ;;
        --tokenizer)             TOKENIZER="$2"; shift 2 ;;
        --post-run-cmd)          POST_RUN_CMD="$2"; shift 2 ;;
        --save-result)           SAVE_RESULT="$2"; shift 2 ;;
        --no-save-result)        NO_SAVE_RESULT=true; shift ;;
        --rebuild)               REBUILD=true; shift ;;
        --shell-only)            SHELL_ONLY=true; shift ;;
        --run-name)              RUN_NAME="$2"; shift 2 ;;
        -h|--help)               usage ;;
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

# ---------------------------------------------------------------------------
# Build the container image (from the local llama-benchy subtree)
# ---------------------------------------------------------------------------

IMAGE_EXISTS=false
if $CONTAINER_RUNTIME image inspect "$IMAGE_NAME" &>/dev/null; then
    IMAGE_EXISTS=true
fi

if [[ "$IMAGE_EXISTS" == false || "$REBUILD" == true ]]; then
    echo ">>> Building container image from llama-benchy/ ..."

    CONTAINERFILE=$(mktemp /tmp/Containerfile.llama-benchy.XXXXXX)
    cat > "$CONTAINERFILE" <<'DOCKERFILE'
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/llama-benchy
COPY . .

RUN pip install --no-cache-dir .

ENTRYPOINT ["llama-benchy"]
DOCKERFILE

    $CONTAINER_RUNTIME build \
        --file "$CONTAINERFILE" \
        -t "$IMAGE_NAME" \
        "$LLAMA_BENCHY_DIR"

    rm -f "$CONTAINERFILE"
fi

# ---------------------------------------------------------------------------
# Translate localhost URLs for container networking
# ---------------------------------------------------------------------------

CONTAINER_ENDPOINT="$ENDPOINT"
if [[ "$ENDPOINT" =~ ^http://localhost(:[0-9]+)? ]]; then
    if [[ "$CONTAINER_RUNTIME" == "podman" ]]; then
        CONTAINER_ENDPOINT="${ENDPOINT/localhost/host.containers.internal}"
    else
        CONTAINER_ENDPOINT="${ENDPOINT/localhost/host.docker.internal}"
    fi
elif [[ "$ENDPOINT" =~ ^http://127\.0\.0\.1(:[0-9]+)? ]]; then
    if [[ "$CONTAINER_RUNTIME" == "podman" ]]; then
        CONTAINER_ENDPOINT="${ENDPOINT/127.0.0.1/host.containers.internal}"
    else
        CONTAINER_ENDPOINT="${ENDPOINT/127.0.0.1/host.docker.internal}"
    fi
fi

HOST_GATEWAY_FLAG="--add-host=host.containers.internal:host-gateway"
if [[ "$CONTAINER_RUNTIME" == "docker" ]]; then
    HOST_GATEWAY_FLAG="--add-host=host.docker.internal:host-gateway"
fi

# ---------------------------------------------------------------------------
# Assemble the llama-benchy command
# ---------------------------------------------------------------------------

BENCHY_CMD=(llama-benchy --base-url "$CONTAINER_ENDPOINT" --model "$MODEL")

if [[ -n "$TOKENIZER" ]];            then BENCHY_CMD+=(--tokenizer "$TOKENIZER"); fi
if [[ ${#PP_VALS[@]} -gt 0 ]];       then BENCHY_CMD+=(--pp "${PP_VALS[@]}"); fi
if [[ ${#TG_VALS[@]} -gt 0 ]];       then BENCHY_CMD+=(--tg "${TG_VALS[@]}"); fi
if [[ ${#DEPTH_VALS[@]} -gt 0 ]];    then BENCHY_CMD+=(--depth "${DEPTH_VALS[@]}"); fi
if [[ -n "$RUNS" ]];                 then BENCHY_CMD+=(--runs "$RUNS"); fi
if [[ -n "$LATENCY_MODE" ]];         then BENCHY_CMD+=(--latency-mode "$LATENCY_MODE"); fi
if [[ ${#CONCURRENCY_VALS[@]} -gt 0 ]]; then BENCHY_CMD+=(--concurrency "${CONCURRENCY_VALS[@]}"); fi
if [[ -n "$RESULT_FORMAT" ]];        then BENCHY_CMD+=(--format "$RESULT_FORMAT"); fi
if [[ "$ENABLE_PREFIX_CACHING" == true ]]; then BENCHY_CMD+=(--enable-prefix-caching); fi
if [[ "$NO_CACHE" == true ]];        then BENCHY_CMD+=(--no-cache); fi
if [[ "$NO_WARMUP" == true ]];       then BENCHY_CMD+=(--no-warmup); fi
if [[ "$SKIP_COHERENCE" == true ]];  then BENCHY_CMD+=(--skip-coherence); fi
if [[ "$NO_ADAPT_PROMPT" == true ]]; then BENCHY_CMD+=(--no-adapt-prompt); fi
if [[ -n "$POST_RUN_CMD" ]];         then BENCHY_CMD+=(--post-run-cmd "$POST_RUN_CMD"); fi

# ---------------------------------------------------------------------------
# Container run args
# ---------------------------------------------------------------------------

RUN_ARGS=(
    -it --rm
    $HOST_GATEWAY_FLAG
    -e OPENAI_API_KEY=dummy-key
)

# Results saving is on by default (JSON to work/results/)
RESULT_HOST_DIR=""
if [[ "$NO_SAVE_RESULT" != true ]]; then
    RESULT_HOST_DIR="$WORK_DIR/results"
    mkdir -p "$RESULT_HOST_DIR"
    if [[ -n "$SAVE_RESULT" ]]; then
        RESULT_BASENAME="$(basename "$SAVE_RESULT")"
    else
        TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
        RESULT_BASENAME="${RUN_NAME}-${TIMESTAMP}.json"
    fi
    RUN_ARGS+=(-v "$RESULT_HOST_DIR":/results)
    BENCHY_CMD+=(--save-result "/results/$RESULT_BASENAME")
    # Ensure JSON format for structured result file
    if [[ -z "$RESULT_FORMAT" ]]; then
        BENCHY_CMD+=(--format json)
    fi
fi

# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

if [[ "$SHELL_ONLY" == true ]]; then
    RUN_ARGS+=(
        -e HISTFILE=/tmp/.bash_history
        -e PROMPT_COMMAND='history -a'
        -e HISTCONTROL=ignoredups
        -e HISTSIZE=10000
        -e HISTFILESIZE=20000
    )
    echo ">>> Launching container shell (benchmark command will NOT run automatically)..."
    echo ">>> When ready, run inside the container:"
    echo "    ${BENCHY_CMD[*]}"
    $CONTAINER_RUNTIME run "${RUN_ARGS[@]}" "$IMAGE_NAME" bash
else
    echo ">>> Running benchmark..."
    echo "    Runtime:       $CONTAINER_RUNTIME"
    echo "    Model:         $MODEL"
    echo "    Endpoint:      $CONTAINER_ENDPOINT"
    if [[ ${#PP_VALS[@]} -gt 0 ]];       then echo "    PP:            ${PP_VALS[*]}"; fi
    if [[ ${#TG_VALS[@]} -gt 0 ]];       then echo "    TG:            ${TG_VALS[*]}"; fi
    if [[ ${#DEPTH_VALS[@]} -gt 0 ]];    then echo "    Depth:         ${DEPTH_VALS[*]}"; fi
    if [[ -n "$RUNS" ]];                 then echo "    Runs:          $RUNS"; fi
    if [[ -n "$LATENCY_MODE" ]];         then echo "    Latency mode:  $LATENCY_MODE"; fi
    if [[ ${#CONCURRENCY_VALS[@]} -gt 0 ]]; then echo "    Concurrency:   ${CONCURRENCY_VALS[*]}"; fi
    if [[ -n "$RESULT_FORMAT" ]];        then echo "    Format:        $RESULT_FORMAT"; fi
    if [[ "$ENABLE_PREFIX_CACHING" == true ]]; then echo "    Prefix cache:  enabled"; fi
    echo "    Run name:      $RUN_NAME"
    if [[ "$NO_SAVE_RESULT" != true ]]; then
        echo "    Results:       $RESULT_HOST_DIR/$RESULT_BASENAME"
    fi
    echo ""

    LOG_DIR="$WORK_DIR/logs"
    mkdir -p "$LOG_DIR"
    if [[ -z "$TIMESTAMP" ]]; then
        TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    fi
    LOG_FILE="$LOG_DIR/${RUN_NAME}-${TIMESTAMP}.log"
    echo ">>> Logging to $LOG_FILE"

    $CONTAINER_RUNTIME run "${RUN_ARGS[@]}" \
        "$IMAGE_NAME" \
        bash -c "echo '--- Verifying API connectivity ---' && curl -sf ${CONTAINER_ENDPOINT%/v1}/v1/models && echo '' && echo '--- Starting benchmark ---' && ${BENCHY_CMD[*]}" \
        2>&1 | tee "$LOG_FILE"

    echo ""
    if [[ -n "$RESULT_HOST_DIR" && -n "$RESULT_BASENAME" ]]; then
        RESULT_PATH="$RESULT_HOST_DIR/$RESULT_BASENAME"
        if [[ -f "$RESULT_PATH" ]]; then
            echo ">>> Results saved to: $RESULT_PATH"
        fi
    fi
    echo ">>> Benchmark complete. Log saved to: $LOG_FILE"
fi
