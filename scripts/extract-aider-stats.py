#!/usr/bin/env python3
"""
Extract all distinct YAML summary entries from an aider benchmark run.

Each entry is a cumulative snapshot after N test cases. Duplicates are
removed (keeping the last occurrence of each test_cases count).

Usage:
    python3 scripts/extract-aider-stats.py <benchmark-dir>

Example:
    python3 scripts/extract-aider-stats.py ./benchmarks/rtx5090-Qwen3.6-35B-A3B-UD-Q5_K_XL/

Output:
    <benchmark-dir>/aider.computed.yaml
    <benchmarks-root>/datasets.json   (regenerated to list all *.computed.yaml)
"""

import json
import os
import re
import sys

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
TEST_CASES_RE = re.compile(r'^\s+test_cases:\s+(\d+)')
RESULTS_RE = re.compile(r'^results:\s*$', re.MULTILINE)

# Per-test execution-error signals (keys as they appear in each ``results:``
# JSON block). A test that did not pass but shows any of these never got a fair
# shot at solving the exercise — an infrastructure/deployment failure, not a
# genuine coding failure. Mirrors ``ERR_SIGNALS`` in
# ``generate-aider-pertest-manifest.py`` so the two views agree.
ERR_SIGNAL_KEYS = (
    'num_error_outputs',
    'num_malformed_responses',
    'syntax_errors',
    'test_timeouts',
    'num_exhausted_context_windows',
)


def strip_ansi(text: str) -> str:
    """Remove ANSI color/escape codes from text."""
    return ANSI_RE.sub('', text)


def classify_test(obj: dict) -> tuple[bool, bool]:
    """Classify one ``results:`` block into ``(passed, infra_recoverable)``.

    * ``passed`` — the test passed on any try (``True in tests_outcomes``),
      matching aider's ``pass_num_2`` and the per-test manifest's ``p2``.
    * ``infra_recoverable`` — the test did NOT pass AND it never got a fair
      shot: either it had an execution-error signal (see ``ERR_SIGNAL_KEYS``)
      or ``tests_outcomes`` was absent (an exception case, ``status='error'``
      in the per-test manifest). Such a test is counted as a pass for the
      *theoretical* rate — the upper bound if no infrastructure error had
      occurred — but never as a genuine coding failure.
    """
    outcomes = obj.get('tests_outcomes')
    has_outcomes = isinstance(outcomes, list)
    passed = bool(has_outcomes and outcomes and True in outcomes)
    status_error = not has_outcomes
    has_err_signal = any((obj.get(k) or 0) > 0 for k in ERR_SIGNAL_KEYS)
    infra_recoverable = (not passed) and (has_err_signal or status_error)
    return passed, infra_recoverable


def parse_results_outcomes(content: str) -> list[tuple[bool, bool]]:
    """Parse every ``results:`` JSON block (in file order) into a list of
    ``(passed, infra_recoverable)`` tuples — one entry per test case, in the
    order the run executed them."""
    decoder = json.JSONDecoder()
    outcomes: list[tuple[bool, bool]] = []
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
        outcomes.append(classify_test(obj))
    return outcomes


def build_theoretical_snapshots(
    yaml_blocks: list[list[str]],
    outcomes: list[tuple[bool, bool]],
) -> dict[int, tuple[int, int]]:
    """Walk the run in order, pairing each YAML cumulative snapshot with the
    per-test outcome that produced it, and return ``{test_cases:
    (pass_num_2, theoretical_pass_num_2)}``.

    The run.log interleaves one ``results:`` block then one ``- dirname:`` YAML
    summary per test, so the *k*-th YAML block reflects the cumulative totals
    after the first *k* tests. We therefore advance one outcome per YAML block
    and snapshot the running counts at that block's ``test_cases`` value. The
    map keeps the last snapshot per ``test_cases`` (matching
    ``deduplicate_blocks``, which keeps the last block per count)."""
    snapshots: dict[int, tuple[int, int]] = {}
    pass_num = 0
    infra_num = 0
    for k, block in enumerate(yaml_blocks):
        if k < len(outcomes):
            passed, infra = outcomes[k]
            if passed:
                pass_num += 1
            if infra:
                infra_num += 1
        tc = get_test_cases_count(block)
        if tc is not None:
            snapshots[tc] = (pass_num, pass_num + infra_num)
    return snapshots


