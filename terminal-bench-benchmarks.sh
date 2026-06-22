#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<EOF
Usage: $(basename "$0") --endpoint <url> --model <name> [options]

Run terminal-bench benchmarks using Harbor (installed via uv).

Required:
  --endpoint <url>     OpenAI-compatible API base URL (e.g. http://localhost:8080/v1)
  --model, -m <name>   Model identifier as recognized by the endpoint
                       If omitted, lists available models from the endpoint

Common options:
  --api-key, -k <key>       API key for the endpoint
  --output-dir <path>       Root directory for results (default: ./benchmarks)
  --work-dir <path>         Directory for cached harbor install (default: ./work)
  --run-name <name>         Name suffix for this run (default: derived from --model)
  --rebuild                 Reinstall harbor via uv
  --shell-only              Drop into a shell with harbor available; don't run benchmark
  --new                     Re-run even if the output symlink already exists (accepted for consistency; terminal-bench does not yet create a symlink)
  -h, --help                Show this help message

terminal-bench options:
  --agent, -a <name>        Agent/agent-slug to benchmark (default: tbd.)
  --dataset, -d <name>      Dataset identifier (default: terminal-bench@2.0)
  --task, -t <id>           Task name filter (glob pattern, sets --include-task-name)
  --n-concurrent, -n <n>    Number of concurrent trials (default: 1)

Examples:
  $(basename "$0") --endpoint http://localhost:8080/v1 --model my-model
  $(basename "$0") -e http://localhost:8080/v1 -m my-model -a terminus -d terminal-bench@2.0 -t adaptive-rejection-sampler
EOF
    exit 0
}

ENDPOINT=""
API_KEY=""
MODEL=""
OUTPUT_DIR="./benchmarks"
WORK_DIR="./work"
RUN_NAME=""
AGENT="pi"
DATASET="terminal-bench@2.0"
TASK_FILTER=""
N_CONCURRENT=1
REBUILD=false
SHELL_ONLY=false
NEW=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint|-e)        ENDPOINT="$2"; shift 2 ;;
        --api-key|-k)         API_KEY="$2"; shift 2 ;;
        --model|-m)           MODEL="$2"; shift 2 ;;
        --output-dir)         OUTPUT_DIR="$2"; shift 2 ;;
        --work-dir)           WORK_DIR="$2"; shift 2 ;;
        --run-name)           RUN_NAME="$2"; shift 2 ;;
        --agent|-a)           AGENT="$2"; shift 2 ;;
        --dataset|-d)         DATASET="$2"; shift 2 ;;
        --task|-t)            TASK_FILTER="$2"; shift 2 ;;
        --n-concurrent|-n)    N_CONCURRENT="$2"; shift 2 ;;
        --rebuild|--reinstall) REBUILD=true; shift ;;
        --shell-only)         SHELL_ONLY=true; shift ;;
        --new)                NEW=true; shift ;;
        -h|--help)            usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

if [[ -z "$MODEL" ]]; then
    if [[ -n "$ENDPOINT" ]]; then
        echo ">>> No --model specified. Available models from $ENDPOINT/models:"
        echo ""
        list_models "$ENDPOINT" "${API_KEY:-EMPTY}"
        echo ""
        echo "Re-run with --model <name> to start the benchmark."
        exit 0
    fi
    echo "Error: --model is required (and --endpoint to list available models)" >&2
    usage
fi

if [[ -z "$RUN_NAME" ]]; then
    RUN_NAME="$(slugify_model "$MODEL")"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' not found in PATH; use the flake devShell or install it." >&2
    exit 1
fi

mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

# Use a per-project location for uv-installed tools so the script does not
# silently depend on whatever the host's $HOME/.local/bin contains.
export UV_TOOL_BIN_DIR="$WORK_DIR/uv-tools/bin"
export UV_TOOL_DIR="$WORK_DIR/uv-tools"
mkdir -p "$UV_TOOL_BIN_DIR"
export PATH="$UV_TOOL_BIN_DIR:$PATH"

