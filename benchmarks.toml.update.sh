#!/usr/bin/env bash
# Run scripts/generate-benchmarks-toml.sh against the default litellm endpoint
# plus the per-host direct endpoints on gfx1151 and rtx5090.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
GEN="${SCRIPT_DIR}/scripts/generate-benchmarks-toml.sh"

# Default litellm host (uses script defaults for ENDPOINT_URL / ENDPOINT_NAME).
echo "==> litellm (default endpoint)" >&2
"$GEN"

# Direct hosts: host_url | producer | backend_label
hosts=(
    "http://gfx1151.thing.wg0.maxhbr.local|gfx1151|vulkan"
    "http://rtx5090.thing.wg0.maxhbr.local|rtx5090|cuda"
)

for entry in "${hosts[@]}"; do
    IFS='|' read -r host producer backend_label <<<"$entry"
    name="$(printf '%s' "$host" | sed -E 's#^https?://##; s#/.*$##')"
    echo "==> direct: ${name} (${producer}/${backend_label})" >&2
    ENDPOINT_URL="${host%/}/v1" \
    ENDPOINT_NAME="$name" \
    ENDPOINT_BACKEND="direct" \
    ENDPOINT_PRODUCER="$producer" \
    ENDPOINT_BACKEND_LABEL="$backend_label" \
        "$GEN"
done
