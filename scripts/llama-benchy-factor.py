#!/usr/bin/env python3
"""Compute the llama-benchy performance factor between two GPU families.

For every model that was benchmarked with llama-benchy on *both* GPUs (by
default the `rtx5090-<model>` and `gfx1151-<model>` run directories), this script
parses the two `llama-benchy.md` tables and reports how many times faster one
GPU is than the other for each test and metric.

The **factor** is always expressed as "rtx5090 is N× faster than gfx1151":

* throughput metrics (t/s …): ``factor = rtx5090 / gfx1151``  (higher is better)
* latency metrics (ttfr / est_ppt / e2e_ttft, in ms): ``factor = gfx1151 / rtx5090``
  (lower latency is better, so inverting makes ``> 1`` mean rtx5090 is faster)

A value ``> 1.0`` therefore always means rtx5090 wins; ``< 1.0`` means gfx1151
wins. Each model also gets a geometric-mean "overall" factor across all the
base-depth (d0) throughput and inverted-latency cells, which is a single
headline number summarising the two GPUs.

The report is written as Markdown (default: ``benchmarks/llama-benchy-factor.md``)
with:

* a **summary** table (one row per model: key metrics + overall geo-mean)
* a **detailed** section (one table per model, full test matrix × metrics)

Usage:
    python3 scripts/llama-benchy-factor.py
    python3 scripts/llama-benchy-factor.py --gpu-a rtx5090 --gpu-b gfx1151
    python3 scripts/llama-benchy-factor.py --stdout
    python3 scripts/llama-benchy-factor.py -o benchmarks/llama-benchy-factor.md
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TextIO

# Reuse the canonical result.md / llama-benchy.md parser so the column
# meanings stay in lock-step with scripts/llama-benchy-md-to-csv.py.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_md2csv", _HERE / "llama-benchy-md-to-csv.py"
)
_md2csv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_md2csv)  # type: ignore[union-attr]

# Map the CSV column name -> whether higher is better. Sourced from the
# parser's VALUE_COLUMNS so we never drift from it.
VALUE_COLUMNS: list[str] = _md2csv.VALUE_COLUMNS
THROUGHPUT_COLS: list[str] = [
    "t/s (total)",
    "t/s (req)",
    "peak t/s",
    "peak t/s (req)",
]
LATENCY_COLS: list[str] = ["ttfr (ms)", "est_ppt (ms)", "e2e_ttft (ms)"]


def _csv_index(col: str) -> int:
    """Position of a VALUE_COLUMN's value (not its _err pair) in an iter_rows row."""
    # Row layout: [model, test, depth, concurrency, v0, e0, v1, e1, ...]
    return 4 + VALUE_COLUMNS.index(col) * 2


@dataclass
class Row:
    model: str
    kind: str          # pp2048 / tg128
    depth: str         # d0 / d8192 / d16384
    conc: str          # 1 / 2 / 4
    values: dict[str, Optional[float]] = field(default_factory=dict)

    @property
    def test_label(self) -> str:
        if self.depth == "d0":
            return f"{self.kind} (c{self.conc})"
        return f"{self.kind} @ {self.depth} (c{self.conc})"


def parse_file(path: Path) -> dict[tuple[str, str, str], Row]:
    """Parse one llama-benchy.md into { (kind, depth, conc): Row }."""
    out: dict[tuple[str, str, str], Row] = {}
    model_key = path.parent.name
    for raw in _md2csv.iter_rows(path, model_key):
        kind, depth, conc = raw[1], raw[2], raw[3]
        row = Row(model=model_key, kind=kind, depth=depth, conc=conc)
        for col in VALUE_COLUMNS:
            cell = raw[_csv_index(col)]
            row.values[col] = float(cell) if cell not in ("", None) else None
        out[(kind, depth, conc)] = row
    return out


def factor(a: Optional[float], b: Optional[float], higher_is_better: bool) -> Optional[float]:
    """rtx5090-vs-gfx1151 factor where >1 always means A (rtx5090) is faster."""
    if a is None or b is None or b == 0 or a == 0:
        return None
    if higher_is_better:
        return a / b
    return b / a


