"""
run_syn.py - builds the SIMD unit for real FPGAs and records size and speed.

For each core (single-cycle and pipelined) and each lane count (1, 2, 4, 8) it:
  1. loads isa_test, the program that runs every instruction, into the FPGA
     memories (a program that skipped some instructions would let synthesis
     remove their logic and make the design look smaller and faster), and
     simulates the board version (fpga_top, tb/tb_fpga_top.v) to prove its
     registers and every memory word match the golden model before anything
     is built
  2. Lattice ECP5-25F:  Yosys synthesis + nextpnr place-and-route
       -> logic cells, flip-flops, multipliers, block RAM and the
          maximum clock frequency (Fmax) from the timing analysis.
          Place-and-route uses a random starting point, so we run 3 seeds
          and keep the median. Every build is given the same 100 MHz target
          (more than any of them reaches); the result does not depend on it.
          It also records where the slowest path starts and ends
          ("critical_path"), which shows which stage limits the clock.
  3. Xilinx Artix-7 (xc7a35t, the Basys-3 board): Yosys synthesis only
       -> LUTs, flip-flops, DSP blocks and block RAM (an estimate; the
          exact numbers and Fmax come from Vivado, see syn/vivado/). The
          design is flattened first, as synth_ecp5 does, so each cell is
          counted once.

Outputs: results/fpga_ecp5.csv and results/fpga_artix7.csv (one row per
core and lane count)

Needs: yosys, and nextpnr-ecp5 (or "pip install yowasp-nextpnr-ecp5").
Usage:  python3 syn/run_syn.py [--lanes 1 2 4 8] [--cores single pipe] [--seeds 3]
        python3 syn/run_syn.py --board-only      # just the board simulation (used by make test;
                                                 # also makes the memory files Vivado needs)
"""

import argparse
import csv
import json
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sw"))
import golden        # noqa: E402
import run_workloads  # noqa: E402
import vasm           # noqa: E402

BUILD = ROOT / "build" / "syn"
RTL = sorted(str(p) for p in (ROOT / "rtl").glob("*.v"))
FPGA_PROGRAM = "isa_test"    # uses every instruction, so nothing can be optimised away
FPGA_DMEM_WORDS = 1024       # fpga_top.v data memory depth (DADDR_W = 10)
RESULT_REG = 7               # isa_test keeps its running dot product in s7
TARGET_MHZ = 100             # what every build asks nextpnr for (none reaches it)
CORES = {"single": 0, "pipe": 1}    # core name -> fpga_top PIPELINED value

# Names inside the placed design, and the part of the processor they belong
# to. Used to say where the slowest path starts and ends.
BLOCKS = [("imem", "instruction RAM"), ("dmem", "data RAM"),
          ("u_vregs", "vector registers"), ("u_sregs", "scalar registers"),
          (".x_", "X stage register"), (".w_", "W stage register"),
          (".d_pc", "fetch address"), ("_count", "counters")]


def find_nextpnr():
    for name in ("nextpnr-ecp5", "yowasp-nextpnr-ecp5"):
        if shutil.which(name):
            return name
    sys.exit("nextpnr-ecp5 not found: install it with  pip install yowasp-nextpnr-ecp5")


def prepare_memories(folder, lanes, core="pipe"):
    """Fill the FPGA memories with isa_test and its data, then simulate the
    board version to check it gives the golden model's answer."""
    folder.mkdir(parents=True, exist_ok=True)
    run = run_workloads.prepare(FPGA_PROGRAM, lanes, pipelined=bool(CORES[core]))
    memory = run["start_memory"]
    if len(memory) != FPGA_DMEM_WORDS:            # the model and the FPGA must have the same memory
        sys.exit(f"golden model has {len(memory)} memory words, the FPGA has {FPGA_DMEM_WORDS}")
    vasm.write_hex(run["words"], folder / "program.hex")
    (folder / "data.hex").write_text("\n".join(memory) + "\n")
    (folder / "expect_mem.hex").write_text("\n".join(golden.memory_hex_lines(run["model"])) + "\n")
    simulate_board(folder, lanes, core, expected=run["model"].s[RESULT_REG])


def simulate_board(folder, lanes, core, expected):
    """Run tb_fpga_top on the exact memories the FPGA will hold."""
    folder = Path(folder).resolve()
    vvp = folder / "tb_fpga_top.vvp"
    subprocess.run(["iverilog", "-g2012", "-I", str(ROOT / "rtl"), "-y", str(ROOT / "rtl"),
                    f"-Ptb_fpga_top.LANES={lanes}", f"-Ptb_fpga_top.PIPELINED={CORES[core]}",
                    "-o", str(vvp), str(ROOT / "tb" / "tb_fpga_top.v")],
                   check=True)
    result = subprocess.run(["vvp", "-n", str(vvp), f"+expect={expected:08x}", f"+sel={RESULT_REG}",
                             "+expect_mem=expect_mem.hex"],
                            cwd=folder, capture_output=True, text=True)
    if "TEST PASSED" not in result.stdout:
        sys.exit(f"{core} lanes={lanes}: board simulation failed\n{result.stdout}{result.stderr}")
    print(f"board   {core:6s} lanes={lanes}: s{RESULT_REG} = {expected:#010x} on the LEDs and all "
          f"{FPGA_DMEM_WORDS} memory words match, simulation passed")


