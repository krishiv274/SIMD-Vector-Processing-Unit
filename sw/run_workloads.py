"""
run_workloads.py - runs every workload on the golden model and on the RTL.

For each workload, each lane count and each core (single-cycle and
pipelined) it:
  1. makes random input data (fixed seed, so every run is the same)
  2. assembles the program with the right constants (ITER = N / LANES, ...)
  3. runs the Python golden model
  4. checks the golden model against plain maths (e.g. C == A + B),
     which catches mistakes in the PROGRAM itself
  5. writes the hex files and runs the Verilog testbench (tb_simd_core.v),
     which compares the HARDWARE against the golden model
  6. records the cycle counts for the scalar-vs-SIMD comparison

It also runs the directed corner tests from directed_tests.py on the RTL
(they check odd cases, not speed, so they are left out of the CSV).

Usage:
    python3 sw/run_workloads.py                       # all workloads, 8 lanes, both cores
    python3 sw/run_workloads.py --lanes 1 2 4 8 --csv results/perf.csv
    python3 sw/run_workloads.py --cores pipe          # pipelined core only
    python3 sw/run_workloads.py --no-rtl              # golden model only
    python3 sw/run_workloads.py --workloads dot --lanes 8 --vcd   # save waveforms
"""

import argparse
import csv
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import directed_tests
import golden
import vasm

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "sim"
N = 256                  # elements per array in the main workloads (at most 256:
W = 16                   # the arrays sit 256 memory words apart)
CORES = {"single": False, "pipe": True}     # core name -> pipelined?
RESTARTABLE = ["vadd", "dot", "abs"]        # same result when restarted part-way through
# Cycles into the run at which reset is pressed: one early, then every cycle of
# a whole loop pass, so a store or a write is caught in every pipeline stage.
RESET_POINTS = [5] + list(range(20, 29))


LOW, HIGH = -(1 << (W - 1)), (1 << (W - 1)) - 1   # full 16-bit range: -32768 .. 32767


def rand_list(rng, count, low=LOW, high=HIGH):
    return [rng.randint(low, high) for _ in range(count)]


def wrap(x):
    """What the hardware keeps of a result: the low 16 bits, read as signed.
    For example 32767 + 1 wraps to -32768, like a car odometer."""
    return golden.to_signed(x & ((1 << W) - 1), W)


# ---- Workload definitions -----------------------------------------------------
# Each workload says: which program, where the arrays go (vector word
# addresses), how to make its inputs (n numbers per array), and what the
# correct answer is.

def setup_vadd(rng, m, n):
    a, b = rand_list(rng, n), rand_list(rng, n)
    golden.store_array(m, 0, a)
    golden.store_array(m, 256, b)
    check = lambda m: golden.load_array(m, 512, n) == [wrap(x + y) for x, y in zip(a, b)]
    return dict(A=0, B=256, C=512), n, check


def setup_saxpy(rng, m, n):
    x, y, alpha = rand_list(rng, n), rand_list(rng, n), 3
    golden.store_array(m, 0, x)
    golden.store_array(m, 256, y)
    check = lambda m: golden.load_array(m, 256, n) == [wrap(alpha * p + q) for p, q in zip(x, y)]
    return dict(X=0, Y=256, ALPHA=alpha), n, check


def setup_relu(rng, m, n):
    x = rand_list(rng, n)
    golden.store_array(m, 0, x)
    check = lambda m: golden.load_array(m, 256, n) == [max(v, 0) for v in x]
    return dict(X=0, Y=256), n, check


def setup_threshold(rng, m, n):
    x, limit = rand_list(rng, n), 100
    golden.store_array(m, 0, x)
    check = lambda m: golden.load_array(m, 256, n) == [v if v >= limit else 0 for v in x]
    return dict(X=0, Y=256, LIMIT=limit), n, check


def setup_dot(rng, m, n):
    a, b = rand_list(rng, n), rand_list(rng, n)
    golden.store_array(m, 0, a)
    golden.store_array(m, 256, b)
    # VDOT keeps the low 16 bits of each product (like VMUL), then adds them in 32 bits.
    want = sum(wrap(x * y) for x, y in zip(a, b)) & 0xFFFFFFFF
    check = lambda m: m.s[3] == want
    return dict(A=0, B=256), n, check


