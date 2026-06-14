#!/usr/bin/env python3
"""Convert llama-benchy.md tables into machine-readable CSV rows.

Each row of the markdown table looks like:

    | rtx5090:Qwen3.6-27B-UD-Q6_K_XL | pp2048 @ d8192 (c2) | 2090.32 ± 4.95 | ... |

Which is converted into a CSV row with the columns:

    model, test, depth, concurrency, t/s, t/s_err,
    t/s (req), t/s (req)_err,
    peak t/s, peak t/s_err,
    peak t/s (req), peak t/s (req)_err,
    ttfr (ms), ttfr (ms)_err,
    est_ppt (ms), est_ppt (ms)_err,
    e2e_ttft (ms), e2e_ttft (ms)_err

Usage:
    # Convert a single file (prints CSV to stdout):
    llama-benchy-md-to-csv.py benchmarks/<dir>/llama-benchy.md

    # Walk a directory tree, gathering every llama-benchy.md, and emit one
    # master CSV (to stdout, or to -o/--output):
    llama-benchy-md-to-csv.py --all benchmarks/
    llama-benchy-md-to-csv.py --all benchmarks/ -o master.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

# Columns whose values follow the "VALUE ± ERR" pattern. The order matches the
# columns in the source markdown after `model` and `test`.
VALUE_COLUMNS: list[str] = [
    "t/s (total)",
    "t/s (req)",
    "peak t/s",
    "peak t/s (req)",
    "ttfr (ms)",
    "est_ppt (ms)",
    "e2e_ttft (ms)",
]

# CSV header: parsed `test` is split into test/depth/concurrency, and "t/s
# (total)" is renamed to plain "t/s" because the user asked for that exact
# name. Every other value column gets a paired `_err` column.
CSV_HEADER: list[str] = ["model", "test", "depth", "concurrency"]
for col in VALUE_COLUMNS:
    name = "t/s" if col == "t/s (total)" else col
    CSV_HEADER.append(name)
    CSV_HEADER.append(f"{name}_err")


# Matches e.g. "pp2048 (c1)", "tg128 @ d8192 (c2)", "pp2048 @ d16384 (c4)".
TEST_RE = re.compile(
    r"""^
        (?P<kind>pp\d+|tg\d+)
        (?:\s*@\s*d(?P<depth>\d+))?
        \s*\(c(?P<conc>\d+)\)
        $""",
    re.VERBOSE,
)

# Matches "3167.90 ± 7.12" (allowing negatives just in case). Also matches a
# bare "3167.90" without an error term, for robustness.
VALUE_RE = re.compile(
    r"^\s*(?P<value>-?\d+(?:\.\d+)?)(?:\s*±\s*(?P<err>-?\d+(?:\.\d+)?))?\s*$"
)


def split_md_row(line: str) -> list[str] | None:
    """Split a markdown table row into trimmed cell strings.

    Returns None if the line is not a table row.
    """
    s = line.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return None
    # Drop the leading and trailing pipes, then split.
    inner = s[1:-1]
    return [cell.strip() for cell in inner.split("|")]


def is_separator_row(cells: list[str]) -> bool:
    """A markdown header separator row is composed of cells like `---`, `:---`,
    `---:`, or `:---:` (with any number of dashes)."""
    return all(re.fullmatch(r":?-+:?", c or "") for c in cells)


def parse_test(test_str: str) -> tuple[str, str, str] | None:
    """Parse e.g. 'pp2048 @ d8192 (c2)' into ('pp2048', 'd8192', '2').

    Depth defaults to 'd0' when not present. Returns None on no match.
    """
    m = TEST_RE.match(test_str.strip())
    if not m:
        return None
    kind = m.group("kind")
    depth = m.group("depth")
    depth_str = f"d{depth}" if depth else "d0"
    return kind, depth_str, m.group("conc")


def parse_value(cell: str) -> tuple[str, str]:
    """Parse a 'VALUE ± ERR' cell into (value, err) strings.

    Empty cells produce ('', ''). Cells that don't match the value pattern are
    returned verbatim as (cell, '') so we don't silently drop data.
    """
    if not cell:
        return "", ""
    m = VALUE_RE.match(cell)
    if not m:
        return cell, ""
    return m.group("value"), (m.group("err") or "")


def iter_rows(path: Path) -> Iterator[list[str]]:
    """Yield CSV rows (as lists of strings) from a single llama-benchy.md file."""
    with path.open(encoding="utf-8") as f:
        lines = f.readlines()

    header_cells: list[str] | None = None
    saw_separator = False

    for raw in lines:
        cells = split_md_row(raw)
        if cells is None:
            continue

        if header_cells is None:
            # First table row is the header.
            header_cells = cells
            continue

        if not saw_separator and is_separator_row(cells):
            saw_separator = True
            continue

        # Data row. Expect at least: model, test, then the value columns.
        if len(cells) < 2 + len(VALUE_COLUMNS):
            print(
                f"warning: {path}: skipping short row: {raw.rstrip()}",
                file=sys.stderr,
            )
            continue

        model_cell, test_cell, *value_cells = cells
        parsed = parse_test(test_cell)
        if parsed is None:
            print(
                f"warning: {path}: unparseable test name {test_cell!r}",
                file=sys.stderr,
            )
            continue
        kind, depth, conc = parsed

        out: list[str] = [model_cell, kind, depth, conc]
        for cell in value_cells[: len(VALUE_COLUMNS)]:
            value, err = parse_value(cell)
            out.append(value)
            out.append(err)
        yield out


def find_md_files(root: Path) -> list[Path]:
    return sorted(root.rglob("llama-benchy.md"))


def write_csv(rows: Iterable[list[str]], out) -> None:
    w = csv.writer(out)
    w.writerow(CSV_HEADER)
    for row in rows:
        w.writerow(row)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "files",
        nargs="*",
        default=[],
        help="One or more llama-benchy.md files to convert.",
    )
    g.add_argument(
        "--all",
        metavar="DIR",
        help="Recursively find every llama-benchy.md under DIR and emit one master CSV.",
    )
    p.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Write CSV to PATH instead of stdout.",
    )
    args = p.parse_args(argv)

    if args.all:
        root = Path(args.all)
        if not root.is_dir():
            print(f"error: {root} is not a directory", file=sys.stderr)
            return 2
        files = find_md_files(root)
    else:
        if not args.files:
            p.error("provide one or more files, or use --all DIR")
        files = [Path(f) for f in args.files]

    def all_rows() -> Iterator[list[str]]:
        for path in files:
            if not path.is_file():
                print(f"warning: skipping missing file {path}", file=sys.stderr)
                continue
            yield from iter_rows(path)

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            write_csv(all_rows(), f)
    else:
        write_csv(all_rows(), sys.stdout)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
