---
name: create-bench-toml
description: Create a new benchmark TOML config for run_benchmarks.py by interviewing the user (endpoint URL/port, endpoint name, model, alias), listing available models via the service's /v1/models endpoint, and writing a ready-to-run benchmarks.*.toml. Use when the user asks to "add a new benchmark config", "create a toml for <service>", "benchmark the service on port X", or similar.
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

Show the available benchmark ids and let the user choose (default:
`llama-benchy` + `agent-bench`):

| id             | what it runs                              |
|----------------|-------------------------------------------|
| `smoke`        | tiny connectivity check (skip existing ok marker) |
| `llama-benchy` | llama-benchy-benchmarks.sh                |
| `agent-bench`  | agent_bench.py (tool-calling structured output) |
| `aider`        | aider-polyglot-benchmarks.sh (needs a coder-ish model) |
| `terminal-bench` | terminal-bench-benchmarks.sh (reasoning) |

If `aider` is selected, ask whether the model is good at `diff` editing
and set `edit_format = "diff"` on the model entry (default is `"whole"`).

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
benchmarks = ["<bench1>", "<bench2>"]

  [[endpoints.models]]
  name = "<exact model id from /v1/models>"
  alias = "<friendly alias>"
  # benchmarks = ["aider"]        # optional per-model override
  # edit_format = "diff"          # optional, aider only
```

Omit the `[[endpoints.models]]` block entirely if the user wants every
model the service reports.

## Step 6 — Verify and hand over the run command

```bash
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml --dry-run
```

Confirm the planned jobs look right (correct endpoint, model, alias,
benchmarks). Then give the user the commands to run for real:

```bash
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml            # run once
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml --new      # re-run, ignoring existing outputs
./run_benchmarks.py --config benchmarks.<endpoint-name>.toml --only smoke,agent-bench
```

Results land in `./benchmarks/<alias>/<bench>/<timestamp>/` with a
summary at `benchmarks/_run-summaries/<ts>/summary.{json,txt}`.