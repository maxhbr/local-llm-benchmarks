---
name: create-bench-toml
description: Create a new benchmark TOML config for run_benchmarks.py by interviewing the user (endpoint URL/port, endpoint name, model, alias), listing available models via the service's /v1/models endpoint, and writing a ready-to-run benchmarks.*.toml for any of the six benchmarks (smoke, llama-benchy, agent-bench, aider, terminal-bench, tool-eval-bench), or auto-generating configs for a LiteLLM/single-endpoint fleet via scripts/generate-benchmarks-toml.sh. Use when the user asks to "add a new benchmark config", "create a toml for <service>", "benchmark the service on port X", or similar.
---

# Create a Benchmark TOML

Guide the user step by step through creating a new TOML config for
`run_benchmarks.py` in this repo, then write the file and verify it.

## Step 1 — Gather the endpoint

Ask the user for the service address if not already given. Accept either:
- a host+port, e.g. `localhost:22545` (build `http://localhost:22545/v1`), or
- a full base URL, e.g. `http://10.0.0.5:8080/v1` (LiteLLM, llama.cpp, vLLM, ...).

Also ask for:
- **endpoint name** — a short label used in the TOML `name = "..."` field
  (suggest deriving it from host/port or model, e.g. `ds4`, `local-22545`).
- **api_key** — optional, default `EMPTY`.

## Step 2 — Query the service for available models

```bash
curl -s -H "Authorization: Bearer EMPTY" http://<host>:<port>/v1/models
```

(Use the real api_key if the user gave one.)

- If the request fails, tell the user and ask them to confirm the
  service is running / the port is right. Do not proceed until it works.
- Parse the JSON `data[].id` list and present the models as a numbered
  list to the user.
- If the user already named a model that exactly matches an id, confirm
  it. If it only partially matches one or more ids, show the matches and
  ask which one. If the service reports many models and the user wants
  "all of them", skip listing and omit `[[endpoints.models]]` from the
  TOML (the runner auto-fetches all models).

## Step 3 — Ask for the alias

The **alias** is a short human-friendly name used for output directories
and the summary table. Suggest one from the model id (e.g. model id
`gguf/DeepSeek-V4-Flash-...-Q4K-...` → alias `deepseek-v4-flash-q4k`).
Confirm with the user.

## Step 4 — Pick benchmarks

Show the full benchmark table and let the user choose (default:
`["llama-benchy"]`; `smoke` is not an option or required — the runner
always prepends it and gates the rest on it). The canonical id list lives in
the `BENCHMARKS` map in `run_benchmarks.py`; verify with
`./run_benchmarks.py --list-benchmarks`.

| id                | driver                       | what it runs |
|-------------------|------------------------------|--------------|
| `smoke`             | (inline in run_benchmarks.py) | tiny liveness check ("Say 'pong'"); always auto-run first for every model (never needs to be listed) and gates the others — on failure that model's remaining benchmarks are skipped; writes `smoke.ok` / `smoke.fail`, skips if ok marker exists |
| `llama-benchy`      | `llama-benchy-benchmarks.sh` | prompt/decode tokens-per-second sweep (pp2048, tg128, …) |
| `agent-bench`       | `agent_bench.py` | structured tool-call + unit-conversion + summary task; scoreboard in `benchmarks/_agent-bench-scoreboards/` |
| `aider`             | `aider-polyglot-benchmarks.sh` | Aider polyglot coding exercises in a container (needs a coder-ish model) |
| `terminal-bench`    | `terminal-bench-benchmarks.sh` | terminal-task agent benchmark via Harbor (reasoning) |
| `tool-eval-bench`   | `tool-eval-bench-benchmarks.sh` | 69 multi-turn tool-call quality scenarios (`--short` 15, `--hardmode` 84); endpoint must support the OpenAI `tools` API; optional `--perf` throughput sweep |

Selection notes:

- `aider` needs a coder-ish model. If it is selected, ask whether the model
  is good at `diff` editing and set `edit_format = "diff"` on the model entry
  (default is `"whole"`).