def setup_brighten(rng, m, n):
    x, k = rand_list(rng, n), 8000
    golden.store_array(m, 0, x)
    # Saturating: anything above 32767 stops there instead of wrapping.
    check = lambda m: golden.load_array(m, 256, n) == [min(v + k, HIGH) for v in x]
    return dict(X=0, Y=256, K=k), n, check


def setup_abs(rng, m, n):
    x = rand_list(rng, n)
    x[0], x[1] = LOW, 0          # the two edge cases: -32768 (no positive partner) and 0
    golden.store_array(m, 0, x)
    check = lambda m: golden.load_array(m, 256, n) == [min(abs(v), HIGH) for v in x]
    return dict(X=0, Y=256), n, check


def setup_sumpos(rng, m, n):
    x = rand_list(rng, n)
    golden.store_array(m, 0, x)
    want = sum(v for v in x if v > 0) & 0xFFFFFFFF
    check = lambda m: m.s[3] == want
    return dict(X=0), n, check


def setup_isa_test(rng, m, n):
    count = 32           # fixed, smaller arrays: this program writes 30 result areas
    golden.store_array(m, 0, rand_list(rng, count))
    golden.store_array(m, 32, rand_list(rng, count))
    check = lambda m: True       # no simple maths formula; the golden model is the reference
    return dict(A=0, B=32, OUT=64, STRIDE=32), count, check


WORKLOADS = {
    "vadd": setup_vadd,
    "saxpy": setup_saxpy,
    "relu": setup_relu,
    "threshold": setup_threshold,
    "dot": setup_dot,
    "brighten": setup_brighten,
    "abs": setup_abs,
    "sumpos": setup_sumpos,
    "isa_test": setup_isa_test,
}


# ---- Running ----------------------------------------------------------------------

def prepare(name, lanes, pipelined=False, n=N):
    """Golden-model run of one workload. Returns everything the RTL run needs."""
    if not 1 <= n <= N:
        raise ValueError(f"{name}: n = {n}, but arrays hold at most {N} numbers")
    rng = random.Random(f"{name}-seed")          # same data for every lane count and core
    model = golden.SimdModel(lanes, W, pipelined=pipelined)
    symbols, elements, check = WORKLOADS[name](rng, model, n)
    if elements % lanes:
        raise ValueError(f"{name}: {elements} elements do not split into {lanes} lanes")
    symbols["ITER"] = elements // lanes

    source = (ROOT / "sw" / "programs" / f"{name}.s").read_text()
    words = vasm.assemble(source, symbols)
    start_memory = golden.memory_hex_lines(model)
    instructions, cycles = model.run(words)
    if not check(model):
        raise AssertionError(f"{name} (lanes={lanes}): golden model disagrees with plain maths")
    return dict(words=words, start_memory=start_memory, model=model,
                instructions=instructions, cycles=cycles, elements=elements)


def write_files(folder, run):
    folder.mkdir(parents=True, exist_ok=True)
    vasm.write_hex(run["words"], folder / "prog.hex")
    (folder / "data.hex").write_text("\n".join(run["start_memory"]) + "\n")
    (folder / "expect_mem.hex").write_text("\n".join(golden.memory_hex_lines(run["model"])) + "\n")
    (folder / "expect_vec.hex").write_text("\n".join(golden.vector_hex_lines(run["model"])) + "\n")
    scalars = run["model"].s + [run["instructions"], run["cycles"], golden.mask_value(run["model"])]
    (folder / "expect_scalar.hex").write_text("\n".join(f"{v:08x}" for v in scalars) + "\n")


def compile_tb(lanes, pipelined=False):
    """Build the testbench once per lane count and core."""
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / f"tb_simd_core_L{lanes}_{'pipe' if pipelined else 'single'}.vvp"
    # Build into a private file, then swap it in with one rename. Another run
    # that is using the old file at that moment keeps reading the old copy, so
    # two test runs at once can never see a half-written simulator.
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    cmd = ["iverilog", "-g2012", "-I", "rtl", "-y", "rtl",
           f"-Ptb_simd_core.LANES={lanes}", f"-Ptb_simd_core.PIPELINED={int(pipelined)}",
           "-o", str(tmp), "tb/tb_simd_core.v"]
    subprocess.run(cmd, cwd=ROOT, check=True)
    tmp.replace(out)
    return out


