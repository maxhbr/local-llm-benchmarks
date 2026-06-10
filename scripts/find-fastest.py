#!/usr/bin/env python3
"""Extract and rank models by speed from llama-benchy.md benchmark files."""

import os
import re
import glob
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BenchmarkResult:
    model_name: str
    gpu: str
    test: str
    tps_total: Optional[float]
    tps_req: Optional[float]
    peak_tps: Optional[float]
    ttfr: Optional[float]
    est_ppt: Optional[float]


def parse_markdown_table(filepath: str):
    """Parse a llama-benchy.md markdown table into structured results."""
    results = []
    with open(filepath, "r") as f:
        lines = f.readlines()

    # Find table rows (skip header and separator)
    in_table = False
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            in_table = False
            continue

        parts = [p.strip() for p in line.split("|")]
        # parts has empty strings at start/end from leading/trailing |
        # Actual columns: parts[1]=model, parts[2]=test, parts[3]=tps_total, parts[4]=tps_req,
        #                 parts[5]=peak_tps, parts[6]=peak_tps_req, parts[7]=ttfr, parts[8]=est_ppt, parts[9]=e2e
        # Skip header rows
        if len(parts) > 1 and (parts[1] == "model" or parts[1] == "test"):
            in_table = True
            continue
        # Skip separator row
        if any(":-" in p for p in parts):
            continue

        if in_table and len(parts) >= 10:
            # Parse model name (format: gpu:model)
            model_full = parts[1]
            test = parts[2]

            def parse_metric(s: str) -> Optional[float]:
                s = s.strip()
                if not s or s == "-":
                    return None
                # "3211.28 ± 186.22" -> 3211.28
                match = re.search(r"([\d.]+)", s)
                return float(match.group(1)) if match else None

            tps_total = parse_metric(parts[3])
            tps_req = parse_metric(parts[4])
            peak_tps = parse_metric(parts[5])
            ttfr = parse_metric(parts[7])
            est_ppt = parse_metric(parts[8])

            # Extract GPU and model name
            if ":" in model_full:
                gpu, model_name = model_full.split(":", 1)
            else:
                gpu = "unknown"
                model_name = model_full

            results.append(BenchmarkResult(
                model_name=model_name,
                gpu=gpu,
                test=test,
                tps_total=tps_total,
                tps_req=tps_req,
                peak_tps=peak_tps,
                ttfr=ttfr,
                est_ppt=est_ppt,
            ))

    return results


def extract_model_size(model_name: str) -> Optional[float]:
    """Try to extract model size in billions (e.g., '9B' -> 9)."""
    match = re.search(r"(\d+(?:\.\d+)?)B", model_name)
    return float(match.group(1)) if match else None


