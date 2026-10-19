#!/usr/bin/env bash
# Regenerate all derived files under benchmarks/.
#
# Runs, in order:
#   1. scripts/extract-aider-stats.py --all benchmarks/
#        -> writes benchmarks/<run>/aider.computed.yaml and benchmarks/datasets.json
#   2. scripts/generate-aider-pertest-manifest.py --root benchmarks/
#        -> writes benchmarks/aider-pertest.json + .csv (per-test-case × model matrix)
#   3. scripts/llama-benchy-md-to-csv.py --all benchmarks/ -o benchmarks/llama-benchy.csv
#        -> writes the master CSV
#   4. scripts/find-fastest.py --benchmarks-dir benchmarks/ -o benchmarks/find-fastest.md
#        -> writes the ranking report
#   5. scripts/generate-agent-bench-manifest.py --root benchmarks/_agent-bench-scoreboards
#        -> writes benchmarks/_agent-bench-scoreboards/manifest.json (agent-bench histories)
#   6. scripts/generate-tool-eval-manifest.py --root benchmarks
#        -> writes benchmarks/_tool-eval-scoreboards/manifest.json (tool-eval-bench runs)
#   7. scripts/generate-llmprobe-manifest.py --root benchmarks
#        -> writes benchmarks/_llmprobe-scoreboards/manifest.json (native llmprobe reports)
#   8. python3 -m http.server --directory benchmarks <port>
#        -> serves the benchmarks folder over HTTP   (skip with --no-serve)
#
# Usage:
#   ./benchmarks.update.sh                       # use default benchmarks/ dir
#   ./benchmarks.update.sh path/to/bench         # use a custom benchmarks dir
#   ./benchmarks.update.sh --no-serve [benchdir] # regenerate only, do not serve
#
set -euo pipefail

# Resolve the directory of this script so it works regardless of CWD.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

SERVE=1
BENCH_DIR="benchmarks"
for arg in "$@"; do
    case "$arg" in
        --no-serve|--update-only) SERVE=0 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) BENCH_DIR="$arg" ;;
    esac
done
PY="${PYTHON:-python3}"

if [[ ! -d "$BENCH_DIR" ]]; then
    echo "error: benchmarks directory not found: $BENCH_DIR" >&2
    exit 1
fi

CSV_OUT="$BENCH_DIR/llama-benchy.csv"
FASTEST_OUT="$BENCH_DIR/find-fastest.md"
HTTP_PORT="${HTTP_PORT:-8000}"

step() {
    printf '\n\033[1;36m==> %s\033[0m\n' "$*"
}

step "1/8  extract-aider-stats.py --all $BENCH_DIR"
"$PY" scripts/extract-aider-stats.py --all "$BENCH_DIR"

step "2/8  generate-aider-pertest-manifest.py --root $BENCH_DIR"
"$PY" scripts/generate-aider-pertest-manifest.py --root "$BENCH_DIR"

step "3/8  llama-benchy-md-to-csv.py --all $BENCH_DIR -o $CSV_OUT"
"$PY" scripts/llama-benchy-md-to-csv.py --all "$BENCH_DIR" -o "$CSV_OUT"
echo "Wrote $CSV_OUT"

step "4/8  find-fastest.py --benchmarks-dir $BENCH_DIR -o $FASTEST_OUT"
"$PY" scripts/find-fastest.py --benchmarks-dir "$BENCH_DIR" -o "$FASTEST_OUT"

step "5/8  generate-agent-bench-manifest.py --root $BENCH_DIR/_agent-bench-scoreboards"
mkdir -p "$BENCH_DIR/_agent-bench-scoreboards"
"$PY" scripts/generate-agent-bench-manifest.py --root "$BENCH_DIR/_agent-bench-scoreboards"

TOOL_EVAL_OUT="$BENCH_DIR/_tool-eval-scoreboards/manifest.json"
step "6/8  generate-tool-eval-manifest.py --root $BENCH_DIR"
"$PY" scripts/generate-tool-eval-manifest.py --root "$BENCH_DIR" -o "$TOOL_EVAL_OUT"
echo "Wrote $TOOL_EVAL_OUT"

LLMPROBE_OUT="$BENCH_DIR/_llmprobe-scoreboards/manifest.json"
step "7/8  generate-llmprobe-manifest.py --root $BENCH_DIR"
"$PY" scripts/generate-llmprobe-manifest.py --root "$BENCH_DIR" -o "$LLMPROBE_OUT"
echo "Wrote $LLMPROBE_OUT"

if [[ "$SERVE" -eq 1 ]]; then
    step "8/8  http.server --directory $BENCH_DIR :$HTTP_PORT"
    printf '\n\033[1;32mAll done. Serving %s at http://localhost:%s/\033[0m\n' "$BENCH_DIR" "$HTTP_PORT"
    exec "$PY" -m http.server --directory "$BENCH_DIR" "$HTTP_PORT"
else
    printf '\n\033[1;32mUpdate complete (--no-serve: HTTP server not started).\033[0m\n'
fi
