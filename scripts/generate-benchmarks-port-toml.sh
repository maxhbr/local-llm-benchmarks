#!/usr/bin/env bash
# Generate benchmarks.<port>.toml for an OpenAI-compatible router on
# <host>:<port> (default localhost), listing the models it serves that are
# NOT already covered by an existing benchmarks.*.toml in the repo root.
#
# Default behaviour reproduces the hand-built benchmarks.4000.toml:
#   * cloud-proxied API models only
#     (skainet*/, skainet-external/*, trustedtokens/*)
#   * all five benchmarks enabled ("smoke" is implicit in the runner and is
#     never listed: run_benchmarks.py auto-prepends it and gates the rest)
#
# The "not already covered" set is: (all /v1/models ids on the endpoint) minus
# (every `name = "..."` value in existing repo-root benchmarks.*.toml, excluding
# the target file itself so re-runs are stable), then filtered:
#   * drop LiteLLM router aliases: any id containing hermes / opencode / sidekick
#     (case-insensitive, any position — catches prefixed and bare aliases)
#   * drop bare round-robin aliases: localhost:<port>:localhost:<port>
#   * drop *-mmproj vision-projector companions (not standalone chat models)
#
# No per-model `alias` is written: run_benchmarks.py slugifies the model id for
# the output dir (skainet/Qwen/... -> skainet_Qwen_...; rtx5090:Foo -> rtx5090-Foo),
# matching the generated benchmarks.litellm.*.toml convention.
#
# Usage:
#   ./scripts/generate-benchmarks-port-toml.sh 4000
#   ./scripts/generate-benchmarks-port-toml.sh 4000 --scope all
#   ./scripts/generate-benchmarks-port-toml.sh 4000 --scope local \
#       --benchmarks llama-benchy,agent-bench
#   PORT=4000 ./scripts/generate-benchmarks-port-toml.sh
#   PORT=4000 SCOPE=cloud BENCHMARKS=llama-benchy ./scripts/generate-benchmarks-port-toml.sh
#
# Options / env vars:
#   PORT (arg or env)        router port (required)
#   --host <h> / HOST        router host (default: localhost)
#   --scope <s> / SCOPE      cloud | local | all  (default: cloud)
#   --benchmarks <b> / BENCHMARKS
#                            comma-separated benchmark ids
#                            (default: all five; "smoke" is never a choice)
#   --name <n> / ENDPOINT_NAME
#                            endpoint name field (default: litellm-<port>)
#   --api-key <k> / API_KEY  bearer token (default: EMPTY)
#   --output-dir <d> / OUTPUT_DIR_VALUE
#                            output_dir field (default: ./benchmarks)
#   --out-dir <d> / OUT_DIR  where to write the toml (default: repo root)
#   --force / FORCE=1        overwrite an existing benchmarks.<port>.toml
#
set -euo pipefail

PORT="${PORT:-}"
HOST="${HOST:-localhost}"
SCOPE="${SCOPE:-cloud}"
BENCHMARKS="${BENCHMARKS:-agent-bench,aider,llama-benchy,terminal-bench,tool-eval-bench}"
ENDPOINT_NAME="${ENDPOINT_NAME:-}"
API_KEY="${API_KEY:-EMPTY}"
OUTPUT_DIR_VALUE="${OUTPUT_DIR_VALUE:-./benchmarks}"
OUT_DIR="${OUT_DIR:-}"
FORCE="${FORCE:-0}"

usage() {
    sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage ;;
        --host)        HOST="$2"; shift 2 ;;
        --scope)       SCOPE="$2"; shift 2 ;;
        --benchmarks)  BENCHMARKS="$2"; shift 2 ;;
        --name)        ENDPOINT_NAME="$2"; shift 2 ;;
        --api-key)     API_KEY="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR_VALUE="$2"; shift 2 ;;
        --out-dir)     OUT_DIR="$2"; shift 2 ;;
        --force)       FORCE=1; shift ;;
        --) shift; break ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *)
            if [[ -z "$PORT" ]]; then
                PORT="$1"
            else
                echo "unexpected argument: $1 (port already set to $PORT)" >&2
                exit 2
            fi
            shift
            ;;
    esac
done

[[ -n "$PORT" ]] || { echo "error: PORT is required (positional arg or PORT env)" >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "error: port must be numeric, got '$PORT'" >&2; exit 2; }
case "$SCOPE" in
    cloud|local|all) ;;
    *) echo "error: --scope must be cloud|local|all, got '$SCOPE'" >&2; exit 2 ;;
esac