if [[ "$REBUILD" == true || ! -x "$UV_TOOL_BIN_DIR/harbor" ]]; then
    if [[ "$REBUILD" == true ]]; then
        echo ">>> Reinstalling harbor via uv..."
        uv tool uninstall harbor 2>/dev/null || true
    else
        echo ">>> Installing harbor via uv..."
    fi
    uv tool install harbor
fi

if [[ ! -x "$UV_TOOL_BIN_DIR/harbor" ]]; then
    echo "Error: harbor not found at $UV_TOOL_BIN_DIR/harbor after install" >&2
    exit 1
fi
HARBOR_CMD="$UV_TOOL_BIN_DIR/harbor"

# Build the harbor command line so we can echo + reuse it.
HARBOR_ARGS=(
    run
    -d "$DATASET"
    -a "$AGENT"
    -m "$MODEL"
    -n "$N_CONCURRENT"
)
if [[ -n "$TASK_FILTER" ]]; then
    HARBOR_ARGS+=( --include-task-name "$TASK_FILTER" )
fi

exec_env_vars=()
if [[ -n "$ENDPOINT" ]]; then
    exec_env_vars+=("OPENAI_BASE_URL=$ENDPOINT")
fi
if [[ -n "$API_KEY" ]]; then
    exec_env_vars+=("OPENAI_API_KEY=$API_KEY")
fi

if [[ "$SHELL_ONLY" == true ]]; then
    echo ">>> Launching shell with harbor available..."
    echo ">>> When ready, run:"
    if [[ ${#exec_env_vars[@]} -gt 0 ]]; then
        echo "    ${exec_env_vars[*]} $HARBOR_CMD ${HARBOR_ARGS[*]}"
    else
        echo "    $HARBOR_CMD ${HARBOR_ARGS[*]}"
    fi
    exec bash
fi

init_run_dir "$OUTPUT_DIR" "$RUN_NAME" "terminal-bench"

write_meta \
    "bench=terminal-bench" \
    "model=$MODEL" \
    "endpoint=$ENDPOINT" \
    "run_name=$RUN_NAME" \
    "agent=$AGENT" \
    "dataset=$DATASET" \
    "task_filter=$TASK_FILTER" \
    "n_concurrent=$N_CONCURRENT"

echo ">>> Running harbor benchmark..."
echo "    Dataset:        $DATASET"
echo "    Agent:          $AGENT"
echo "    Model:          $MODEL"
echo "    Task:           ${TASK_FILTER:-<all>}"
echo "    Concurrent:     $N_CONCURRENT"
echo "    Harbor:         $HARBOR_CMD"
if [[ -n "$ENDPOINT" ]]; then echo "    Endpoint:       $ENDPOINT"; fi
if [[ -n "$API_KEY"  ]]; then echo "    API Key:        <set>"; fi
echo "    Run dir:        $RUN_DIR"
echo ""

if [[ ${#exec_env_vars[@]} -gt 0 ]]; then
    export "${exec_env_vars[@]}"
fi

# Record the command with the env vars prefixed so the file is replayable.
if [[ ${#exec_env_vars[@]} -gt 0 ]]; then
    write_cmd env "${exec_env_vars[@]}" "$HARBOR_CMD" "${HARBOR_ARGS[@]}"
else
    write_cmd "$HARBOR_CMD" "${HARBOR_ARGS[@]}"
fi

"$HARBOR_CMD" "${HARBOR_ARGS[@]}" 2>&1 | tee "$LOG_FILE"

# Best-effort: copy harbor's own run output into the result dir if it
# uses the standard XDG cache layout.
HARBOR_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/harbor/runs"
if [[ -d "$HARBOR_CACHE" ]]; then
    mkdir -p "$RUN_DIR/harbor-runs"
    # symlink the latest few runs so the user has fast access; keep small.
    find "$HARBOR_CACHE" -maxdepth 1 -mindepth 1 -newer "$LOG_FILE" -print0 2>/dev/null \
        | xargs -0 -I {} ln -snf {} "$RUN_DIR/harbor-runs/" 2>/dev/null || true
fi

echo ""
echo ">>> Benchmark complete."
echo ">>> Run dir: $RUN_DIR"
echo ">>> Log:     $LOG_FILE"
echo ">>> Meta:    $META_FILE"
echo ">>> Cmd:     $CMD_FILE"
