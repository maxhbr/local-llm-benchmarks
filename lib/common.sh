# Shared helpers for local-llm-benchmarks driver scripts.
#
# This file is meant to be sourced, not executed.  It deliberately does *not*
# set `set -euo pipefail` so callers stay in charge of their shell options.
#
#   . "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
#
# Public API:
#   slugify_model <model>            -> stdout: filesystem-safe slug
#   list_models <endpoint> [api-key] -> stdout: one model id per line
#   pick_container_runtime [pref]    -> stdout: "podman" or "docker"
#   init_run_dir <root> <model> <bench>
#       Creates <root>/<slug>/<bench>/<ts>/ and exports:
#         RUN_DIR, LOG_FILE, META_FILE, MODEL_SLUG, RUN_TS
#   write_meta <key=value> [...]
#       Writes/updates "$META_FILE" as a JSON object.
#
# All helpers are intentionally portable across bash 4+; no external deps
# beyond curl, python3 (fallback when jq is missing) and coreutils.

# shellcheck shell=bash

# Turn a model id like "rtx5090:Qwen3.6-35B-A3B/Q5_K_M" into
# "rtx5090-Qwen3.6-35B-A3B_Q5_K_M".
slugify_model() {
    local model="$1"
    printf '%s' "$model" | sed 's|/|_|g; s|:|-|g; s|[^a-zA-Z0-9._-]|-|g'
}

# Fetch the list of model ids from an OpenAI-compatible endpoint.
# Falls back to python3 when jq is unavailable; if both fail, dumps the
# raw response so the user still gets something useful.
list_models() {
    local endpoint="$1"
    local api_key="${2:-EMPTY}"
    local url="${endpoint%/v1}/v1/models"
    if command -v jq >/dev/null 2>&1; then
        curl -sf -H "Authorization: Bearer $api_key" "$url" \
            | jq -r '.data[].id' 2>/dev/null \
            || curl -sf -H "Authorization: Bearer $api_key" "$url"
    else
        curl -sf -H "Authorization: Bearer $api_key" "$url" \
            | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]" 2>/dev/null \
            || curl -sf -H "Authorization: Bearer $api_key" "$url"
    fi
}

# Pick a container runtime.  If $1 is given and non-empty, validate it;
# otherwise prefer podman, then docker.
pick_container_runtime() {
    local pref="${1:-}"
    if [[ -n "$pref" ]]; then
        case "$pref" in
            podman|docker) printf '%s' "$pref"; return 0 ;;
            *) echo "Error: container runtime must be 'podman' or 'docker' (got '$pref')" >&2; return 1 ;;
        esac
    fi
    if command -v podman >/dev/null 2>&1; then
        printf '%s' "podman"
    elif command -v docker >/dev/null 2>&1; then
        printf '%s' "docker"
    else
        echo "Error: neither podman nor docker found in PATH" >&2
        return 1
    fi
}

# Create <root>/<slug>/<bench>/<ts>/ and export standard env vars.
#
#   init_run_dir <root> <model> <bench>
#
# Exports: RUN_DIR, LOG_FILE, META_FILE, MODEL_SLUG, RUN_TS
init_run_dir() {
    local root="$1"
    local model="$2"
    local bench="$3"
    if [[ -z "$root" || -z "$model" || -z "$bench" ]]; then
        echo "init_run_dir: usage: init_run_dir <root> <model> <bench>" >&2
        return 2
    fi
    MODEL_SLUG="$(slugify_model "$model")"
    RUN_TS="$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$root"
    # Make the root absolute so callers can `cd` later without breaking
    # any --save-result / log paths derived from $RUN_DIR.
    root="$(cd "$root" && pwd)"
    RUN_DIR="$root/$MODEL_SLUG/$bench/$RUN_TS"
    LOG_FILE="$RUN_DIR/run.log"
    META_FILE="$RUN_DIR/meta.json"
    mkdir -p "$RUN_DIR"
    export RUN_DIR LOG_FILE META_FILE MODEL_SLUG RUN_TS
}

# Write a meta.json sidecar.  Each argument is key=value.  Values that
# look like JSON (start with { [ " or are a bare number / true / false /
# null) are inlined; everything else is JSON-string-quoted.
write_meta() {
    if [[ -z "${META_FILE:-}" ]]; then
        echo "write_meta: \$META_FILE is not set; call init_run_dir first" >&2
        return 2
    fi
    local first=1
    {
        printf '{\n'
        printf '  "timestamp": "%s",\n' "${RUN_TS:-$(date +%Y%m%d-%H%M%S)}"
        printf '  "host": "%s",\n' "$(hostname 2>/dev/null || echo unknown)"
        if git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --short HEAD >/dev/null 2>&1; then
            printf '  "git_rev": "%s",\n' "$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --short HEAD)"
        fi
        for kv in "$@"; do
            local k="${kv%%=*}"
            local v="${kv#*=}"
            if [[ $first -eq 0 ]]; then printf ',\n'; fi
            first=0
            printf '  "%s": ' "$k"
            if [[ "$v" =~ ^[\{\[\"] || "$v" =~ ^-?[0-9]+(\.[0-9]+)?$ || "$v" == "true" || "$v" == "false" || "$v" == "null" ]]; then
                printf '%s' "$v"
            else
                # JSON-escape backslashes and double quotes.
                local esc="${v//\\/\\\\}"
                esc="${esc//\"/\\\"}"
                printf '"%s"' "$esc"
            fi
        done
        printf '\n}\n'
    } > "$META_FILE"
}
