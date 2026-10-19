"""Driver and harness tests using a fake executable, without network or npm."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import llmprobe_bench as driver
import run_benchmarks as harness
from test_llmprobe_manifest import native_report, manifest


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "results"
        self.fake = self.base / "fake-llmprobe"
        self.fixture = self.base / "fixture.json"
        self.report = native_report()
        self.report["target"]["model"] = "api/model:Q4"
        self.report["run"]["phases"]["performance"] = {"status": "not-run", "reason": "disabled"}
        self.fixture.write_text(json.dumps(self.report))
        self.calls = self.base / "calls.jsonl"
        self.fake.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as stream:
    stream.write(json.dumps({{"args": args, "key": os.environ.get("LLMPROBE_API_KEY")}}) + "\\n")
mode = os.environ.get("FAKE_MODE", "success")
if mode == "timeout": time.sleep(30)
if mode != "no-report":
    report = json.loads(pathlib.Path(os.environ["FAKE_REPORT"]).read_text())
    if mode == "budget": report["run"]["budget"]["exhausted"] = True
    if mode == "incomplete": report["incomplete"] = "server disconnected"
    if mode == "partial": report["run"]["phases"]["coverage"]["status"] = "partial"
    if mode == "unexpected-partial": report["run"]["phases"]["agentic"]["status"] = "partial"
    if "--quick" in args:
        report["run"]["depth"] = "quick"
        report["run"]["phases"]["conformance"]["status"] = "partial"
        for phase in ("capability", "agentic", "fidelity"):
            report["run"]["phases"][phase]["status"] = "not-run"
    path = pathlib.Path(args[args.index("--save") + 1])
    path.write_text("not JSON" if mode == "malformed" else json.dumps(report))
print("fake llmprobe output")
sys.exit(1 if mode in ("conformance", "budget") else 2 if mode == "incomplete" else 0)
''')
        self.fake.chmod(0o755)
        self.env = mock.patch.dict(os.environ, FAKE_CALLS=str(self.calls), FAKE_REPORT=str(self.fixture))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.argv = ["--endpoint", "http://localhost:8080/v1", "--model", "api/model:Q4",
                     "--api-key", "sensitive-key", "--output-dir", str(self.output),
                     "--run-name", "gfx1151-friend/Q4", "--llmprobe-bin", str(self.fake)]
        self.latest = self.output / "gfx1151-friend_Q4" / "llmprobe.json"

    def execute(self, *extra, mode="success"):
        with mock.patch.dict(os.environ, FAKE_MODE=mode), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return driver.main(self.argv + list(extra))

    def histories(self):
        return sorted(self.latest.parent.glob("llmprobe/*"))

    def test_native_report_alias_artifacts_environment_and_cache(self):
        self.assertEqual(self.execute(), 0)
        self.assertTrue(self.latest.is_symlink())
        self.assertFalse(os.readlink(self.latest).startswith("/"))
        path = self.latest.resolve().parent
        self.assertEqual(json.loads(self.latest.read_text()), self.report)
        meta = json.loads((path / "meta.json").read_text())
        self.assertEqual(meta["status"], "complete")
        self.assertEqual(meta["model"], "api/model:Q4")
        self.assertEqual(meta["run_name"], "gfx1151-friend/Q4")
        for artifact in ("meta.json", "run.cmd", "run.log"):
            self.assertNotIn("sensitive-key", (path / artifact).read_text())
        self.assertIn("fake llmprobe output", (path / "run.log").read_text())
        call = json.loads(self.calls.read_text())
        self.assertEqual(call["key"], "sensitive-key")
        self.assertEqual(call["args"][0], "http://localhost:8080/v1")
        self.assertIn("--no-bench", call["args"])
        self.assertIn("--no-save", call["args"])
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.histories()), 1)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(self.execute("--new"), 0)
        self.assertEqual(len(self.histories()), 2)
        board = manifest.build_manifest(self.output)
        self.assertEqual(board["count"], 2)
        self.assertEqual(board["skipped"], [])
        self.assertTrue(all(r["rankable"] for r in board["runs"]))

    def test_conformance_failure_is_published_and_cache_preserves_exit_code(self):
        self.assertEqual(self.execute(mode="conformance"), 1)
        self.assertTrue(self.latest.exists())
        self.assertEqual(self.execute(), 1)
        self.assertEqual(len(self.histories()), 1)
        self.assertEqual(json.loads((self.histories()[0] / "meta.json").read_text())["status"], "conformance-failed")
        self.assertTrue(manifest.build_manifest(self.output)["runs"][0]["rankable"])

    def test_failures_do_not_publish_or_cache_but_keep_reports_and_metadata(self):
        for mode, rc, status in (("budget", 1, "budget-exhausted"), ("incomplete", 2, "incomplete"),
                                 ("unexpected-partial", 1, "incomplete"), ("malformed", 1, "failed"),
                                 ("no-report", 1, "failed")):
            with self.subTest(mode=mode):
                self.assertEqual(self.execute(mode=mode), rc)
                self.assertFalse(self.latest.exists())
                meta = json.loads((self.histories()[-1] / "meta.json").read_text())
                self.assertEqual(meta["status"], status)
        self.assertEqual(len(self.histories()), 5)
        board = manifest.build_manifest(self.output)
        self.assertEqual(board["count"], 3)
        self.assertTrue(all(not r["rankable"] for r in board["runs"]))

    def test_intentionally_partial_scope_is_saved_and_cached_without_claiming_full_ranking(self):
        self.assertEqual(self.execute("--quick", mode="partial"), 0)
        self.assertTrue(self.latest.exists())
        self.assertEqual(self.execute("--quick"), 0)
        self.assertEqual(len(self.histories()), 1)
        self.assertEqual(json.loads((self.histories()[0] / "meta.json").read_text())["status"], "complete")
        self.assertFalse(manifest.build_manifest(self.output)["runs"][0]["rankable"])

    def test_default_partial_coverage_keeps_measured_cards_comparable(self):
        self.assertEqual(self.execute(mode="partial"), 0)
        self.assertTrue(self.latest.exists())
        board = manifest.build_manifest(self.output)
        self.assertTrue(board["runs"][0]["complete"])
        self.assertTrue(board["runs"][0]["rankable"])
        self.assertEqual(board["runs"][0]["report"]["run"]["phases"]["coverage"]["status"], "partial")

    def test_failed_rerun_keeps_previous_latest(self):
        self.assertEqual(self.execute(), 0)
        previous = self.latest.resolve()
        self.assertEqual(self.execute("--new", mode="incomplete"), 2)
        self.assertEqual(self.latest.resolve(), previous)

    def test_whole_run_timeout_keeps_failure_metadata(self):
        self.assertEqual(self.execute("--run-timeout", "1", mode="timeout"), 124)
        meta = json.loads((self.histories()[0] / "meta.json").read_text())
        self.assertEqual(meta["status"], "timeout")
        self.assertFalse(self.latest.exists())

    def test_scope_options_are_forwarded_and_change_cache_signature(self):
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.execute("--quick", "--bench", "--budget", "5000", "--reasoning", "off",
                                      "--timeout", "15", "--concurrency", "2"), 0)
        self.assertEqual(len(self.histories()), 2)
        args = json.loads(self.calls.read_text().splitlines()[-1])["args"]
        self.assertIn("--quick", args)
        self.assertNotIn("--no-bench", args)
        self.assertEqual(args[args.index("--budget") + 1], "5000")
        self.assertEqual(args[args.index("--reasoning") + 1], "off")

    def test_structurally_incomplete_reports_are_not_published_or_cached(self):
        original = json.dumps(self.report)
        for mutation in (lambda r: r.update(coverage={}),
                         lambda r: r["run"].update(phases={}),
                         lambda r: r["run"]["phases"]["agentic"].clear(),
                         lambda r: r["run"].pop("depth"),
                         lambda r: r["agentic"].update(tasks=None),
                         lambda r: r.update(durationMs=float("nan"))):
            report = json.loads(original)
            mutation(report)
            self.fixture.write_text(json.dumps(report))
            self.assertEqual(self.execute(), 1)
            self.assertFalse(self.latest.exists())
        self.assertEqual(len(self.histories()), 6)

    def test_corrupt_cached_report_triggers_rerun(self):
        self.assertEqual(self.execute(), 0)
        report = json.loads(self.latest.read_text())
        report["coverage"] = {}
        self.latest.write_text(json.dumps(report))
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.histories()), 2)
        self.assertEqual(json.loads(self.latest.read_text())["coverage"], self.report["coverage"])

    def test_wrong_model_report_is_not_published(self):
        self.report["target"]["model"] = "wrong-model"
        self.fixture.write_text(json.dumps(self.report))
        self.assertEqual(self.execute(), 1)
        self.assertFalse(self.latest.exists())

    def test_pinned_install_uses_npm_without_lifecycle_scripts(self):
        args = driver.parse_args(["--endpoint", "url", "--model", "model", "--work-dir", str(self.base)])
        package = self.base / "llmprobe" / driver.LLMPROBE_VERSION / "node_modules" / "llmprobe"
        def install(*a, **kw):
            self.assertIn(f"llmprobe@{driver.LLMPROBE_VERSION}", a[0])
            self.assertIn("--ignore-scripts", a[0])
            (package / "bin/dist").mkdir(parents=True)
            (package / "bin/dist/llmprobe.mjs").write_text("// bundled CLI")
            (package / "package.json").write_text(json.dumps({"version": driver.LLMPROBE_VERSION}))
        with mock.patch.object(driver.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             mock.patch.object(driver.subprocess, "check_output", return_value="v24.15.0"), \
             mock.patch.object(driver.subprocess, "run", side_effect=install) as npm:
            tool = driver.resolve_tool(args, io.StringIO())
            self.assertEqual(tool[0], "/bin/node")
            driver.resolve_tool(args, io.StringIO())
            npm.assert_called_once()

    def test_old_node_is_rejected(self):
        with mock.patch.object(driver.shutil, "which", return_value="node"), \
             mock.patch.object(driver.subprocess, "check_output", return_value="v22.18.0"):
            with self.assertRaisesRegex(RuntimeError, ">=22.19.0"):
                driver.resolve_tool(driver.parse_args(["--endpoint", "url", "--model", "model"]), io.StringIO())

    def test_harness_selection_resolution_and_subprocess_dispatch(self):
        config = {"output_dir": str(self.output), "endpoints": [{"name": "test", "url": "http://localhost:1/v1",
                  "benchmarks": ["llmprobe"], "models": [{"name": "api/model:Q4", "alias": "gfx1151-friend/Q4"}]}]}
        plan = harness.build_plan(config, {"llmprobe"}, None, None)
        self.assertEqual([j.bench for j in plan.jobs], ["llmprobe"])
        all_jobs = harness.build_plan(config, None, None, None).jobs
        self.assertEqual([j.bench for j in all_jobs], ["smoke", "llmprobe"])
        with mock.patch.object(harness.shutil, "which", return_value=None):
            self.assertEqual(harness.resolve_driver("llmprobe", ROOT), [sys.executable, str(ROOT / "llmprobe_bench.py")])
        # Use the public driver contract through the real harness execution path.
        with mock.patch.object(harness, "resolve_driver", return_value=[sys.executable, str(ROOT / "llmprobe_bench.py"),
                                                                       "--llmprobe-bin", str(self.fake)]):
            harness.run_job(plan.jobs[0], self.output, ROOT, dry_run=False)
        self.assertEqual(plan.jobs[0].returncode, 0)
        self.assertTrue(self.latest.exists())

    def test_harness_cli_dry_run_and_smoke_failure_gate(self):
        config = self.base / "benchmarks.toml"
        config.write_text(f'''output_dir = {json.dumps(str(self.output))}
[[endpoints]]
name = "test"
url = "http://127.0.0.1:1/v1"
benchmarks = ["llmprobe"]
[[endpoints.models]]
name = "api/model:Q4"
''')
        command = [sys.executable, str(ROOT / "run_benchmarks.py"), "--config", str(config)]
        dry = subprocess.run(command + ["--dry-run", "--only", "llmprobe"], text=True, capture_output=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("llmprobe", dry.stdout)
        real = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(real.returncode, 1)
        summary = json.loads(next(self.output.glob("_run-summaries/*/summary.json")).read_text())
        self.assertEqual(summary["jobs"][1]["error"], "skipped: smoke failed")
        self.assertFalse(list(self.output.glob("*/llmprobe/*")))


if __name__ == "__main__":
    unittest.main()
