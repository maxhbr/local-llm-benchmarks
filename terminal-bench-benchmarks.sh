#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$SCRIPT_DIR/work"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Run terminal-bench benchmarks using Harbor (installed via uv) against a configured model.

Required:
  -m, --model <name>       Model identifier (e.g. anthropic/claude-sonnet-4-20250514)

Options:
  -a, --agent <name>       Agent/agent-slug to benchmark (default: terminus)
  -d, --dataset <name>     Dataset identifier (default: terminal-bench@2.0)
  -t, --task <id>          Task name filter (glob pattern, sets --include-task-name)
  -e, --endpoint <url>     OpenAI-compatible API endpoint (sets OPENAI_BASE_URL env var for the agent)
  -k, --api-key <key>      API key for the endpoint (sets OPENAI_API_KEY env var for the agent)
  -n, --n-concurrent <n>   Number of concurrent trials (default: 1)
  --reinstall              Reinstall harbor via uv
  --shell-only             Drop into the shell with harbor available instead of running
  -h, --help               Show this help message

Examples:
  $(basename "$0") --model anthropic/claude-sonnet-4-20250514
  $(basename "$0") -m anthropic/claude-sonnet-4-20250514 -a terminus -d terminal-bench@2.0 -t adaptive-rejection-sampler
  $(basename "$0") -m rtx5090:Qwen3.6-35B-A3B-UD-Q5_K_XL -e http://litellm.thing.wg0.maxhbr.local/v1
  $(basename "$0") -m rtx5090:Qwen3.6-35B-A3B-UD-Q5_K_XL -e http://litellm.thing.wg0.maxhbr.local/v1 -k your-api-key
EOF
    exit 0
}

MODEL=""
AGENT="terminus"
DATASET="terminal-bench@2.0"
TASK_FILTER="adaptive-rejection-sampler"
ENDPOINT=""
API_KEY=""
N_CONCURRENT=1
REINSTALL=false
SHELL_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--model)           MODEL="$2"; shift 2 ;;
        -a|--agent)           AGENT="$2"; shift 2 ;;
        -d|--dataset)         DATASET="$2"; shift 2 ;;
        -t|--task)            TASK_FILTER="$2"; shift 2 ;;
        -e|--endpoint)        ENDPOINT="$2"; shift 2 ;;
        -k|--api-key)         API_KEY="$2"; shift 2 ;;
        -n|--n-concurrent)    N_CONCURRENT="$2"; shift 2 ;;
        --reinstall)          REINSTALL=true; shift ;;
        --shell-only)         SHELL_ONLY=true; shift ;;
        -h|--help)            usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$MODEL" ]]; then
    echo ">>> No --model specified. Re-run with --model <name> to start the benchmark."
    echo ""
    echo "Examples:"
    echo "  $(basename "$0") --model anthropic/claude-sonnet-4-20250514"
    echo "  $(basename "$0") -m rtx5090:Qwen3.6-35B-A3B-UD-Q5_K_XL -e http://litellm.thing.wg0.maxhbr.local/v1"
    exit 0
fi

mkdir -p "$WORK_DIR"

# Install harbor via uv if not already installed or if --reinstall
if [[ "$REINSTALL" == true || ! -x "$(which harbor 2>/dev/null || true)" ]]; then
    echo ">>> Installing harbor via uv..."
    if [[ "$REINSTALL" == true ]]; then
        echo ">>> Reinstalling harbor..."
        uv tool uninstall harbor 2>/dev/null || true
    fi
    uv tool install harbor
    echo ">>> Harbor installed"
fi

# Ensure harbor is in PATH (uv tool installs to ~/.local/bin)
if [[ ! -x "$(which harbor 2>/dev/null || true)" ]]; then
    export PATH="/home/mhuber/.local/bin:$PATH"
    if [[ ! -x "$(which harbor 2>/dev/null || true)" ]]; then
        echo "Error: harbor not found after installation" >&2
        exit 1
    fi
fi

HARBOR_CMD="$(which harbor)"

if [[ "$SHELL_ONLY" == true ]]; then
    echo ">>> Launching shell with harbor available..."
    echo ">>> When ready, run:"
    echo "    harbor run -d $DATASET -a $AGENT -m $MODEL --include-task-name $TASK_FILTER"
    if [[ -n "$ENDPOINT" ]]; then
        echo "    # With custom endpoint:"
        echo "    OPENAI_BASE_URL=$ENDPOINT harbor run -d $DATASET -a $AGENT -m $MODEL --include-task-name $TASK_FILTER"
    fi
    if [[ -n "$API_KEY" ]]; then
        echo "    # With API key:"
        echo "    OPENAI_API_KEY=$API_KEY harbor run -d $DATASET -a $AGENT -m $MODEL --include-task-name $TASK_FILTER"
    fi
    exec bash
else
    echo ">>> Running harbor benchmark..."
    echo "    Dataset:        $DATASET"
    echo "    Agent:          $AGENT"
    echo "    Model:          $MODEL"
    echo "    Task:           $TASK_FILTER"
    echo "    Concurrent:     $N_CONCURRENT"
    echo "    Harbor:         $HARBOR_CMD"
    if [[ -n "$ENDPOINT" ]]; then
        echo "    Endpoint:       $ENDPOINT"
    fi
    if [[ -n "$API_KEY" ]]; then
        echo "    API Key:        <set>"
    fi
    echo ""

    # Create logs directory
    LOG_DIR="$SCRIPT_DIR/logs"
    mkdir -p "$LOG_DIR"
    TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    LOG_FILE="$LOG_DIR/${AGENT}-${TASK_FILTER}-${TIMESTAMP}.log"
    echo ">>> Logging to $LOG_FILE"
    echo ""

    echo ">>> Executing:"
    if [[ -n "$ENDPOINT" || -n "$API_KEY" ]]; then
        echo "    OPENAI_BASE_URL=${ENDPOINT:-} OPENAI_API_KEY=${API_KEY:-} $HARBOR_CMD run \\"
    else
        echo "    $HARBOR_CMD run \\"
    fi
    echo "        -d $DATASET \\"
    echo "        -a $AGENT \\"
    echo "        -m $MODEL \\"
    echo "        --include-task-name $TASK_FILTER \\"
    echo "        -n $N_CONCURRENT"
    echo ""

    # Set env vars and run harbor
    exec_env_vars=()
    if [[ -n "$ENDPOINT" ]]; then
        exec_env_vars+=("OPENAI_BASE_URL=$ENDPOINT")
    fi
    if [[ -n "$API_KEY" ]]; then
        exec_env_vars+=("OPENAI_API_KEY=$API_KEY")
    fi

    if [[ ${#exec_env_vars[@]} -gt 0 ]]; then
        export "${exec_env_vars[@]}"
    fi

    $HARBOR_CMD run \
        -d "$DATASET" \
        -a "$AGENT" \
        -m "$MODEL" \
        --include-task-name "$TASK_FILTER" \
        -n "$N_CONCURRENT" \
        2>&1 | tee "$LOG_FILE"

    echo ""
    echo ">>> Benchmark complete."
    echo ">>> Log saved to: $LOG_FILE"
fi