def extract_yaml_blocks(content: str) -> list[list[str]]:
    """
    Extract all YAML blocks starting with '- dirname:' from the content.
    Each block is terminated by a blank line (or end of file).
    """
    lines = content.split('\n')
    blocks: list[list[str]] = []
    current_block: list[str] | None = None

    for line in lines:
        if line.strip().startswith('- dirname:'):
            if current_block is not None:
                blocks.append(current_block)
            current_block = [line]
        elif current_block is not None:
            if not line.strip():
                blocks.append(current_block)
                current_block = None
            else:
                current_block.append(line)

    if current_block is not None:
        blocks.append(current_block)

    return blocks


def get_test_cases_count(block: list[str]) -> int | None:
    """Extract the test_cases count from a YAML block."""
    for line in block:
        m = TEST_CASES_RE.match(strip_ansi(line))
        if m:
            return int(m.group(1))
    return None


def repair_wrapped_lines(block: list[str]) -> list[str]:
    """
    Repair terminal-wrapped lines within a YAML block.

    Aider prints its YAML summary through a terminal-aware console (rich).
    When the captured terminal width is narrow, long scalar values such as
    ``model: openai/rtx5090:Some-Long-Model-Name-Q4_K_M`` are hard-wrapped
    onto a following line that has no leading indentation::

        - dirname: 2026-...-Some-Long-Model-Name-Q4_K_M
          test_cases: 14
          model:
    openai/rtx5090:Some-Long-Model-Name-Q4_K_M
          edit_format: whole

    Written out verbatim this is invalid YAML: the unindented ``openai/...``
    line is parsed as the start of a new top-level document, producing errors
    like "end of the stream or a document separator is expected".

    In a well-formed block of this schema every line after ``- dirname:`` is
    indented, so any unindented line is a wrap fragment.  Merge each fragment
    back into the preceding line, re-inserting a single separating space when
    the preceding line does not already end with whitespace.
    """
    repaired: list[str] = []
    for line in block:
        is_wrap_fragment = (
            bool(repaired)
            and line != ''
            and not line[0].isspace()
            and not line.startswith('- ')
        )
        if is_wrap_fragment:
            prev = repaired[-1]
            sep = '' if prev[-1].isspace() else ' '
            repaired[-1] = prev + sep + line
        else:
            repaired.append(line)
    return repaired


def deduplicate_blocks(blocks: list[list[str]]) -> list[list[str]]:
    """
    Deduplicate blocks by test_cases count, keeping the last occurrence.
    Returns blocks sorted by test_cases count ascending.
    """
    seen: dict[int, list[str]] = {}
    for block in blocks:
        tc = get_test_cases_count(block)
        if tc is not None:
            seen[tc] = block
    # Sort by test_cases count
    return [seen[k] for k in sorted(seen.keys())]


def inject_theoretical(
    blocks: list[list[str]],
    snapshots: dict[int, tuple[int, int]],
) -> list[list[str]]:
    """Add ``theoretical_pass_num_2`` and ``theoretical_pass_rate_2`` lines to
    each block that has a snapshot. Inserted right after ``pass_num_2:`` to keep
    the related pass-rate fields together. Blocks without a snapshot (e.g. a
    run.log with no ``results:`` blocks) are left unchanged; the front end then
    falls back to a flat band at ``pass_rate_2``."""
    out: list[list[str]] = []
    for block in blocks:
        tc = get_test_cases_count(block)
        snap = snapshots.get(tc)
        if snap is None:
            out.append(block)
            continue
        _pass_num, theo_num = snap
        theo_rate = f'{100 * theo_num / tc:.1f}' if tc else '0.0'
        insert_idx = len(block)
        for i, line in enumerate(block):
            if line.lstrip().startswith('pass_num_2:'):
                insert_idx = i + 1
                break
        new_block = list(block)
        new_block[insert_idx:insert_idx] = [
            f'  theoretical_pass_num_2: {theo_num}',
            f'  theoretical_pass_rate_2: {theo_rate}',
        ]
        out.append(new_block)
    return out


def find_latest_run_dir(benchmark_dir: str, bench_type: str = 'aider') -> str | None:
    """Find the most recent <bench_type>/<timestamp> subdirectory."""
    bench_dir = os.path.join(benchmark_dir, bench_type)
    if not os.path.isdir(bench_dir):
        return None

    subdirs = sorted(
        d for d in os.listdir(bench_dir)
        if os.path.isdir(os.path.join(bench_dir, d))
    )

    if not subdirs:
        return None

    return os.path.join(bench_dir, subdirs[-1])


def get_output_filename(bench_type: str) -> str:
    """Map benchmark type to output filename."""
    return f"{bench_type}.computed.yaml"