def run_rtl(vvp, folder, vcd, extra=()):
    args = ["vvp", "-n", str(vvp), *extra,
            f"+prog={folder / 'prog.hex'}", f"+data={folder / 'data.hex'}",
            f"+expect_mem={folder / 'expect_mem.hex'}",
            f"+expect_vec={folder / 'expect_vec.hex'}",
            f"+expect_scalar={folder / 'expect_scalar.hex'}"]
    if vcd:
        args.append(f"+vcd={folder / 'wave.vcd'}")
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    perf = re.search(r"PERF lanes=\d+ cycles=(\d+) instructions=(\d+)", result.stdout)
    passed = "TEST PASSED" in result.stdout
    if not passed:
        print(result.stdout)
    return passed, (int(perf.group(1)), int(perf.group(2))) if perf else (None, None)


def main():
    parser = argparse.ArgumentParser(description="Run workloads on the golden model and RTL.")
    parser.add_argument("--lanes", type=int, nargs="+", default=[8])
    parser.add_argument("--cores", nargs="+", default=list(CORES), choices=list(CORES))
    parser.add_argument("--workloads", nargs="+", default=list(WORKLOADS), choices=list(WORKLOADS))
    parser.add_argument("--no-rtl", action="store_true", help="golden model only")
    parser.add_argument("--no-directed", action="store_true", help="skip the directed corner tests")
    parser.add_argument("--vcd", action="store_true", help="save a waveform for each run")
    parser.add_argument("--csv", help="write the results table to this file")
    args = parser.parse_args()

    rows, failures = [], 0
    for core in args.cores:
        pipelined = CORES[core]
        for lanes in args.lanes:
            vvp = None if args.no_rtl else compile_tb(lanes, pipelined)
            for name in args.workloads:
                run = prepare(name, lanes, pipelined)
                folder = BUILD / f"{name}_L{lanes}_{core}"
                write_files(folder, run)
                status = "golden only"
                # The table records what the hardware measured when the RTL ran;
                # on PASS the testbench has already checked it equals the model.
                cycles, instrs = run["cycles"], run["instructions"]
                if vvp:
                    passed, (rtl_cycles, rtl_instrs) = run_rtl(vvp, folder, args.vcd)
                    status = "PASS" if passed else "FAIL"
                    failures += not passed
                    if passed:
                        cycles, instrs = rtl_cycles, rtl_instrs
                m = run["model"]
                print(f"{name:10s} {core:6s} lanes={lanes}  instructions={instrs:6d}  "
                      f"cycles={cycles:6d}  {status}")
                rows.append(dict(workload=name, core=core, lanes=lanes, elements=run["elements"],
                                 instructions=instrs, cycles=cycles, loads=m.loads,
                                 stalls=m.stalls, mispredicts=m.mispredicts, status=status))
            if vvp and not args.no_directed:
                for name in directed_tests.DIRECTED:
                    run = directed_tests.prepare_directed(name, lanes, pipelined=pipelined)
                    folder = BUILD / f"directed_{name}_L{lanes}_{core}"
                    write_files(folder, run)
                    passed, _ = run_rtl(vvp, folder, args.vcd)
                    failures += not passed
                    print(f"{name:10s} {core:6s} lanes={lanes}  instructions={run['instructions']:6d}  "
                          f"cycles={run['cycles']:6d}  {'PASS' if passed else 'FAIL'}  (directed)")
                # Reset pressed in the middle of a run: nothing may be written while
                # it is held, and the restarted program must end exactly as before.
                # These programs give the same result when started again.
                for name in RESTARTABLE:
                    if name not in args.workloads:
                        continue
                    folder = BUILD / f"{name}_L{lanes}_{core}"
                    for at in RESET_POINTS:
                        passed, _ = run_rtl(vvp, folder, False, [f"+reset_at={at}"])
                        failures += not passed
                        print(f"{name:10s} {core:6s} lanes={lanes}  reset at cycle {at:3d}  "
                              f"{'PASS' if passed else 'FAIL'}  (reset)")

    if args.csv:
        add_speedup(rows)
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {args.csv}")

    if failures:
        sys.exit(f"{failures} run(s) FAILED")


def add_speedup(rows):
    """Speed-up = cycles with 1 lane / cycles with N lanes, on the same core
    (needs a 1-lane run of that core)."""
    base = {(r["workload"], r["core"]): r["cycles"] for r in rows if r["lanes"] == 1}
    for r in rows:
        b = base.get((r["workload"], r["core"]))
        r["speedup"] = round(b / r["cycles"], 3) if b else ""
        r["elements_per_cycle"] = round(r["elements"] / r["cycles"], 4)


if __name__ == "__main__":
    main()
