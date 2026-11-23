"""
sweep.py - how the speed-up depends on the problem size.

Phase 1 measured one size: N = 256 numbers per array. Real programs are
often smaller, and then the fixed part of a program (setting up the loop,
the final HALT, filling the pipeline) is a larger share of the work, so the
speed-up from more lanes is smaller. This is Amdahl's law.

For every workload below, both cores, 1, 2, 4 and 8 lanes, and
N = 8, 16, 32, 64, 128 and 256, this script runs the program on the Verilog
core (the same testbench as run_workloads.py, checked against the golden
model every time) and records the measured cycle counts in results/sweep.csv.
The unrolled programs need N / LANES to be a multiple of 4, so sizes that do
not allow that are left out for them.

Usage:  python3 sw/sweep.py                      # about a minute
        python3 sw/sweep.py --no-rtl             # golden model only (no Verilog tools)
"""

import argparse
import csv
import sys
from pathlib import Path

import run_workloads

ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = ["vadd", "dot", "vadd_unroll", "dot_unroll"]
SIZES = [8, 16, 32, 64, 128, 256]
LANES = [1, 2, 4, 8]


def fits(name, n, lanes):
    """Can this program process n numbers with this many lanes?"""
    passes = n // lanes
    if n % lanes or passes == 0:
        return False
    return passes % 4 == 0 if name.endswith("_unroll") else True


def main():
    parser = argparse.ArgumentParser(description="Problem-size sweep on both cores.")
    parser.add_argument("--no-rtl", action="store_true", help="golden model only")
    parser.add_argument("--csv", default=str(ROOT / "results" / "sweep.csv"))
    args = parser.parse_args()

    rows, failures = [], 0
    for core, pipelined in run_workloads.CORES.items():
        for lanes in LANES:
            vvp = None if args.no_rtl else run_workloads.compile_tb(lanes, pipelined)
            for name in WORKLOADS:
                for n in SIZES:
                    if not fits(name, n, lanes):
                        continue
                    run = run_workloads.prepare(name, lanes, pipelined, n=n)
                    cycles, status = run["cycles"], "golden only"
                    if vvp:
                        folder = run_workloads.BUILD / f"sweep_{name}_{core}_L{lanes}_N{n}"
                        run_workloads.write_files(folder, run)
                        passed, (rtl_cycles, _) = run_workloads.run_rtl(vvp, folder, False)
                        status = "PASS" if passed else "FAIL"
                        failures += not passed
                        cycles = rtl_cycles if passed else cycles
                    m = run["model"]
                    rows.append(dict(workload=name, core=core, lanes=lanes, n=n,
                                     instructions=run["instructions"], cycles=cycles,
                                     stalls=m.stalls, mispredicts=m.mispredicts, status=status))
            print(f"sweep {core:6s} lanes={lanes}: done, {failures} failures so far")

    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {Path(args.csv).resolve().relative_to(ROOT)} ({len(rows)} runs)")
    if failures:
        sys.exit(f"{failures} run(s) FAILED")


if __name__ == "__main__":
    main()