def main():
    # Find all llama-benchy.md files
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmarks")
    pattern = os.path.join(base_dir, "**", "llama-benchy.md")
    files = glob.glob(pattern, recursive=True)

    if not files:
        print(f"No llama-benchy.md files found under {base_dir}")
        return

    print(f"Found {len(files)} benchmark files\n")

    # Parse all files
    all_results = []
    for filepath in sorted(files):
        results = parse_markdown_table(filepath)
        all_results.extend(results)

    # Deduplicate by (model_name, gpu, test) keeping the one with non-null tps_total
    seen = {}
    for r in all_results:
        key = (r.model_name, r.gpu, r.test)
        if key not in seen or (r.tps_total and not seen[key].tps_total):
            seen[key] = r

    all_results = list(seen.values())

    # Key metrics to show:
    # 1. tg128 (c1) - single-request generation speed (the main "fast" metric)
    # 2. pp2048 (c1) - single-request prefill speed
    # 3. tg128 (c1) at longest context - generation under memory pressure

    print("=" * 120)
    print("FASTEST MODELS BY SINGLE-REQUEST GENERATION SPEED (tg128 c1)")
    print("=" * 120)

    tg128_c1 = [r for r in all_results if "tg128" in r.test and "(c1)" in r.test and "@ d" not in r.test and r.tps_total]
    tg128_c1.sort(key=lambda r: r.tps_total, reverse=True)

    print(f"\n{'Rank':<5} {'Model':<45} {'GPU':<10} {'t/s (total)':<15} {'t/s (req)':<15} {'Peak t/s':<15} {'Size':<8}")
    print("-" * 120)
    for i, r in enumerate(tg128_c1, 1):
        size = extract_model_size(r.model_name)
        size_str = f"{size}B" if size else "N/A"
        print(f"{i:<5} {r.model_name:<45} {r.gpu:<10} {r.tps_total:<15.2f} {r.tps_req:<15.2f} {r.peak_tps if r.peak_tps else 0:<15.2f} {size_str:<8}")

    print("\n" + "=" * 120)
    print("FASTEST MODELS BY PREFILL SPEED (pp2048 c1)")
    print("=" * 120)

    pp2048_c1 = [r for r in all_results if "pp2048" in r.test and "(c1)" in r.test and "@ d" not in r.test and r.tps_total]
    pp2048_c1.sort(key=lambda r: r.tps_total, reverse=True)

    print(f"\n{'Rank':<5} {'Model':<45} {'GPU':<10} {'t/s (total)':<15} {'TTFR (ms)':<15} {'est_ppt (ms)':<15} {'Size':<8}")
    print("-" * 120)
    for i, r in enumerate(pp2048_c1, 1):
        size = extract_model_size(r.model_name)
        size_str = f"{size}B" if size else "N/A"
        print(f"{i:<5} {r.model_name:<45} {r.gpu:<10} {r.tps_total:<15.2f} {r.ttfr if r.ttfr else 0:<15.2f} {r.est_ppt if r.est_ppt else 0:<15.2f} {size_str:<8}")

    # Per-GPU comparison
    print("\n" + "=" * 120)
    print("TOP MODELS PER GPU (tg128 c1)")
    print("=" * 120)

    gpus = sorted(set(r.gpu for r in tg128_c1))
    for gpu in gpus:
        gpu_results = [r for r in tg128_c1 if r.gpu == gpu]
        gpu_results.sort(key=lambda r: r.tps_total, reverse=True)
        print(f"\n--- {gpu} ({len(gpu_results)} models benchmarked) ---")
        for i, r in enumerate(gpu_results[:10], 1):
            size = extract_model_size(r.model_name)
            size_str = f"{size}B" if size else "N/A"
            print(f"  {i:<3}. {r.model_name:<45} {r.tps_total:>8.2f} t/s  ({size_str})")

    # Per-size comparison
    print("\n" + "=" * 120)
    print("TOP MODELS PER SIZE CLASS (tg128 c1)")
    print("=" * 120)

    sized = [(r, extract_model_size(r.model_name)) for r in tg128_c1 if extract_model_size(r.model_name)]
    size_classes = {}
    for r, size in sized:
        # Group into buckets: <3B, 3-7B, 7-15B, 15-35B, 35-70B, 70-130B, >130B
        if size < 3:
            bucket = "<3B"
        elif size < 7:
            bucket = "3-7B"
        elif size < 15:
            bucket = "7-15B"
        elif size < 35:
            bucket = "15-35B"
        elif size < 70:
            bucket = "35-70B"
        elif size < 130:
            bucket = "70-130B"
        else:
            bucket = ">130B"
        size_classes.setdefault(bucket, []).append((r, size))

    bucket_order = ["<3B", "3-7B", "7-15B", "15-35B", "35-70B", "70-130B", ">130B"]
    for bucket in bucket_order:
        entries = size_classes.get(bucket, [])
        if not entries:
            continue
        entries.sort(key=lambda x: x[0].tps_total, reverse=True)
        print(f"\n--- {bucket} ---")
        for r, size in entries[:10]:
            print(f"  {r.model_name:<45} {r.tps_total:>8.2f} t/s  ({size}B, {r.gpu})")


if __name__ == "__main__":
    main()
