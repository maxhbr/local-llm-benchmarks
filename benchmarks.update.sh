#!/usr/bin/env bash
# Regenerate all derived files under benchmarks/.
#
# Runs, in order:
#   1. scripts/extract-aider-stats.py --all benchmarks/
#        -> writes benchmarks/<run>/aider.computed.yaml and benchmarks/datasets.json
#   2. scripts/llama-benchy-md-to-csv.py --all benchmarks/ -o benchmarks/llama-benchy.csv
#        -> writes the master CSV
#   3. scripts/find-fastest.py --benchmarks-dir benchmarks/ -o benchmarks/find-fastest.md
#        -> writes the ranking report
#
# Usage:
#   ./benchmarks.update.sh                 # use default benchmarks/ dir
#   ./benchmarks.update.sh path/to/bench   # use a custom benchmarks dir
#
set -euo pipefail

# Resolve the directory of this script so it works regardless of CWD.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

BENCH_DIR="${1:-benchmarks}"
PY="${PYTHON:-python3}"

if [[ ! -d "$BENCH_DIR" ]]; then
    echo "error: benchmarks directory not found: $BENCH_DIR" >&2
    exit 1
fi

CSV_OUT="$BENCH_DIR/llama-benchy.csv"
FASTEST_OUT="$BENCH_DIR/find-fastest.md"

step() {
    printf '\n\033[1;36m==> %s\033[0m\n' "$*"
}

step "1/3  extract-aider-stats.py --all $BENCH_DIR"
"$PY" scripts/extract-aider-stats.py --all "$BENCH_DIR"

step "2/3  llama-benchy-md-to-csv.py --all $BENCH_DIR -o $CSV_OUT"
"$PY" scripts/llama-benchy-md-to-csv.py --all "$BENCH_DIR" -o "$CSV_OUT"
echo "Wrote $CSV_OUT"

step "3/3  find-fastest.py --benchmarks-dir $BENCH_DIR -o $FASTEST_OUT"
"$PY" scripts/find-fastest.py --benchmarks-dir "$BENCH_DIR" -o "$FASTEST_OUT"

printf '\n\033[1;32mAll done.\033[0m\n'
