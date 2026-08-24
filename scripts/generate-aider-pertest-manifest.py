#!/usr/bin/env python3
"""Compile per-test-case outcomes from every aider / aider-diff ``run.log`` into a
test-case × model matrix.

Aider runs its polyglot benchmark exercises in a random order (``benchmark.py``
shuffles the test directories with no seed), so the matrix is keyed on the
*identity* of each test case — ``<lang>/<exercise>`` — never on the order in
which tests ran. Each ``run.log`` prints one pretty-printed JSON object per test
case behind a line that is exactly ``results:``::

    results:
    {
        "testdir": ".../java/exercises/practice/satellite",
        "testcase": "satellite",
        "model": "openai/rtx5090:Qwen3.6-27B-UD-Q5_K_XL",
        "edit_format": "whole",
        "tests_outcomes": [false, false],
        ...
    }

From ``tests_outcomes`` (a list of booleans, one per try) we derive, matching
aider's own metrics:

    p1 = outcomes[0] is True          # pass_rate_1  (passed on the first try)
    p2 = True in outcomes             # pass_rate_2  (passed on any try)  <- headline

Many runs completed only a subset of the 225 exercises (timeouts, smoke tests,
re-runs). For each ``(model-folder, bench-type)`` column we therefore pick the
single run that parsed the most test-case blocks (most complete); ties break to
the newest timestamp. The chosen ``run_dir`` is recorded in the column metadata
so the selection is auditable.

Output (two files):

  * ``<root>/aider-pertest.json`` — consumed by ``index.html``:
        {
          "generated_at": "...", "column_count": N, "row_count": M,
          "columns": [ {column_id, folder, bench, edit_format, model,
                        run_dir, num_tests_run}, ... ],
          "rows":    [ {row_id, lang, exercise, results: {column_id:
                       {p1, p2, n, status}}}, ... ],
          "skipped": ["<folder>/<bench>: <reason>", ...],
          "fallback_keys": ["<row_id>", ...]
        }
    ``status`` is ``"ran"`` when ``tests_outcomes`` was present, ``"error"``
    when the block had no outcomes (exception case); a column that never ran a
    given test simply has no entry for it (sparse matrix). For ``"ran"`` cells,
    ``err``/``errd`` are added when the run had execution problems
    (error outputs, malformed responses, syntax errors, test timeouts, or
    exhausted context windows) — these failed despite never getting a fair shot.

  * ``<root>/aider-pertest.csv`` — human-readable intermediate (p2 metric):
        row_id,lang,exercise,<column_id>,<column_id>,...
        java/satellite,java,satellite,FAIL,PASS,,ERR,...
    Cell values: ``PASS`` (passed on any try), ``FAIL`` (ran, never passed),
    ``EXEC`` (ran, never passed, but the execution had errors / malformed
    responses / timeouts / exhausted context), ``ERR`` (error/no outcomes),
    empty (not run by that model).

Usage:
    python3 scripts/generate-aider-pertest-manifest.py
    python3 scripts/generate-aider-pertest-manifest.py --root benchmarks/
    python3 scripts/generate-aider-pertest-manifest.py -o out.json --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from typing import Any

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
RESULTS_RE = re.compile(r'^results:\s*$', re.MULTILINE)
TESTDIR_RE = re.compile(r'/(\w+)/exercises/practice/([^/]+)')

BENCH_TYPES = ('aider', 'aider-diff')

# Per-test signals (from each ``results:`` block) that indicate the test case
# execution had problems — the model never got a fair shot at solving it. A
# failed test with any of these is shown with a distinct icon in the matrix,
# separate from a clean "the model wrote valid code that is just wrong" FAIL.
# ``num_user_asks`` is deliberately excluded: it counts the model asking for
# clarification, which is normal aider behaviour, not an execution error.
ERR_SIGNALS = (
    ('error_outputs', 'num_error_outputs'),
    ('malformed', 'num_malformed_responses'),
    ('syntax_errors', 'syntax_errors'),
    ('timeouts', 'test_timeouts'),
    ('ctx_exhausted', 'num_exhausted_context_windows'),
)


def exec_problem(obj: dict) -> list[str]:
    """Return ``["error_outputs:15", "timeouts:1", ...]`` for any non-zero
    execution-error signal in a results block (empty when the run was clean)."""
    parts: list[str] = []
    for label, key in ERR_SIGNALS:
        try:
            v = int(obj.get(key, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            parts.append(f"{label}:{v}")
    return parts


def strip_ansi(text: str) -> str:
    """Remove ANSI color/escape codes from text."""
    return ANSI_RE.sub('', text)


def discover_benchmarks(base_dir: str) -> list[tuple[str, str]]:
    """Return ``[(benchmark_dir, bench_type)]`` for every folder that has an
    ``aider`` or ``aider-diff`` subdirectory."""
    results: list[tuple[str, str]] = []
    for entry in sorted(os.listdir(base_dir)):
        bench_dir = os.path.join(base_dir, entry)
        if not os.path.isdir(bench_dir):
            continue
        for bt in BENCH_TYPES:
            if os.path.isdir(os.path.join(bench_dir, bt)):
                results.append((bench_dir, bt))
    return results


def list_run_dirs(benchmark_dir: str, bench_type: str) -> list[str]:
    """All timestamped run subdirectories for a bench type, sorted oldest→newest."""
    bench_dir = os.path.join(benchmark_dir, bench_type)
    if not os.path.isdir(bench_dir):
        return []
    subdirs = sorted(
        d for d in os.listdir(bench_dir)
        if os.path.isdir(os.path.join(bench_dir, d))
    )
    return [os.path.join(bench_dir, d) for d in subdirs]


def parse_run_log(path: str) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    """Parse one ``run.log`` into ``{row_id: cell}`` plus the run's model/edit_format.

    A *cell* is ``{"p1": bool|None, "p2": bool|None, "n": int|None, "status": str}``.
    ``status`` is ``"ran"`` or ``"error"``. Rows are keyed on ``<lang>/<exercise>``;
    when ``testdir`` does not match the practice-exercise path the row falls back
    to the bare ``testcase`` (and is flagged by the caller via ``fallback_keys``).

    Returns ``(rows, model, edit_format)`` where ``model``/``edit_format`` come
    from the first parseable block (the run's ground truth).
    """
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = strip_ansi(f.read())

    decoder = json.JSONDecoder()
    rows: dict[str, dict[str, Any]] = {}
    model: str | None = None
    edit_format: str | None = None

    for m in RESULTS_RE.finditer(content):
        brace = content.find('{', m.end())
        if brace < 0:
            continue
        try:
            obj, _end = decoder.raw_decode(content, brace)
        except json.JSONDecodeError:
            # truncated/malformed block (interrupted run) — skip, never abort
            continue
        if not isinstance(obj, dict):
            continue
        testcase = obj.get('testcase')
        testdir = obj.get('testdir')
        if testcase is None or testdir is None:
            continue

        mm = TESTDIR_RE.search(str(testdir))
        row_id = f"{mm.group(1)}/{mm.group(2)}" if mm else str(testcase)

        outcomes = obj.get('tests_outcomes')
        if isinstance(outcomes, list) and outcomes:
            p1 = outcomes[0] is True
            p2 = True in outcomes
            n = len(outcomes)
            status = 'ran'
        elif isinstance(outcomes, list) and not outcomes:
            # empty list — treated as ran with no successful tries
            p1 = p2 = False
            n = 0
            status = 'ran'
        else:
            # tests_outcomes absent (exception case in benchmark.py)
            p1 = p2 = None
            n = None
            status = 'error'

        cell: dict[str, Any] = {'p1': p1, 'p2': p2, 'n': n, 'status': status}
        if status == 'ran':
            problem = exec_problem(obj)
            if problem:
                cell['err'] = True
                cell['errd'] = ', '.join(problem)
        rows[row_id] = cell

        if model is None:
            model = obj.get('model')
        if edit_format is None:
            edit_format = obj.get('edit_format')

    return rows, model, edit_format


def choose_best_run(run_dirs: list[str], skipped: list[str], label: str
                    ) -> tuple[str, dict[str, dict[str, Any]], str, str, str] | None:
    """Parse every run dir and pick the one with the most parsed test-case
    blocks (most complete); ties break to the newest (lexicographically largest
    timestamp dir name). Returns ``(run_dir, rows, run_dir_name, model,
    edit_format)`` or ``None`` when no run parsed any block."""
    candidates: list[tuple[int, str, dict[str, dict[str, Any]], str, str, str]] = []
    for rd in run_dirs:
        log = os.path.join(rd, 'run.log')
        if not os.path.isfile(log):
            skipped.append(f"{label}: missing run.log in {os.path.basename(rd)}")
            continue
        try:
            rows, model, edit_format = parse_run_log(log)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{label}/{os.path.basename(rd)}: {e}")
            continue
        if not rows:
            skipped.append(f"{label}/{os.path.basename(rd)}: no per-test blocks")
            continue
        candidates.append((len(rows), os.path.basename(rd), rows, rd, model or '', edit_format or ''))

    if not candidates:
        return None
    # most blocks first, then newest timestamp
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _count, name, rows, run_dir, model, edit_format = candidates[0]
    return run_dir, rows, name, model, edit_format


def build_manifest(root: str) -> dict[str, Any]:
    targets = discover_benchmarks(root)
    columns: list[dict[str, Any]] = []
    column_cells: list[tuple[str, dict[str, dict[str, Any]]]] = []  # (column_id, rows)
    skipped: list[str] = []
    fallback_keys: set[str] = set()

    for bench_dir, bench_type in targets:
        folder = os.path.basename(bench_dir)
        label = f"{folder}/{bench_type}"
        run_dirs = list_run_dirs(bench_dir, bench_type)
        if not run_dirs:
            skipped.append(f"{label}: no run directories")
            continue
        best = choose_best_run(run_dirs, skipped, label)
        if best is None:
            continue
        run_dir, rows, run_dir_name, model, edit_format = best
        column_id = f"{folder}|{bench_type}"

        # detect fallback-keyed rows (no lang/ prefix)
        for rid in rows:
            if '/' not in rid:
                fallback_keys.add(rid)

        columns.append({
            'column_id': column_id,
            'folder': folder,
            'bench': bench_type,
            'edit_format': edit_format,
            'model': model,
            'run_dir': run_dir_name,
            'num_tests_run': len(rows),
        })
        column_cells.append((column_id, rows))

    columns.sort(key=lambda c: (c['folder'], c['bench']))

    # union of all row_ids, sorted by (lang, exercise)
    all_row_ids: set[str] = set()
    for _cid, rows in column_cells:
        all_row_ids.update(rows.keys())

    def row_sort_key(rid: str) -> tuple[str, str]:
        if '/' in rid:
            lang, ex = rid.split('/', 1)
            return (lang, ex)
        return ('~', rid)  # fallback keys sort last

    row_list: list[dict[str, Any]] = []
    for rid in sorted(all_row_ids, key=row_sort_key):
        lang, exercise = (rid.split('/', 1) + [''])[:2] if '/' in rid else ('', rid)
        results: dict[str, dict[str, Any]] = {}
        for column_id, rows in column_cells:
            cell = rows.get(rid)
            if cell is not None:
                results[column_id] = cell
        row_list.append({
            'row_id': rid,
            'lang': lang,
            'exercise': exercise,
            'results': results,
        })

    return {
        'generated_at': dt.datetime.now().isoformat(timespec='seconds'),
        'column_count': len(columns),
        'row_count': len(row_list),
        'columns': columns,
        'rows': row_list,
        'skipped': skipped,
        'fallback_keys': sorted(fallback_keys),
    }


def write_csv(manifest: dict[str, Any], path: str) -> None:
    columns = manifest['columns']
    rows = manifest['rows']
    header = ['row_id', 'lang', 'exercise'] + [c['column_id'] for c in columns]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            line = [r['row_id'], r['lang'], r['exercise']]
            for c in columns:
                cell = r['results'].get(c['column_id'])
                if cell is None:
                    line.append('')
                elif cell['status'] == 'error':
                    line.append('ERR')
                elif cell['p2']:
                    line.append('PASS')
                elif cell.get('err'):
                    line.append('EXEC')
                else:
                    line.append('FAIL')
            w.writerow(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Build a per-test-case × model matrix from aider run logs.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--root',
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'benchmarks',
        ),
        help='benchmarks/ root to scan (default: ./benchmarks)',
    )
    parser.add_argument(
        '-o', '--output',
        default=None,
        help='output JSON path (default: <root>/aider-pertest.json)',
    )
    parser.add_argument(
        '--csv',
        default=None,
        help='output CSV path (default: <root>/aider-pertest.csv)',
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    manifest = build_manifest(root)

    json_path = args.output or os.path.join(root, 'aider-pertest.json')
    csv_path = args.csv or os.path.join(root, 'aider-pertest.csv')

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')
    write_csv(manifest, csv_path)

    print(f"wrote {json_path}: {manifest['column_count']} columns, "
          f"{manifest['row_count']} rows")
    print(f"wrote {csv_path}")
    if manifest['skipped']:
        print(f"  skipped {len(manifest['skipped'])}:")
        for s in manifest['skipped']:
            print(f"    {s}")
    if manifest['fallback_keys']:
        print(f"  {len(manifest['fallback_keys'])} row(s) fell back to bare testcase "
              f"(no lang/ prefix): {manifest['fallback_keys']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