def yosys(folder, lanes, core, synth_cmd, json_out=None):
    """Run Yosys and return the cell counts from its 'stat' report."""
    write = f"write_json {json_out}; " if json_out else ""
    script = (f"read_verilog -I {ROOT / 'rtl'} {' '.join(RTL)}; "
              f"chparam -set LANES {lanes} -set PIPELINED {CORES[core]} fpga_top; "
              f"{synth_cmd}; {write}tee -o stat.txt stat")
    subprocess.run(["yosys", "-q", "-p", script], cwd=folder, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cells = {}
    for line in (folder / "stat.txt").read_text().splitlines():
        m = re.match(r"\s+(\d+)\s+(\S+)$", line)
        if m:
            cells[m.group(2)] = cells.get(m.group(2), 0) + int(m.group(1))
    return cells


def block_of(cell):
    """Which part of the processor a placed cell belongs to."""
    for key, name in BLOCKS:
        if key in cell:
            return name
    return cell.split(".")[0]


def critical_path(report):
    """'where it starts -> where it ends' for the slowest path in a report."""
    path = report["critical_paths"][0]["path"]
    start, end = path[0]["from"]["cell"], path[-1]["to"]["cell"]
    return f"{block_of(start)} -> {block_of(end)}"


def ecp5(lanes, core, seeds, nextpnr):
    folder = BUILD / f"ecp5_{core}_L{lanes}"
    prepare_memories(folder, lanes, core)
    yosys(folder, lanes, core, "synth_ecp5 -top fpga_top", json_out="fpga.json")

    runs, util = [], {}
    for seed in range(1, seeds + 1):
        report = folder / f"report_seed{seed}.json"
        subprocess.run([nextpnr, "--25k", "--package", "CABGA256", "--speed", "6",
                        "--json", "fpga.json", "--lpf-allow-unconstrained",
                        "--freq", str(TARGET_MHZ), "--timing-allow-fail", "--seed", str(seed),
                        "--report", report.name, "-q"],
                       cwd=folder, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = json.loads(report.read_text())
        runs.append((min(c["achieved"] for c in data["fmax"].values()), critical_path(data)))
        util = data["utilization"]

    fmax = [f for f, _ in runs]
    median_run = sorted(runs)[len(runs) // 2]              # the seed with the median Fmax
    used = lambda k: util.get(k, {}).get("used", 0)
    return dict(core=core, lanes=lanes,
                logic_cells=used("TRELLIS_COMB"), logic_cells_total=util["TRELLIS_COMB"]["available"],
                flip_flops=used("TRELLIS_FF"),
                lutram_blocks=used("TRELLIS_RAMW"),
                multipliers=used("MULT18X18D"), multipliers_total=util["MULT18X18D"]["available"],
                block_ram=used("DP16KD"), block_ram_total=util["DP16KD"]["available"],
                fmax_mhz_median=round(statistics.median(fmax), 2),
                fmax_mhz_min=round(min(fmax), 2), fmax_mhz_max=round(max(fmax), 2),
                critical_path=median_run[1])


def artix7(lanes, core):
    folder = BUILD / f"artix7_{core}_L{lanes}"
    prepare_memories(folder, lanes, core)
    c = yosys(folder, lanes, core, "synth_xilinx -family xc7 -top fpga_top -flatten")
    count = lambda prefix: sum(v for k, v in c.items() if k.startswith(prefix))
    return dict(core=core, lanes=lanes,
                luts=count("LUT"), luts_total=20800,
                lutram=count("RAM32") + count("RAM64") + count("RAM128"),
                flip_flops=count("FD"), flip_flops_total=41600,
                dsp48=count("DSP48"), dsp48_total=90,
                bram36_equiv=count("RAMB36") + count("RAMB18") / 2, bram36_total=50,
                carry4=count("CARRY4"))


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description="FPGA resource and Fmax sweep.")
    parser.add_argument("--lanes", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--cores", nargs="+", default=list(CORES), choices=list(CORES))
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--board-only", action="store_true",
                        help="only simulate the board version (no synthesis tools needed)")
    args = parser.parse_args()
    if args.board_only:
        for core in args.cores:
            for lanes in args.lanes:
                # The pipelined core is the default board build, so Vivado finds its
                # memory files in board_L<lanes>; the single-cycle ones go next to them.
                name = f"board_L{lanes}" if core == "pipe" else f"board_single_L{lanes}"
                prepare_memories(BUILD / name, lanes, core)
        return
    nextpnr = find_nextpnr()

    ecp5_rows, artix_rows = [], []
    for core in args.cores:
        for lanes in args.lanes:
            row = ecp5(lanes, core, args.seeds, nextpnr)
            ecp5_rows.append(row)
            print(f"ECP5    {core:6s} lanes={lanes}: {row['logic_cells']} cells, "
                  f"{row['multipliers']} mult, Fmax {row['fmax_mhz_median']} MHz "
                  f"({row['critical_path']})")
            row = artix7(lanes, core)
            artix_rows.append(row)
            print(f"Artix-7 {core:6s} lanes={lanes}: {row['luts']} LUTs, {row['flip_flops']} FFs, "
                  f"{row['dsp48']} DSP")

    write_csv(ROOT / "results" / "fpga_ecp5.csv", ecp5_rows)
    write_csv(ROOT / "results" / "fpga_artix7.csv", artix_rows)


if __name__ == "__main__":
    main()
