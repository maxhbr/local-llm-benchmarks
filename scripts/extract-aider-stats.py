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
"""

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


def find_latest_aider_dir(benchmark_dir: str) -> str | None:
    """Find the most recent aider/<timestamp> subdirectory."""
    aider_dir = os.path.join(benchmark_dir, 'aider')
    if not os.path.isdir(aider_dir):
        return None

    # List subdirectories and sort by name (timestamp format ensures lexicographic = chronological)
    subdirs = sorted(
        d for d in os.listdir(aider_dir)
        if os.path.isdir(os.path.join(aider_dir, d))
    )

    if not subdirs:
        return None

    return os.path.join(aider_dir, subdirs[-1])


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <benchmark-dir>", file=sys.stderr)
        sys.exit(1)

    benchmark_dir = sys.argv[1].rstrip('/')
    run_dir = find_latest_aider_dir(benchmark_dir)

    if run_dir is None:
        print(f"Error: No aider run directories found in {benchmark_dir}", file=sys.stderr)
        sys.exit(1)

    run_log = os.path.join(run_dir, 'run.log')
    if not os.path.isfile(run_log):
        print(f"Error: run.log not found in {run_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading: {run_log}")

    with open(run_log, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Strip ANSI codes before parsing
    clean_content = strip_ansi(content)
    blocks = extract_yaml_blocks(clean_content)

    if not blocks:
        print("Error: No YAML blocks found in run.log", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(blocks)} YAML block(s), deduplicating...")
    unique_blocks = deduplicate_blocks(blocks)
    print(f"{len(unique_blocks)} distinct entries (test_cases 1..{len(unique_blocks)})")

    # Join blocks: each is already a YAML list item starting with '-'
    yaml_output = '\n'.join('\n'.join(block) for block in unique_blocks)

    output_path = os.path.join(benchmark_dir, 'aider.computed.yaml')
    with open(output_path, 'w') as f:
        f.write(yaml_output + '\n')

    print(f"Written: {output_path}")


if __name__ == '__main__':
    main()