def geomean(xs: list[float]) -> Optional[float]:
    xs = [x for x in xs if x > 0]
    if not xs:
        return None
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def find_pairs(bench_dir: Path, gpu_a: str, gpu_b: str) -> list[str]:
    """Return model aliases that have a llama-benchy.md on *both* GPUs."""
    a_models = {p.parent.name[len(gpu_a) + 1:]
                for p in bench_dir.glob(f"{gpu_a}-*/llama-benchy.md")}
    b_models = {p.parent.name[len(gpu_b) + 1:]
                for p in bench_dir.glob(f"{gpu_b}-*/llama-benchy.md")}
    return sorted(a_models & b_models)


def _fmt(v: Optional[float], spec: str = ".2f") -> str:
    return "-" if v is None else format(v, spec)


def _write_table(out: TextIO, headers: list[str], rows: list[list[str]]) -> None:
    out.write("| " + " | ".join(headers) + " |\n")
    out.write("|" + "|".join("---" for _ in headers) + "|\n")
    for r in rows:
        out.write("| " + " | ".join(r) + " |\n")
    out.write("\n")


# Summary metrics: (label, kind, conc, depth, column).  These are the headline
# numbers shown one-per-model in the summary table.
SUMMARY_METRICS: list[tuple[str, str, str, str, str]] = [
    ("gen c1 (t/s)",       "tg128",  "1", "d0", "t/s (total)"),
    ("gen c4 t/s(req)",    "tg128",  "4", "d0", "t/s (req)"),
    ("prefill c1 (t/s)",   "pp2048", "1", "d0", "t/s (total)"),
    ("prefill c4 t/s(req)","pp2048", "4", "d0", "t/s (req)"),
]


