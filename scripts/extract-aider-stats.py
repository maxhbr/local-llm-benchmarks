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


def strip_ansi(text: str) -> str:
    """Remove ANSI color/escape codes from text."""
    return ANSI_RE.sub('', text)


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

    if not blocks:
        print(f"  Skipping: no YAML blocks found")
        return False

    unique_blocks = deduplicate_blocks(blocks)
    print(f"  {len(unique_blocks)} distinct entries")

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
