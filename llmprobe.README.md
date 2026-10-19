# llmprobe integration

llmprobe fits this harness as an optional API compatibility and model capability benchmark. It accepts the existing endpoint URL, model ID, and API key. Its native JSON report provides separate coverage, conformance, capability, agentic, and fidelity results. These results do not form one combined model score.

The integration pins npm package `llmprobe@0.6.17`. The reviewed upstream revision is `0b18df612f644e9619792c3ab3d0cabfc0a3d710`. The package requires Node.js 22.19.0 or newer. The first installation needs npm and access to the npm registry. The driver caches the package under `work/llmprobe/0.6.17/`. The Nix wrapper and development shell supply Node.js 24 and npm. Python 3.11 or newer runs the harness and driver. Standard probes and simulated agentic tasks do not require Docker.

## Select and run

Add `llmprobe` to an endpoint or model's `benchmarks` list in your existing TOML configuration. It stays disabled when absent. The configuration generators also list it as an available benchmark. Existing default benchmark selections do not change.

```toml
output_dir = "./benchmarks"

[[endpoints]]
name = "local"
url = "http://localhost:8080/v1"
api_key = "EMPTY"
benchmarks = ["llmprobe"]

[[endpoints.models]]
name = "exact-api-model-id"
alias = "my-model-Q4_K_M"
```

```bash
python3 run_benchmarks.py --config your-config.toml --only llmprobe --dry-run
python3 run_benchmarks.py --config your-config.toml --only llmprobe
# Or use the packaged harness:
nix run .#run-benchmarks -- --config your-config.toml --only llmprobe
```

The usual smoke gate applies when you do not use `--only`. That gate requires `/chat/completions`. To test a Responses-only or Messages-only endpoint, select `--only llmprobe` and specify a model in the TOML configuration. This also avoids model discovery by the harness.

The default driver runs the standard upstream probes without its performance sweep. This avoids duplicating llama-benchy measurements. It does not run the optional accuracy suites or execute generated coding solutions. Upstream warns that its optional coding suite's `node:vm` is not a security sandbox.

For a short compatibility run, invoke the driver with `--quick`. For performance measurements, add `--bench`. These flags apply to direct driver runs, not TOML settings.

```bash
python3 llmprobe_bench.py --endpoint http://localhost:8080/v1 \
  --model exact-api-model-id --run-name my-model-Q4_K_M --quick

nix run .#llmprobe-bench -- --endpoint http://localhost:8080/v1 \
  --model exact-api-model-id --bench --full --new
```

The driver also accepts `--budget`, `--timeout`, `--run-timeout`, `--reasoning`, and `--concurrency`. Use `--help` for defaults. Use `--llmprobe-bin /path/to/llmprobe` to bypass the npm installation. An external executable must emit the native version 2 report. Its version is recorded as unknown.

## Results and failures

Each invocation writes a unique directory:

```text
benchmarks/<model-or-alias>/llmprobe/<timestamp>/
  result.json   # Unchanged native llmprobe report, when upstream writes one
  meta.json     # Invocation scope, status, raw upstream exit code, driver exit code
  run.log       # stdout and stderr, including installation errors
  run.cmd       # Replay command, without the API key
```

The driver supplies the key through `LLMPROBE_API_KEY`. For an authenticated replay, set that variable before running `bash run.cmd`. Upstream does not read `OPENAI_API_KEY`. The existing matrix runner prints and stores its own job arguments, including API keys. Treat its console output and `_run-summaries` as sensitive.

A completed report becomes the relative link `<model-or-alias>/llmprobe.json`. A matching completed result skips subsequent runs. The match includes endpoint, model, release, depth, performance selection, and driver controls. Use `--new` to rerun. Incomplete runs and exhausted budgets do not update the link or suppress retries. A failed rerun leaves an earlier complete link intact.

Upstream exit 1 indicates an engine conformance failure or an exhausted token budget. A low capability score alone does not produce exit 1. If the report is valid and the run completed, the driver returns 0 even when compatibility checks failed. The harness reports OK for the execution, while the dashboard keeps the `conformance-failed` status and failed assertions. In `meta.json`, `returncode` preserves the upstream code and `driver_returncode` records the harness-facing code. Cached reports from older drivers also return success without rewriting their historical metadata.

Budget exhaustion, incomplete or invalid reports, and execution errors still produce a nonzero driver code. Upstream exit 2 indicates an incomplete run or another fatal error. A whole-process deadline produces driver exit 124. Interruption produces driver exit 130. Historical failure reports remain available to the dashboard. A startup failure without a report remains in the log and metadata, not the score table. Existing run summaries record the old behavior and do not change automatically. Rerun the same configuration without `--new` to produce a new summary from matching cached reports.

The native report uses `version: 2`. It records the target, run depth, phase statuses and reasons, coverage entries, conformance assertions, capability evaluations, optional cards, token usage, and elapsed time. Performance statistics use objects with `median`, `min`, `max`, and `samples`. Missing sections and `null` measurements are not zero scores. The dashboard keeps the native report and displays phase scope beside its scores.

## Update the dashboard

Run the usual update command, then open the llmprobe tab in `benchmarks/index.html`. Serve the directory over HTTP rather than opening the file directly.

```bash
./benchmarks.update.sh
# Or generate only the llmprobe manifest:
python3 scripts/generate-llmprobe-manifest.py --root benchmarks
python3 -m http.server --directory benchmarks 8000
```

The generator writes `_llmprobe-scoreboards/manifest.json` under the supplied root. It indexes historical `result.json` files with their metadata. It does not count the latest link twice. Corrupt and unsupported reports produce diagnostics instead of breaking other results. The tab supports the global GPU, quantization, text, and highlight controls. It shows separate scores, coverage, optional performance values, run scope, phase reasons, failures, and artifact links.

Quick, interrupted, and unknown-scope runs remain visible without ranking as full comparable probes. Upstream intentionally marks unprobed coverage as partial, even at standard depth. The dashboard keeps that warning without discarding independently measured cards. Different run depths, inference hardware, reasoning settings, and available API features also affect comparisons. Machine metadata describes the client host, not necessarily a remote inference server.

## Runtime and upstream sources

No fixed runtime or dollar estimate applies. Probes send real requests to the selected endpoint. The default per-request timeout is 60 seconds. Some upstream performance and accuracy requests do not honor that deadline. The driver therefore imposes a separate 3,600-second whole-process deadline. Use `--run-timeout` to change it.

Do not use `--budget` as a guaranteed billing limit. Upstream checks accumulated reported token usage before requests. Missing usage is not estimated, and a request can exceed the remaining balance. Performance sweeps and expanded checks can use large contexts. The harness never uploads reports or opens an upstream browser page. The driver uses `--no-save` to disable upstream's automatic home-directory library, while `--save` writes the explicit report artifact.

The requirements and report contract come from these first-party sources at the reviewed revision:

- [Package version, Node requirement, executable, and dependencies](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/package.json).
- [CLI flags, explicit report writes, API key selection, and exit behavior](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/bin/llmprobe.ts).
- [JSON report contract and phase statuses](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/src/core/report/json.ts).
- [Optional cards and performance statistic types](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/src/core/outcome.ts).
- [Token accounting and budget enforcement](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/src/core/client.ts).
- [Benchmark workloads and request deadlines](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/src/bench/index.ts).
- [Upstream usage, comparison limits, and coding-suite warning](https://github.com/ddalcu/llmprobe/blob/0b18df612f644e9619792c3ab3d0cabfc0a3d710/README.md).

Run the offline tests with `python3 -m unittest discover -s tests`. These tests use fixtures and a fake executable. They do not spend endpoint tokens.
