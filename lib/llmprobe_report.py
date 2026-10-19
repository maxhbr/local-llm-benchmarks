"""Shared validation for native llmprobe JSON reports (upstream json.ts/outcome.ts).

``read_json`` rejects non-JSON constants and overflowing numbers, even in unknown
extension fields. ``validate_report`` checks known required and optional fields
without changing the report or discarding extensions. Historical v1/v2 reports
may omit run scope or scope facts: validation never invents measured phases.
Callers that need a current, fully scoped report must separately require version
2, depth/mode, and all six core phase statuses. Completion/ranking policy belongs
to the caller, not this schema: native partial coverage and quick-depth partial
conformance can describe a successfully completed scoped execution.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable


Validator = Callable[[Any, str], None]
PHASES = ("coverage", "conformance", "capability", "agentic", "fidelity", "performance")
PHASE_STATUSES = ("measured", "partial", "not-run", "unavailable", "interrupted", "failed")
DEPTHS = ("quick", "default", "full")


def require(condition: bool, path: str, expected: str) -> None:
    if not condition:
        raise ValueError(f"{path}: expected {expected}")


def string(value: Any, path: str) -> None:
    require(isinstance(value, str), path, "string")


def boolean(value: Any, path: str) -> None:
    require(isinstance(value, bool), path, "boolean")


def number(value: Any, path: str) -> None:
    require(type(value) is int or (type(value) is float and math.isfinite(value)),
            path, "finite number")


def count(value: Any, path: str) -> None:
    require(type(value) is int and value >= 0, path, "nonnegative integer")


def percent(value: Any, path: str) -> None:
    number(value, path)
    require(0 <= value <= 100, path, "percentage in [0, 100]")


def choice(*values: Any) -> Validator:
    def validate(value: Any, path: str) -> None:
        require(type(value) is type(values[0]) and value in values, path, repr(values))
    return validate


def nullable(validator: Validator) -> Validator:
    def validate(value: Any, path: str) -> None:
        if value is not None:
            validator(value, path)
    return validate


def array(validator: Validator) -> Validator:
    def validate(value: Any, path: str) -> None:
        require(isinstance(value, list), path, "array")
        for index, item in enumerate(value):
            validator(item, f"{path}[{index}]")
    return validate


def record(required: dict[str, Validator], optional: dict[str, Validator] | None = None) -> Validator:
    """Validate known fields, leaving extension fields untouched."""
    def validate(value: Any, path: str) -> None:
        require(isinstance(value, dict), path, "object")
        for key, validator in required.items():
            require(key in value, f"{path}.{key}", "field")
            validator(value[key], f"{path}.{key}")
        for key, validator in (optional or {}).items():
            if key in value:
                validator(value[key], f"{path}.{key}")
    return validate


def token_logprob(value: Any, path: str) -> None:
    require(isinstance(value, list) and len(value) == 2, path, "[token, logprob] pair")
    string(value[0], f"{path}[0]")
    number(value[1], f"{path}[1]")


STRINGS = array(string)
SCORE = {"pct": percent, "passed": count, "total": count}
MACHINE = record({"platform": string, "arch": string, "cpu": nullable(string), "memGB": number})
PHASE = record({}, {"status": choice(*PHASE_STATUSES), "reason": string})
# Missing scope facts are accepted as unknown, not fabricated or considered measured.
SCOPE = record({}, {
    "depth": choice(*DEPTHS), "mode": choice("probe", "bench-only", "eval-only"),
    "startedAt": string, "label": string, "labelAuto": boolean,
    "phases": record({}, {key: PHASE for key in (*PHASES, "reasoning")}),
    "budget": record({"exhausted": boolean}, {"limitTokens": number}),
})
AGENTIC = record({**SCORE, "tasks": array(record({
    "id": string, "name": string, "passed": boolean, "steps": count,
}, {
    "failure": string, "detail": string,
    "calls": record({"total": count, "valid": count}),
    "violations": array(record({"rule": string, "severity": choice("must", "should"),
                                 "step": nullable(count), "detail": string})),
}))})
FIDELITY = record({
    "pct": percent, "items": count,
    "slices": array(record({"id": string, "label": string, "score": number,
                            "weight": number, "detail": string, "measured": boolean},
                           {"unmeasuredReason": string})),
    "firstDivergence": nullable(record({"itemId": string, "charIndex": count,
                                         "run": count, "runs": count})),
    "unmeasured": STRINGS, "reasoningCaveat": boolean,
    "measurements": record({"meanTopProb": nullable(number), "meanMargin": nullable(number),
                            "items": array(record({"id": string, "hard": boolean,
                                                   "correct": boolean, "topProb": nullable(number),
                                                   "margin": nullable(number)}))}),
}, {"fingerprint": nullable(record({"topK": count, "prompts": array(record({
    "id": string, "tokens": array(record({"t": string, "lp": number, "top": array(token_logprob)})),
}))}))})
BENCH_STAT = nullable(record({"median": number, "min": number, "max": number, "samples": array(number)}))
# Validate the benchmark's headline shape; detailed nullable timing records remain native.
BENCH = record({"decodeTokPerSec": BENCH_STAT, "ttftMs": BENCH_STAT,
                "prefillTokPerSec": BENCH_STAT, "machine": MACHINE}, {
    "streamCaveat": nullable(string), "prefillPromptTokens": nullable(count),
    "contextScaling": nullable(array(record({"targetTokens": count, "runs": count}, {
        key: nullable(number) for key in ("inputTokens", "decodeTokPerSec", "ttftMs", "prefillTokPerSec")
    }))),
    **{key: nullable(record({})) for key in ("speculative", "prefixCache", "batching", "loadDrift")},
})
REPORT = record({
    "version": choice(1, 2),
    "target": record({"baseUrl": string, "model": string}, {"engine": string}),
    "durationMs": number,
    "coverage": record({
        "byTier": array(record({"tier": string, "supported": count, "total": count,
                                 "pct": percent, "missing": STRINGS, "unprobed": STRINGS})),
        "credits": array(record({"id": string, "label": string}, {"detail": string})),
        "entries": array(record({"id": string, "tier": string, "supported": boolean}, {
            "label": string, "kind": string, "probed": boolean, "detail": string,
        })),
    }),
    "conformance": record({
        **SCORE, "bySurface": array(record({**SCORE, "surface": string})),
        "results": array(record({"id": string, "surface": string, "outcome": string,
                                 "failures": array(record({"id": string, "severity": string},
                                                          {"label": string, "message": string}))},
                                {"name": string, "reason": string, "durationMs": number})),
    }, {key: array(record({"id": string})) for key in ("inconclusive", "warnings", "nits")}),
    "capability": record({
        "pct": percent, "verdict": choice("below-floor", "capable", "strong"),
        "categories": array(record({**SCORE, "category": string})), "weakCategories": STRINGS,
        "evals": array(record({"id": string, "category": string, "passed": count, "total": count},
                              {"name": string, "outcome": string, "failures": STRINGS})),
    }, {"unmeasured": STRINGS}),
}, {"run": SCOPE, "machine": MACHINE, "incomplete": string,
    "agentic": AGENTIC, "fidelity": FIDELITY, "bench": BENCH,
    "usage": record({"inputTokens": count, "outputTokens": count}),
    "reasoning": record({"passed": count, "total": count, "cases": array(record({}))},
                        {"aborted": nullable(record({"reason": string, "message": string}))})})


def validate_report(report: Any) -> None:
    """Raise ValueError for malformed native fields; allow v1/v2 and unknown scope."""
    REPORT(report, "report")


def reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def read_json(path: str | Path) -> Any:
    """Read strict JSON, rejecting non-finite numbers throughout the document."""
    with Path(path).open(encoding="utf-8") as stream:
        # Reject overflow even in unknown extension fields; otherwise dumping the
        # manifest could fail after an otherwise valid report was admitted.
        return json.load(stream, parse_constant=reject_constant, parse_float=finite_float)
