"""Stdlib tests for shared native validation and history; no llmprobe install required."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate-llmprobe-manifest.py"
SPEC = importlib.util.spec_from_file_location("llmprobe_manifest", SCRIPT)
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)
from lib import llmprobe_report  # manifest makes repo imports work outside the repo cwd


def native_report():
    """Native v2 field names and types from upstream json.ts and outcome.ts."""
    machine = {"platform": "linux", "arch": "x64", "cpu": "test cpu", "memGB": 64}
    return {
        "version": 2,
        "run": {
            "depth": "default", "mode": "probe", "startedAt": "2026-08-11T22:40:09.006Z",
            "label": "a test run", "labelAuto": False,
            "phases": {name: {"status": "measured"} for name in manifest.PHASES},
            "budget": {"limitTokens": 900000, "exhausted": False},
        },
        "target": {"baseUrl": "http://localhost:8080/v1", "model": "native-model",
                   "engine": "llama.cpp", "engineSettings": {"kvCacheType": "q8_0"}},
        "machine": machine,
        "coverage": {
            "byTier": [{"tier": "core", "supported": 2, "total": 3, "pct": 67,
                        "missing": ["audio"], "unprobed": []}],
            "credits": [{"id": "ollama-chat", "label": "Ollama native /api/chat"}],
            "entries": [{"id": "chat", "label": "chat/completions", "kind": "surface",
                         "tier": "core", "supported": True, "probed": True}],
        },
        "conformance": {
            "pct": 90, "passed": 9, "total": 10,
            "bySurface": [{"surface": "chat", "passed": 9, "total": 10, "pct": 90}],
            "results": [{"id": "chat-fail", "name": "bad serialization", "surface": "chat",
                         "outcome": "fail", "durationMs": 350,
                         "failures": [{"id": "must-1", "severity": "MUST", "message": "broken"}]}],
            "inconclusive": [], "warnings": [], "nits": [],
        },
        "capability": {
            "pct": 78, "verdict": "capable",
            "categories": [{"category": "tool-selection", "passed": 6, "total": 6, "pct": 100}],
            "weakCategories": [], "unmeasured": ["tool-restraint"],
            "evals": [{"id": "eval-tool-select", "name": "picks weather", "category": "tool-selection",
                       "passed": 3, "total": 3, "failures": []}],
        },
        "agentic": {
            "pct": 50, "passed": 1, "total": 2,
            "tasks": [{"id": "edit-config", "name": "edit config", "passed": False,
                       "steps": 4, "failure": "rule-violation", "detail": "edited protected file",
                       "calls": {"total": 4, "valid": 4},
                       "violations": [{"rule": "protected-file", "severity": "must", "step": None,
                                       "detail": "protected file changed"}]}],
        },
        "fidelity": {
            "pct": 95, "items": 24,
            "slices": [{"id": "correctness", "label": "Correctness", "score": 0.95,
                        "weight": 0.4, "detail": "23/24 items", "measured": True},
                       {"id": "confidence", "label": "Confidence", "score": 0, "weight": 0.25,
                        "detail": "no logprobs", "measured": False,
                        "unmeasuredReason": "logprobs unavailable"}],
            "firstDivergence": {"itemId": "item-1", "charIndex": 84, "run": 2, "runs": 3},
            "unmeasured": ["Confidence"], "reasoningCaveat": True,
            "measurements": {"meanTopProb": None, "meanMargin": None,
                             "items": [{"id": "item-1", "hard": True, "correct": True,
                                        "topProb": None, "margin": None}]},
            "fingerprint": {"topK": 1, "prompts": [{"id": "item-1", "tokens": [
                {"t": "blue", "lp": -0.1, "top": [["blue", -0.1]]}]}]},
        },
        "bench": {
            "decodeTokPerSec": {"median": 40, "min": 39, "max": 41, "samples": [39, 41]},
            "ttftMs": None, "prefillTokPerSec": None, "prefillPromptTokens": None,
            "streamCaveat": None, "speculative": None, "prefixCache": None,
            "batching": None, "loadDrift": None, "contextScaling": None, "machine": machine,
        },
        "usage": {"inputTokens": 90000, "outputTokens": 10000},
        "durationMs": 123456,
        "futureExtension": {"kept": ["verbatim", None, False]},
    }


def driver_meta(**overrides):
    return {
        "timestamp": "2026-08-12T03:04:05.123456Z", "bench": "llmprobe", "host": "test-host",
        "endpoint": "http://localhost:8080/v1", "model": "driver-model", "run_name": "test name",
        "llmprobe_version": "0.6.17", "status": "complete", "returncode": 0,
        "depth": "default", "performance": True, "extra": {"preserved": True}, **overrides,
    }


class ReportValidationTests(unittest.TestCase):
    def test_manifest_uses_shared_validator_and_public_aliases(self):
        for name in ("PHASES", "PHASE_STATUSES", "DEPTHS", "REPORT", "read_json", "validate_report"):
            self.assertIs(getattr(manifest, name), getattr(llmprobe_report, name), name)

    def test_validation_accepts_historical_unknown_scope_without_changing_report(self):
        for version in (1, 2):
            for scope in (None, {}, {"depth": "quick", "phases": {"coverage": {}}}):
                with self.subTest(version=version, scope=scope):
                    report = native_report()
                    report["version"] = version
                    if scope is None:
                        report.pop("run")
                    else:
                        report["run"] = scope
                    before = json.dumps(report)
                    llmprobe_report.validate_report(report)
                    self.assertEqual(json.dumps(report), before)

    def test_validation_checks_required_and_optional_native_fields(self):
        invalid = (
            (("version",), True),
            (("coverage", "byTier", 0, "total"), -1),
            (("conformance", "pct"), 101),
            (("durationMs",), float("nan")),
            (("durationMs",), float("inf")),
            (("run", "depth"), "fast"),
            (("run", "phases", "coverage", "status"), "done"),
            (("run", "budget", "exhausted"), 0),
            (("usage", "inputTokens"), True),
            (("machine", "cpu"), 3),
            (("agentic", "tasks", 0, "violations", 0, "step"), -1),
            (("fidelity", "fingerprint", "prompts", 0, "tokens", 0, "top"), [["blue"]]),
            (("bench", "decodeTokPerSec", "samples"), [False]),
        )
        for path, value in invalid:
            with self.subTest(path=path, value=value):
                report = native_report()
                field = report
                for part in path[:-1]:
                    field = field[part]
                field[path[-1]] = value
                with self.assertRaisesRegex(ValueError, "report\\."):
                    llmprobe_report.validate_report(report)

    def test_shared_read_json_rejects_nonfinite_numbers_in_extension_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            for literal in ("NaN", "Infinity", "-Infinity", "1e9999", "-1e9999"):
                with self.subTest(literal=literal):
                    path.write_text('{"extension": {"number": ' + literal + '}}', encoding="utf-8")
                    with self.assertRaises(ValueError):
                        llmprobe_report.read_json(path)
            report = native_report()
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(llmprobe_report.read_json(path), report)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "benchmarks"
        self.root.mkdir()

    def write_run(self, name="20260812-030405", folder="model-slug", report=None, meta=None):
        path = self.root / folder / "llmprobe" / name
        path.mkdir(parents=True)
        (path / "result.json").write_text(json.dumps(native_report() if report is None else report),
                                          encoding="utf-8")
        if meta is not None:
            (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return path

    def build(self):
        return manifest.build_manifest(self.root)

    def test_empty_manifest(self):
        result = self.build()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["warnings"], [])
        self.assertIsNotNone(manifest.timestamp(result["generated_at"]))

    def test_fractional_native_budget_is_valid_and_preserved(self):
        report = native_report()
        report["run"]["budget"]["limitTokens"] = 100000.5
        self.write_run(report=report, meta=driver_meta())
        result = self.build()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["runs"][0]["report"]["run"]["budget"]["limitTokens"], 100000.5)

    def test_native_v2_agentic_fidelity_and_metadata_are_not_normalized(self):
        report, meta = native_report(), driver_meta()
        path = self.write_run(report=report, meta=meta)
        (path / "llmprobe.log").write_text("llmprobe output", encoding="utf-8")
        result = self.build()
        self.assertEqual(result["count"], 1)
        run = result["runs"][0]
        self.assertEqual(run["report"], report)
        self.assertEqual(run["meta"], meta)
        self.assertEqual(run["folder"], "model-slug")
        self.assertEqual(run["run"], "20260812-030405")
        self.assertEqual(run["model"], "driver-model")
        self.assertEqual(run["host"], "test-host")
        self.assertEqual(run["ts"], meta["timestamp"])
        self.assertEqual(run["path"], "model-slug/llmprobe/20260812-030405")
        self.assertEqual(run["artifacts"], {
            "report": run["path"] + "/result.json", "meta": run["path"] + "/meta.json",
            "log": run["path"] + "/llmprobe.log",
        })
        self.assertTrue(run["complete"])
        self.assertTrue(run["rankable"])
        self.assertEqual(run["completion_reasons"], [])
        self.assertEqual(run["returncode"], 0)
        self.assertTrue(run["performance"])

    def test_default_partial_coverage_is_completed_and_rankable(self):
        # runScope marks coverage partial whenever any item is unprobed, including
        # the normal default-depth report. This is scope, not an execution error.
        for depth in ("default", "full"):
            for status, returncode in (("complete", 0), ("conformance-failed", 1)):
                with self.subTest(depth=depth, status=status):
                    report = native_report()
                    report["run"]["depth"] = depth
                    report["run"]["phases"]["coverage"] = {
                        "status": "partial", "reason": "1 items were not probed",
                    }
                    report["coverage"]["byTier"][0]["unprobed"] = ["reasoning"]
                    self.write_run(name=f"{depth}-{status}", report=report,
                                   meta=driver_meta(status=status, returncode=returncode))
        result = self.build()
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["skipped"], [])
        for run in result["runs"]:
            self.assertTrue(run["complete"])
            self.assertTrue(run["rankable"])
            self.assertEqual(run["status"], run["meta"]["status"])
            self.assertEqual(run["completion_reasons"], [])
            self.assertEqual(run["report"]["run"]["phases"]["coverage"], {
                "status": "partial", "reason": "1 items were not probed",
            })
            self.assertEqual(run["report"]["coverage"]["byTier"][0]["unprobed"], ["reasoning"])

    def test_quick_partial_conformance_is_completed_but_not_rankable(self):
        report = native_report()
        report["run"]["depth"] = "quick"
        report["run"]["phases"]["coverage"] = {
            "status": "partial", "reason": "16 items were not probed",
        }
        report["run"]["phases"]["conformance"] = {
            "status": "partial", "reason": "quick depth omits slow conformance checks",
        }
        for name in ("capability", "agentic", "fidelity", "performance"):
            report["run"]["phases"][name] = {"status": "not-run"}
        for name in ("agentic", "fidelity", "bench"):
            report.pop(name)
        self.write_run(report=report, meta=driver_meta(depth="quick", performance=False))
        run = self.build()["runs"][0]
        self.assertTrue(run["complete"])
        self.assertFalse(run["rankable"])
        self.assertEqual(run["completion_reasons"], [])
        self.assertEqual(run["report"], report)

    def test_unexpected_partial_or_failed_phases_still_block_completion(self):
        for depth in manifest.DEPTHS:
            for phase in (*manifest.PHASES, "reasoning"):
                for status in ("partial", "failed", "interrupted"):
                    if status == "partial" and (phase == "coverage" or
                                                (phase == "conformance" and depth == "quick")):
                        continue
                    report = native_report()
                    report["run"]["depth"] = depth
                    report["run"]["phases"]["coverage"]["status"] = "partial"
                    report["run"]["phases"][phase] = {"status": status}
                    self.write_run(name=f"{depth}-{phase}-{status}", report=report,
                                   meta=driver_meta())
        result = self.build()
        self.assertEqual(result["skipped"], [])
        for run in result["runs"]:
            with self.subTest(run=run["run"]):
                self.assertFalse(run["complete"])
                self.assertFalse(run["rankable"])
                self.assertTrue(run["completion_reasons"])

    def test_missing_optional_fields_and_v1_scope_remain_unknown(self):
        for version in (1, 2):
            report = native_report()
            report["version"] = version
            for name in ("run", "machine", "usage", "agentic", "fidelity", "bench"):
                report.pop(name)
            self.write_run(name=f"20260812-030405-{version}", report=report, meta=driver_meta())
        result = self.build()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["skipped"], [])
        for run in result["runs"]:
            self.assertFalse(run["complete"])
            self.assertFalse(run["rankable"])
            self.assertIsNone(run["mode"])
            self.assertEqual(run["depth"], "default")  # sidecar fallback, not native scope
            self.assertNotIn("run", run["report"])
            self.assertNotIn("agentic", run["report"])
            self.assertIn("native run scope missing", run["completion_reasons"])

    def test_optional_cards_can_be_absent_from_a_completed_scoped_report(self):
        report = native_report()
        for name in ("usage", "agentic", "fidelity", "bench"):
            report.pop(name)
        for name in ("agentic", "fidelity", "performance"):
            report["run"]["phases"][name] = {"status": "not-run", "reason": "not requested"}
        self.write_run(report=report, meta=driver_meta(performance=False))
        run = self.build()["runs"][0]
        self.assertEqual(run["report"], report)
        self.assertTrue(run["complete"])
        self.assertTrue(run["rankable"])
        self.assertFalse(run["performance"])

    def test_missing_meta_uses_native_identity_and_time_not_success_assumptions(self):
        self.write_run()
        run = self.build()["runs"][0]
        self.assertEqual(run["meta"], {})
        self.assertEqual(run["host"], "")
        self.assertEqual(run["model"], "native-model")
        self.assertEqual(run["ts"], "2026-08-11T22:40:09.006000Z")
        self.assertEqual(run["status"], "unknown")
        self.assertFalse(run["complete"])
        self.assertFalse(run["rankable"])
        self.assertIsNone(run["returncode"])
        self.assertNotIn("meta", run["artifacts"])
        self.assertNotIn("log", run["artifacts"])

    def test_all_phase_statuses_are_preserved_and_conservative(self):
        for index, status in enumerate(manifest.PHASE_STATUSES):
            report = native_report()
            report["run"]["phases"]["agentic"] = {"status": status, "reason": "native reason"}
            self.write_run(name=f"20260812-030405-{index}", report=report, meta=driver_meta())
        result = self.build()
        self.assertEqual(result["count"], len(manifest.PHASE_STATUSES))
        self.assertFalse(result["skipped"])
        for run in result["runs"]:
            status = run["report"]["run"]["phases"]["agentic"]["status"]
            expected_complete = status in ("measured", "not-run", "unavailable")
            self.assertEqual(run["complete"], expected_complete, status)
            self.assertEqual(run["rankable"], expected_complete, status)

    def test_missing_scope_fields_and_phase_statuses_are_not_synthesized(self):
        variants = [{}, {"depth": "default", "mode": "probe"},
                    {"depth": "default", "mode": "probe", "phases": {"coverage": {"reason": "unknown"}}}]
        for index, scope in enumerate(variants):
            report = native_report()
            report["run"] = scope
            self.write_run(name=str(index), report=report, meta=driver_meta())
        for run in self.build()["runs"]:
            self.assertFalse(run["complete"])
            self.assertFalse(run["rankable"])
            self.assertTrue(any("unknown" in reason for reason in run["completion_reasons"]))

    def test_quick_and_non_probe_runs_are_never_rankable(self):
        for index, (depth, mode) in enumerate((("quick", "probe"), ("full", "bench-only"),
                                               ("full", "eval-only"))):
            report = native_report()
            report["run"].update(depth=depth, mode=mode)
            if depth == "quick":
                report["run"]["phases"]["conformance"]["status"] = "partial"
                for name in ("capability", "agentic", "fidelity"):
                    report["run"]["phases"][name]["status"] = "not-run"
            self.write_run(name=str(index), report=report, meta=driver_meta())
        for run in self.build()["runs"]:
            self.assertFalse(run["rankable"])
            self.assertTrue(run["complete"])

    def test_core_unavailable_or_empty_is_not_rankable(self):
        for index in range(4):
            report = native_report()
            report["run"]["phases"]["coverage"]["status"] = "partial"
            if index == 0:
                report["run"]["phases"]["capability"]["status"] = "unavailable"
            elif index == 1:
                report["capability"]["categories"] = []
            elif index == 2:
                report["coverage"]["byTier"][0]["total"] = 0
            else:
                report["conformance"]["total"] = 0
            self.write_run(name=str(index), report=report, meta=driver_meta())
        for run in self.build()["runs"]:
            self.assertTrue(run["complete"])
            self.assertFalse(run["rankable"])

    def test_failures_and_raw_exit_codes_are_kept(self):
        for index, status in enumerate(("failed", "timeout", "interrupted", "incomplete", "budget-exhausted")):
            self.write_run(name=str(index), meta=driver_meta(status=status, returncode=-15 if index == 2 else 2))
        result = self.build()
        self.assertEqual(result["count"], 5)
        self.assertFalse(result["skipped"])
        for run in result["runs"]:
            self.assertEqual(run["status"], run["meta"]["status"])
            self.assertEqual(run["returncode"], run["meta"]["returncode"])
            self.assertFalse(run["complete"])
            self.assertFalse(run["rankable"])
            self.assertEqual(run["report"]["conformance"]["pct"], 90)

    def test_conformance_failure_is_a_completed_measurement_not_a_crash(self):
        self.write_run(meta=driver_meta(status="conformance-failed", returncode=1))
        run = self.build()["runs"][0]
        self.assertTrue(run["complete"])
        self.assertTrue(run["rankable"])
        self.assertEqual(run["status"], "conformance-failed")
        self.assertEqual(run["returncode"], 1)

    def test_native_early_stop_overrides_successful_driver_status(self):
        for index in range(4):
            report = native_report()
            report["run"]["phases"]["coverage"]["status"] = "partial"
            if index == 0:
                report["incomplete"] = "target stopped answering"
            elif index == 1:
                report["run"]["budget"]["exhausted"] = True
            elif index == 2:
                report["run"]["phases"]["fidelity"]["status"] = "failed"
            else:
                report["run"]["phases"]["capability"]["status"] = "interrupted"
            self.write_run(name=str(index), report=report,
                           meta=driver_meta(status="conformance-failed", returncode=1))
        statuses = set()
        for run in self.build()["runs"]:
            self.assertFalse(run["complete"])
            self.assertFalse(run["rankable"])
            self.assertEqual(run["meta"]["status"], "conformance-failed")
            statuses.add(run["status"])
        self.assertEqual(statuses, {"incomplete", "budget-exhausted", "failed", "interrupted"})

    def test_optional_reasoning_phase_and_aborted_report_are_not_assumed_complete(self):
        for index in range(2):
            report = native_report()
            report["run"]["phases"]["reasoning"] = {"status": "measured"}
            report["reasoning"] = {"passed": 1, "total": 2, "cases": [], "aborted": None}
            if index == 0:
                report["run"]["phases"]["reasoning"]["status"] = "partial"
            else:
                report["reasoning"]["aborted"] = {"reason": "budget", "message": "token budget exhausted"}
            self.write_run(name=str(index), report=report, meta=driver_meta())
        result = self.build()
        self.assertEqual(result["count"], 2)
        for run in result["runs"]:
            self.assertFalse(run["complete"])
            self.assertFalse(run["rankable"])
            self.assertIn("reasoning", run["report"])

    def test_inconsistent_returncode_does_not_confirm_completion(self):
        for index, rc in enumerate((1, 2, -9, "0", False)):
            self.write_run(name=str(index), meta=driver_meta(returncode=rc))
        for run in self.build()["runs"]:
            self.assertFalse(run["complete"])
            self.assertFalse(run["rankable"])

    def test_every_history_is_included_no_latest_or_symlink_duplicate(self):
        old = self.write_run(name="20260801-010203", meta=driver_meta(timestamp="2026-08-01T01:02:03Z"))
        new = self.write_run(name="20260801-010203-123456", meta=driver_meta(timestamp="2026-08-01T01:02:03.1Z"))
        self.write_run(name="non-timestamp-history", meta=driver_meta(timestamp="2026-08-02T00:00:00Z"))
        (old.parent / "latest").symlink_to(new, target_is_directory=True)
        (old.parent / "alias").symlink_to(old, target_is_directory=True)
        # Even a non-symlink latest is not another historical run.
        self.write_run(name="latest", folder="other")
        result = self.build()
        self.assertEqual(result["count"], 3)
        self.assertEqual([run["run"] for run in result["runs"]],
                         ["non-timestamp-history", "20260801-010203-123456", "20260801-010203"])
        self.assertEqual(len(result["skipped"]), 3)
        self.assertTrue(any("latest alias ignored" in item for item in result["skipped"]))
        self.assertTrue(any("alias: symlink ignored" in item for item in result["skipped"]))

    def test_timestamp_fallbacks_and_timezone_sorting(self):
        report = native_report()
        report.pop("run")
        self.write_run(name="20260802-030405-unique", report=report,
                       meta=driver_meta(timestamp="not-an-ISO-timestamp"))
        self.write_run(name="unknown", report=report, meta=driver_meta(timestamp=None))
        self.write_run(name="offset", report=report, meta=driver_meta(timestamp="2026-08-02T05:04:06+02:00"))
        result = self.build()
        self.assertEqual([run["run"] for run in result["runs"]], ["offset", "20260802-030405-unique", "unknown"])
        self.assertEqual(result["runs"][0]["ts"], "2026-08-02T03:04:06Z")
        self.assertEqual(result["runs"][1]["ts"], "2026-08-02T03:04:05Z")
        self.assertIsNone(result["runs"][2]["ts"])

    def test_corrupt_unsupported_and_wrong_native_shapes_are_skipped(self):
        bad_reports = [[], {}, {**native_report(), "version": 3},
                       {**native_report(), "version": True},
                       {**native_report(), "agentic": {"score": 100}},
                       {**native_report(), "fidelity": {"passed": 24, "total": 24}},
                       {**native_report(), "usage": {"inputTokens": "5", "outputTokens": 6}}]
        bad_percent = native_report()
        bad_percent["conformance"]["pct"] = "90"
        bad_reports.append(bad_percent)
        for index, report in enumerate(bad_reports):
            self.write_run(name=f"bad-{index}", report=report)
        path = self.write_run(name="truncated")
        (path / "result.json").write_text('{"version": 2,', encoding="utf-8")
        path = self.write_run(name="nan")
        (path / "result.json").write_text(json.dumps(native_report()).replace('123456', 'NaN'), encoding="utf-8")
        (self.root / "model-slug" / "llmprobe" / "no-report").mkdir()
        self.write_run(name="good", meta=driver_meta())
        result = self.build()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["runs"][0]["run"], "good")
        self.assertEqual(len(result["skipped"]), len(bad_reports) + 3)
        self.assertTrue(all("model-slug/llmprobe/" in item for item in result["skipped"]))

    def test_nonfinite_extension_numbers_are_skipped_without_poisoning_output(self):
        path = self.write_run(name="overflow")
        (path / "result.json").write_text(
            json.dumps(native_report()).replace('"kept":', '"overflow": 1e9999, "kept":'),
            encoding="utf-8")
        self.write_run(name="good", meta=driver_meta())
        result = self.build()
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["skipped"]), 1)
        manifest.write_manifest(self.root / "manifest.json", result)

    def test_bad_optional_metadata_warns_without_dropping_reports(self):
        for index, contents in enumerate(("{broken", "[]", "null")):
            path = self.write_run(name=str(index))
            (path / "meta.json").write_text(contents, encoding="utf-8")
        result = self.build()
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["warnings"]), 3)
        self.assertFalse(result["skipped"])
        for run in result["runs"]:
            self.assertEqual(run["meta"], {})
            self.assertIn("meta", run["artifacts"])
            self.assertFalse(run["complete"])

    def test_symlinks_at_every_level_are_excluded_and_paths_stay_inside_root(self):
        real = self.write_run(meta=driver_meta())
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "result.json").write_text(json.dumps(native_report()), encoding="utf-8")
        (outside / "meta.json").write_text(json.dumps(driver_meta()), encoding="utf-8")
        (outside / "run.log").write_text("outside data", encoding="utf-8")
        (self.root / "folder-alias").symlink_to(real.parent.parent, target_is_directory=True)
        (self.root / "external-folder").symlink_to(outside, target_is_directory=True)
        (self.root / "bench-alias").mkdir()
        (self.root / "bench-alias" / "llmprobe").symlink_to(real.parent, target_is_directory=True)
        (real.parent / "run-alias").symlink_to(real, target_is_directory=True)
        (real.parent / "external-run").symlink_to(outside, target_is_directory=True)
        (real.parent / "result-alias").mkdir()
        (real.parent / "result-alias" / "result.json").symlink_to(outside / "result.json")
        (real.parent / "dangling").symlink_to(outside / "missing", target_is_directory=True)
        (real / "meta.json").unlink()
        (real / "meta.json").symlink_to(outside / "meta.json")
        (real / "run.log").symlink_to(outside / "run.log")
        result = self.build()
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["skipped"]), 7)
        self.assertEqual(len(result["warnings"]), 1)
        run = result["runs"][0]
        self.assertEqual(run["meta"], {})
        self.assertEqual(run["artifacts"], {"report": run["path"] + "/result.json"})
        for relative in (run["path"], *run["artifacts"].values()):
            self.assertFalse(Path(relative).is_absolute())
            self.assertNotIn("..", Path(relative).parts)
            path = self.root / relative
            self.assertTrue(path.resolve().is_relative_to(self.root))
            self.assertFalse(path.is_symlink())

    def test_browser_path_separators_and_url_schemes_are_not_artifact_components(self):
        self.write_run(name="good", meta=driver_meta())
        self.write_run(name="back\\\\slash")
        self.write_run(name="url:scheme")
        result = self.build()
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertTrue(all("unsafe path component" in item for item in result["skipped"]))

    def test_only_existing_regular_local_logs_are_linked(self):
        path = self.write_run(meta=driver_meta(log="../../secret.log", log_path="https://example.invalid"))
        self.assertNotIn("log", self.build()["runs"][0]["artifacts"])
        (path / "output.log").write_text("log", encoding="utf-8")
        run = self.build()["runs"][0]
        self.assertEqual(run["artifacts"]["log"], run["path"] + "/output.log")

    def test_custom_root_cli_derives_default_output_and_does_not_change_inputs(self):
        run_dir = self.write_run(meta=driver_meta())
        before = {path: path.read_bytes() for path in run_dir.iterdir()}
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)],
                                cwd=self.base, capture_output=True, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / "_llmprobe-scoreboards" / "manifest.json"
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["count"], 1)
        self.assertIn("1 runs", result.stdout)
        self.assertEqual(before, {path: path.read_bytes() for path in run_dir.iterdir()})
        self.assertFalse(list(output.parent.glob("*.tmp")))

    def test_explicit_cli_output_and_invalid_root(self):
        output = self.base / "custom" / "manifest.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "-o", str(output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["runs"], [])
        self.assertFalse((self.root / "_llmprobe-scoreboards").exists())
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.base / "missing")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a directory", result.stderr)

    def test_atomic_write_replaces_only_after_complete_json_is_written(self):
        output = self.root / "manifest.json"
        output.write_text("old manifest", encoding="utf-8")
        expected = self.build()
        replace = os.replace

        def checked_replace(source, destination):
            self.assertEqual(Path(source).parent, output.parent)
            self.assertEqual(output.read_text(encoding="utf-8"), "old manifest")
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), expected)
            replace(source, destination)

        with mock.patch.object(manifest.os, "replace", side_effect=checked_replace) as patched:
            manifest.write_manifest(output, expected)
        patched.assert_called_once()
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)
        self.assertEqual(list(output.parent.glob(".manifest.json.*.tmp")), [])

    def test_atomic_write_failure_keeps_previous_output_and_cleans_temporary_file(self):
        output = self.root / "manifest.json"
        output.write_text("old manifest", encoding="utf-8")
        with mock.patch.object(manifest.os, "replace", side_effect=OSError("cannot replace")):
            with self.assertRaises(OSError):
                manifest.write_manifest(output, self.build())
        self.assertEqual(output.read_text(encoding="utf-8"), "old manifest")
        self.assertEqual(list(output.parent.glob(".manifest.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