def render(pairs: list[str], bench_dir: Path, gpu_a: str, gpu_b: str,
           out: TextIO) -> None:
    out.write(f"# llama-benchy factor: {gpu_a} vs {gpu_b}\n\n")
    out.write(
        f"Performance factor between {gpu_a} and {gpu_b} from matching "
        f"`llama-benchy.md` runs. For every model benchmarked on **both** GPUs "
        f"({len(pairs)} models), the factor is **{gpu_a} is N× faster than "
        f"{gpu_b}**:\n\n"
        f"- throughput metrics (t/s …): `{gpu_a} / {gpu_b}` (higher is better)\n"
        f"- latency metrics (ttfr / est_ppt / e2e_ttft, in ms): "
        f"`{gpu_b} / {gpu_a}` (lower latency is better, inverted so `> 1` "
        f"means {gpu_a} is faster)\n\n"
        f"**`> 1.0` ⇒ {gpu_a} wins · `< 1.0` ⇒ {gpu_b} wins.** The *overall* "
        f"column is the geometric mean of every d0 (base-depth) throughput and "
        f"inverted-latency factor (equal weight per cell in log-space).\n\n"
        f"### Caveats\n\n"
        f"- Extreme factors (very large, or near 0) are read straight from the "
        f"source `llama-benchy.md` files. They usually mean one GPU's run broke "
        f"at that depth/concurrency (e.g. an absurd t/s from a measurement "
        f"error, or a genuine collapse such as {gpu_a} stalling at c4). They are "
        f"kept as-is rather than clamped, so the table stays faithful to the runs.\n"
        f"- The *overall* geo-mean only uses base-depth (d0) cells, so the "
        f"deep-context (d8192/d16384) blow-ups do not pull it around, but a single "
        f"catastrophic d0 cell can still move it noticeably.\n"
        f"- Cells shown as `-` mean the metric was empty for that test on one or "
        f"both GPUs (e.g. `peak t/s` is only populated for `tg` rows, latency "
        f"cols only for `pp` rows).\n\n"
    )

    # Parse everything up front.
    parsed: dict[str, dict[str, dict[tuple[str, str, str], Row]]] = {}
    for m in pairs:
        parsed[m] = {
            gpu_a: parse_file(bench_dir / f"{gpu_a}-{m}" / "llama-benchy.md"),
            gpu_b: parse_file(bench_dir / f"{gpu_b}-{m}" / "llama-benchy.md"),
        }

    # --- Summary table ----------------------------------------------------------
    out.write("## Summary (one row per model)\n\n")
    headers = ["Model"] + [lbl for lbl, *_ in SUMMARY_METRICS] + ["overall (geo-mean d0)"]
    rows: list[list[str]] = []
    for m in pairs:
        cells = [m]
        factors_all: list[float] = []
        for _, kind, conc, depth, col in SUMMARY_METRICS:
            a = parsed[m][gpu_a].get((kind, depth, conc))
            b = parsed[m][gpu_b].get((kind, depth, conc))
            f = factor(a.values[col] if a else None,
                       b.values[col] if b else None,
                       higher_is_better=True)
            cells.append(_fmt(f))
            if f is not None:
                factors_all.append(f)
        # overall: geo-mean across ALL d0 cells (throughput + inverted latency)
        overall: list[float] = []
        for (kind, depth, conc), a_row in parsed[m][gpu_a].items():
            if depth != "d0":
                continue
            b_row = parsed[m][gpu_b].get((kind, depth, conc))
            for col in VALUE_COLUMNS:
                f = factor(a_row.values[col],
                           b_row.values[col] if b_row else None,
                           higher_is_better=col in THROUGHPUT_COLS)
                if f is not None:
                    overall.append(f)
        cells.append(_fmt(geomean(overall)))
        rows.append(cells)
    _write_table(out, headers, rows)

    # --- Detailed tables (one per model) ---------------------------------------
    out.write("## Detailed factors (per model)\n\n")
    for m in pairs:
        out.write(f"### {m}\n\n")
        a_map = parsed[m][gpu_a]
        b_map = parsed[m][gpu_b]
        keys = sorted(a_map.keys(), key=lambda k: (k[0], int(k[1][1:]), int(k[2])))
        headers = ["test"] + VALUE_COLUMNS
        rows = []
        for key in keys:
            kind, depth, conc = key
            a_row = a_map[key]
            b_row = b_map.get(key)
            cells = [a_row.test_label]
            for col in VALUE_COLUMNS:
                f = factor(a_row.values[col],
                           b_row.values[col] if b_row else None,
                           higher_is_better=col in THROUGHPUT_COLS)
                cells.append(_fmt(f))
            rows.append(cells)
        out.write(f"Factor = {gpu_a} is N× faster than {gpu_b} "
                  f"(latency cols inverted; `> 1` ⇒ {gpu_a} wins).\n\n")
        _write_table(out, headers, rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    repo_root = _HERE.parent
    p.add_argument("--benchmarks-dir", default=str(repo_root / "benchmarks"))
    p.add_argument("--gpu-a", default="rtx5090", help="First GPU prefix (numerator).")
    p.add_argument("--gpu-b", default="gfx1151", help="Second GPU prefix (denominator).")
    p.add_argument("-o", "--output",
                   default=str(repo_root / "benchmarks" / "llama-benchy-factor.md"))
    p.add_argument("--stdout", action="store_true")
    args = p.parse_args(argv)

    bench_dir = Path(args.benchmarks_dir)
    if not bench_dir.is_dir():
        print(f"error: {bench_dir} is not a directory", file=sys.stderr)
        return 2

    pairs = find_pairs(bench_dir, args.gpu_a, args.gpu_b)
    if not pairs:
        print(f"error: no models with llama-benchy.md on both {args.gpu_a} and "
              f"{args.gpu_b} under {bench_dir}", file=sys.stderr)
        return 1
    print(f"Found {len(pairs)} matching pairs: {args.gpu_a} vs {args.gpu_b}",
          file=sys.stderr)

    buf = io.StringIO()
    render(pairs, bench_dir, args.gpu_a, args.gpu_b, buf)

    if args.stdout:
        sys.stdout.write(buf.getvalue())
    else:
        out_path = Path(args.output)
        os.makedirs(out_path.parent or ".", exist_ok=True)
        out_path.write_text(buf.getvalue(), encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