- `terminal-bench` and `tool-eval-bench` are slow; leave them out for a
  quick run.
- `smoke` needs no selection: it always runs first for each model and, when
  it fails, the model's remaining benchmarks are skipped for that run.
  (`--only` entries without `smoke` drop the check too.)

## Step 5 — Write the TOML

Write the file to the repo root, named `benchmarks.<endpoint-name>.toml`
(use a different name if that file already exists; never overwrite
without confirming). Template:

```toml
output_dir = "./benchmarks"

[[endpoints]]
name       = "<endpoint-name>"
url        = "http://<host>:<port>/v1"
api_key    = "EMPTY"
benchmarks = ["llama-benchy"]
# remaining options: "agent-bench", "aider", "terminal-bench", "tool-eval-bench"

  [[endpoints.models]]
  name = "<exact model id from /v1/models>"
  alias = "<friendly alias>"
  # benchmarks = ["aider"]        # optional per-model override
  # edit_format = "diff"          # optional, aider only
```

Omit the `[[endpoints.models]]` block entirely if the user wants every
model the service reports.

Benchmark list convention: the active `benchmarks` array stays minimal — by
default only `["llama-benchy"]`.  `smoke` is deliberately **not** listed:
`run_benchmarks.py` auto-inserts it first for every model and skips the
model's remaining benchmarks when it fails.  The options that are *not*
active are written as a comment line directly after the array, so they stay
visible without being enabled. If the user picks a different subset, move the
chosen ids into the active array and keep only the unchosen ones in the
comment (drop the comment once every option is active). This mirrors
`scripts/generate-benchmarks-toml.sh`, which also defaults to
`BENCHMARKS="llama-benchy"`.

## Step 6 — Verify and hand over the run command

```bash
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml --dry-run
```

Confirm the planned jobs look right (correct endpoint, model, alias,
benchmarks). Then give the user the commands to run for real:

```bash
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml            # run once
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml --new      # re-run, ignoring existing outputs
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml --only agent-bench
# ^ only agent-bench: no auto smoke and no file default (llama-benchy)
```

Results land in `./benchmarks/<alias>/<bench>/<timestamp>/` with a
summary at `benchmarks/_run-summaries/<ts>/summary.{json,txt}`.

## Related scripts

Alternative to the interview above when the endpoint is already running,
`scripts/generate-benchmarks-toml.sh` generates the TOMLs straight from it:

- `ENDPOINT_BACKEND=litellm` (default): queries `/model/info` and writes one
  `benchmarks.litellm.<producer>.<backend>.toml` per (producer, backend) tag
  pair found on the models (`.variants` suffix to include all models).
- `ENDPOINT_BACKEND=direct`: queries `/models` of a plain
  llama-swap/llama-server host; requires `ENDPOINT_PRODUCER` and
  `ENDPOINT_BACKEND_LABEL`.
- The benchmark list is taken from the `BENCHMARKS` env var, comma-separated;
  active default is `llama-benchy` only — `smoke` is implicit in the runner,
  not a choice.  Example:

  ```bash
  BENCHMARKS="llama-benchy,aider" \
  ENDPOINT_URL=http://localhost:1234/v1 \
  ENDPOINT_NAME=local \
  ENDPOINT_BACKEND=direct \
  ENDPOINT_PRODUCER=rtx5090 ENDPOINT_BACKEND_LABEL=cuda \
  ./scripts/generate-benchmarks-toml.sh
  ```

Post-result helpers in `scripts/` (mention if the user asks to analyse runs):

- `extract-aider-stats.py <benchmark-dir>` — aider cumulative
  pass-rate per test count into YAML + `datasets.json`
- `find-fastest.py` — rank models by throughput from llama-benchy.md tables
- `generate-agent-bench-manifest.py` / `generate-aider-pertest-manifest.py`
  — per-test case × model matrices for the dashboards
- `llama-benchy-md-to-csv.py` — llama-benchy.md → CSV