def process_benchmark(benchmark_dir: str, bench_type: str = 'aider') -> bool:
    """
    Process a single benchmark directory for the given bench type.
    Returns True if successful, False if skipped/failed.
    """
    run_dir = find_latest_run_dir(benchmark_dir, bench_type)
    if run_dir is None:
        return False

    run_log = os.path.join(run_dir, 'run.log')
    if not os.path.isfile(run_log):
        return False

    print(f"  {run_log}")

    with open(run_log, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    clean_content = strip_ansi(content)
    blocks = extract_yaml_blocks(clean_content)
    blocks = [repair_wrapped_lines(b) for b in blocks]

    if not blocks:
        print(f"  Skipping: no YAML blocks found")
        return False

    # Theoretical-best pass_rate_2: parse the per-test ``results:`` blocks of
    # the same run.log (in order) and, for each cumulative snapshot, count how
    # many tests would have passed if every infrastructure error had instead
    # been a success (i.e. passed + infra-recoverable). Injected as extra YAML
    # fields so the front end can plot a band above pass_rate_2 with no formula.
    outcomes = parse_results_outcomes(clean_content)
    snapshots = build_theoretical_snapshots(blocks, outcomes)
    unique_blocks = deduplicate_blocks(blocks)
    unique_blocks = inject_theoretical(unique_blocks, snapshots)
    print(f"  {len(unique_blocks)} distinct entries"
          + (f" ({len(snapshots)} with theoretical)" if snapshots else ""))

    yaml_output = '\n'.join('\n'.join(block) for block in unique_blocks)

    output_path = os.path.join(benchmark_dir, get_output_filename(bench_type))
    with open(output_path, 'w') as f:
        f.write(yaml_output + '\n')

    print(f"  Written: {output_path}")
    return True


def discover_benchmarks(base_dir: str) -> list[tuple[str, str]]:
    """
    Discover all benchmark subdirectories containing aider or aider-diff.
    Returns list of (benchmark_dir, bench_type) tuples.
    """
    results = []
    bench_types = ['aider', 'aider-diff']

    for entry in sorted(os.listdir(base_dir)):
        bench_dir = os.path.join(base_dir, entry)
        if not os.path.isdir(bench_dir):
            continue
        for bt in bench_types:
            if os.path.isdir(os.path.join(bench_dir, bt)):
                results.append((bench_dir, bt))

    return results


def update_datasets_json(base_dir: str) -> None:
    """
    Regenerate datasets.json in base_dir with all *.computed.yaml files
    found in immediate subfolders.
    """
    files: list[str] = []
    for entry in sorted(os.listdir(base_dir)):
        sub = os.path.join(base_dir, entry)
        if not os.path.isdir(sub):
            continue
        for name in sorted(os.listdir(sub)):
            if name.endswith('.computed.yaml'):
                files.append(f"{entry}/{name}")

    output_path = os.path.join(base_dir, 'datasets.json')
    with open(output_path, 'w') as f:
        json.dump({'files': files}, f, indent=2)
        f.write('\n')

    print(f"\nWrote {output_path} ({len(files)} files)")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Extract YAML summary entries from aider benchmark run logs.',
    )
    parser.add_argument('path', help='Benchmark directory or benchmarks root (with --all)')
    parser.add_argument('--all', action='store_true',
                        help='Scan path for all benchmarks with aider/aider-diff subdirs')
    args = parser.parse_args()

    base_dir = args.path.rstrip('/')

    if not os.path.isdir(base_dir):
        print(f"Error: {base_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.all:
        targets = discover_benchmarks(base_dir)
        if not targets:
            print("No benchmarks found.", file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(targets)} benchmark(s) in {base_dir}")
        success = 0
        for bench_dir, bench_type in targets:
            print(f"\nProcessing: {bench_dir} ({bench_type})")
            if process_benchmark(bench_dir, bench_type):
                success += 1

        print(f"\nDone: {success}/{len(targets)} written")
        update_datasets_json(base_dir)
    else:
        # Single benchmark directory mode
        # Process all matching bench types in the directory
        found = False
        for bench_type in ['aider', 'aider-diff']:
            if os.path.isdir(os.path.join(base_dir, bench_type)):
                if process_benchmark(base_dir, bench_type):
                    found = True

        if not found:
            print(f"Error: No aider or aider-diff directory found in {base_dir}", file=sys.stderr)
            sys.exit(1)

        # Update datasets.json in the parent (benchmarks/) directory
        update_datasets_json(os.path.dirname(os.path.abspath(base_dir)))


if __name__ == '__main__':
    main()