command -v curl >/dev/null || { echo "error: curl is required" >&2; exit 1; }
command -v jq   >/dev/null || { echo "error: jq is required" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
[[ -n "$OUT_DIR" ]] || OUT_DIR="$REPO_ROOT"
[[ -n "$ENDPOINT_NAME" ]] || ENDPOINT_NAME="litellm-${PORT}"

OUT_FILE="${OUT_DIR}/benchmarks.${PORT}.toml"
if [[ -e "$OUT_FILE" && "$FORCE" != 1 ]]; then
    echo "error: $OUT_FILE already exists; re-run with --force (or FORCE=1) to overwrite" >&2
    exit 1
fi

# Full set of selectable benchmark ids known to run_benchmarks.py's BENCHMARKS
# map (verify with `run_benchmarks.py --list-benchmarks`). "smoke" is
# deliberately absent: the runner always runs it and gates the others on it.
ALL_BENCHMARKS=(agent-bench aider llama-benchy terminal-bench tool-eval-bench)

# Convert "a, b, c" -> TOML array literal: ["a", "b", "c"]
benchmarks_toml_array() {
    local raw="$1" IFS=',' out="[" first=1 p
    local -a parts
    read -r -a parts <<<"$raw"
    for p in "${parts[@]}"; do
        p="${p#"${p%%[![:space:]]*}"}"; p="${p%"${p##*[![:space:]]}"}"
        [[ -z "$p" ]] && continue
        if (( first )); then out+="\"$p\""; first=0
        else out+=", \"$p\""; fi
    done
    out+="]"
    printf '%s' "$out"
}

# Print the "# remaining options:" comment line for the ids NOT in the active
# comma-separated list. Empty string when every option is active.
benchmarks_options_comment() {
    local active p
    active="$(printf '%s\n' "$1" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    local -a remaining=()
    for p in "${ALL_BENCHMARKS[@]}"; do
        grep -qxF -- "$p" <<<"$active" || remaining+=("$p")
    done
    if (( ${#remaining[@]} == 0 )); then
        printf '%s' ""
    else
        local out="# remaining options:" first=1
        for p in "${remaining[@]}"; do
            if (( first )); then out+=" \"$p\""; first=0
            else out+=", \"$p\""; fi
        done
        printf '%s' "$out"
    fi
}

BENCHMARKS_ARRAY="$(benchmarks_toml_array "$BENCHMARKS")"
BENCHMARKS_OPTIONS_COMMENT="$(benchmarks_options_comment "$BENCHMARKS")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

port_models="$work/port_models.txt"
existing="$work/existing.txt"
delta="$work/delta.txt"
filtered="$work/filtered.txt"
scoped="$work/scoped.txt"
bucketed="$work/bucketed.txt"

echo "Fetching models from http://${HOST}:${PORT}/v1/models ..." >&2
if ! curl -fsS -H "Authorization: Bearer ${API_KEY}" "http://${HOST}:${PORT}/v1/models" \
        | jq -r '.data[].id' | sort -u > "$port_models"; then
    echo "error: could not list models at http://${HOST}:${PORT}/v1/models" >&2
    echo "       (confirm the service is running and the port is correct)" >&2
    exit 1
fi
port_n="$(wc -l < "$port_models")"

# Existing `name =` values from every repo-root benchmark toml EXCEPT the target
# file, so re-running on the same port yields a stable delta rather than empty.
{
    for f in "$REPO_ROOT"/benchmarks.*.toml; do
        [[ -e "$f" ]] || continue
        [[ "$f" -ef "$OUT_FILE" ]] && continue
        sed -nE 's/^[[:space:]]*name[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' "$f"
    done
} | sort -u > "$existing"
existing_n="$(wc -l < "$existing")"

# Delta = on-port minus already-covered.
comm -23 "$port_models" "$existing" > "$delta"
delta_n="$(wc -l < "$delta")"

# Junk filter: LiteLLM router aliases, bare round-robin aliases, *-mmproj.
awk '
    {
        id=$0; low=tolower(id)
        if (low ~ /hermes/ || low ~ /opencode/ || low ~ /sidekick/) next
        if (id ~ /^localhost:[0-9]+:localhost:[0-9]+$/) next
        if (id ~ /-mmproj$/) next
        print
    }
' "$delta" | sort > "$filtered"
filtered_n="$(wc -l < "$filtered")"

# Scope filter.
case "$SCOPE" in
    cloud)
        grep -E '^(skainet(-external)?|trustedtokens)/' "$filtered" > "$scoped" || true
        scope_desc="cloud-proxied API models (skainet*/, skainet-external/*, trustedtokens/*)"
        ;;
    local)
        grep -E '^(gfx1151:|rtx5090:)' "$filtered" > "$scoped" || true
        scope_desc="local models (gfx1151:*, gfx1151:ROCm0:*, rtx5090:*)"
        ;;
    all)
        cp "$filtered" "$scoped"
        scope_desc="all models (cloud + local)"
        ;;
esac
scoped_n="$(wc -l < "$scoped")"

if [[ "$scoped_n" -eq 0 ]]; then
    echo "error: no models left after scope='$SCOPE' filter." >&2
    echo "       port reported $port_n models; $delta_n not in existing tomls;" >&2
    echo "       $filtered_n after junk filter; 0 in scope." >&2
    exit 1
fi

# Bucket each model into exactly one group for readable section comments.
awk '
    /^skainet-external\// { print "skainet-external\t" $0; next }
    /^skainet\//          { print "skainet\t" $0; next }
    /^trustedtokens\//    { print "trustedtokens\t" $0; next }
    /^gfx1151:ROCm0:/     { print "gfx1151-ROCm0\t" $0; next }
    /^gfx1151:/           { print "gfx1151\t" $0; next }
    /^rtx5090:/           { print "rtx5090\t" $0; next }
                          { print "other\t" $0 }
' "$scoped" | sort > "$bucketed"

emit_bucket() {
    # $1 = bucket key, $2 = label
    local key="$1" label="$2" lines
    lines="$(awk -F'\t' -v k="$key" '$1==k {print $2}' "$bucketed")"
    [[ -z "$lines" ]] && return 0
    printf '\n  # --- %s ---\n' "$label"
    printf '%s\n' "$lines" | awk '{print "  [[endpoints.models]]"; print "  name = \"" $0 "\""}'
}

echo "Writing $OUT_FILE ($scoped_n models, scope=$SCOPE, benchmarks=$BENCHMARKS)" >&2
{
    cat <<HDR
# Benchmark config for the OpenAI-compatible router on ${HOST}:${PORT}.
#
# "Not already covered" = models this endpoint reports that are NOT in any
# existing repo-root benchmarks.*.toml (the target file itself is excluded from
# that comparison, so re-runs are stable). Computed as:
#   (all /v1/models ids on ${HOST}:${PORT}) minus (every \`name =\` in existing
#   benchmark tomls), then filtered:
#     * dropped LiteLLM router aliases: hermes*, opencode*, sidekick* (any
#       position, case-insensitive)
#     * dropped bare round-robin aliases: localhost:<port>:localhost:<port>
#     * dropped *-mmproj vision-projector companions (not standalone chat models)
#   ${port_n} ids reported -> ${delta_n} not in existing tomls -> ${filtered_n}
#   after junk filter -> ${scoped_n} kept (scope=${SCOPE}: ${scope_desc}).
#
# Benchmarks: ${BENCHMARKS}. "smoke" runs first implicitly and gates the rest;
# on smoke failure that model's remaining benchmarks are skipped for the run.
# No per-model \`alias\`: run_benchmarks.py slugifies the model id for the output
# dir (skainet/Qwen/... -> skainet_Qwen_...; rtx5090:Foo -> rtx5090-Foo),
# matching the generated benchmarks.litellm.*.toml convention.

output_dir = "${OUTPUT_DIR_VALUE}"

[[endpoints]]
name       = "${ENDPOINT_NAME}"
url        = "http://${HOST}:${PORT}/v1"
api_key    = "${API_KEY}"
benchmarks = ${BENCHMARKS_ARRAY}
HDR
    if [[ -n "$BENCHMARKS_OPTIONS_COMMENT" ]]; then
        printf '%s\n' "$BENCHMARKS_OPTIONS_COMMENT"
    fi
    printf '\n'
    emit_bucket skainet-external "skainet-external (cloud-proxied API models)"
    emit_bucket skainet          "skainet (cloud-proxied API models)"
    emit_bucket trustedtokens   "trustedtokens (cloud-proxied API models)"
    emit_bucket gfx1151-ROCm0   "gfx1151:ROCm0 (local, rocm)"
    emit_bucket gfx1151         "gfx1151 (local)"
    emit_bucket rtx5090         "rtx5090 (local, cuda)"
    emit_bucket other           "other"
    printf '\n'
} > "$OUT_FILE"

echo "Wrote $OUT_FILE" >&2
echo "  ${port_n} on ${HOST}:${PORT} | ${delta_n} new | ${filtered_n} after junk | ${scoped_n} in scope (${SCOPE})" >&2
echo "Done." >&2
