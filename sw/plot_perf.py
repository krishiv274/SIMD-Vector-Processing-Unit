"""
plot_perf.py - figures for the scalar vs SIMD and single-cycle vs pipelined
comparison.

Reads results/perf.csv (run_workloads.py), results/sweep.csv (sweep.py) and
results/fpga_ecp5.csv (syn/run_syn.py) and draws:

  results/figures/cycles.png           clock cycles per workload, 1/2/4/8 lanes, one panel per core
  results/figures/speedup.png          speed-up vs lanes: left in clock cycles (over 1 lane of the
                                       same core), right in real time (over the Phase 1 scalar design)
  results/figures/cycle_breakdown.png  where the clock cycles go at 8 lanes, one panel per core
  results/figures/problem_size.png     speed-up of 8 lanes over 1 lane against the array size N
  results/figures/unrolling.png        time per element: normal loops vs loops unrolled 4 times

Real time = clock cycles / Fmax, with each design's own Fmax (median of the
ECP5 place-and-route seeds). The single-cycle core is always blue and the
pipelined core always orange, with different marker shapes, in every figure.

Usage:  uv run --python 3.12 --with matplotlib python sw/plot_perf.py
"""

import csv
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
FIGURES = RESULTS / "figures"

# Colours from a colour-blind-safe palette (checked with a CVD validator).
BLUE, ORANGE, GREY, INK, GRID = "#2a78d6", "#eb6834", "#8a8984", "#52514e", "#e4e3df"
CORES = [("single", "single-cycle", BLUE, "o"), ("pipe", "pipelined", ORANGE, "s")]
LANE_SHADES = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]   # light -> dark = few -> many lanes
# Kinds of extra cycle, for the breakdown figure (base instructions are grey).
EXTRA = [("loads", "load waits (single-cycle)", "#1baf7a"),
         ("stalls", "stalls (pipelined)", "#eda100"),
         ("guesses", "wrong branch guesses + fill (pipelined)", "#e87ba4")]

# The benchmarks (isa_test is a coverage test; the unrolled programs have their own figure).
BENCHMARKS = ["vadd", "saxpy", "relu", "threshold", "dot", "brighten", "abs", "sumpos"]
PHASE1 = ["vadd", "saxpy", "relu", "threshold", "dot"]


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GREY)
    ax.spines["bottom"].set_color(GREY)
    ax.tick_params(colors=INK, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def save(fig, name):
    fig.tight_layout()
    fig.savefig(FIGURES / name, dpi=300)
    plt.close(fig)


def find(rows, **want):
    """The one row whose columns match every given value."""
    hits = [r for r in rows if all(str(r[k]) == str(v) for k, v in want.items())]
    if len(hits) != 1:
        raise KeyError(f"{len(hits)} rows match {want}")
    return hits[0]


def seconds(row, fmax):
    """Run time of one row: cycles divided by that design's clock speed."""
    return int(row["cycles"]) / (fmax[(row["core"], int(row["lanes"]))] * 1e6)


def plot_cycles(perf):
    lanes_list = sorted({int(r["lanes"]) for r in perf})
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.2), sharey=True)
    width = 0.8 / len(lanes_list)
    for ax, (core, label, _, _) in zip(axes, CORES):
        for j, lanes in enumerate(lanes_list):
            values = [int(find(perf, workload=w, core=core, lanes=lanes)["cycles"]) for w in PHASE1]
            xs = [i + (j - (len(lanes_list) - 1) / 2) * width for i in range(len(PHASE1))]
            ax.bar(xs, values, width * 0.9, color=LANE_SHADES[j],
                   label=f"{lanes} lane{'s' if lanes > 1 else ''}")
        ax.set_xticks(range(len(PHASE1)), PHASE1)
        ax.set_title(f"{label} core", color=INK, fontsize=10)
        style(ax)
    axes[0].set_ylabel("clock cycles (N = 256)", color=INK)
    axes[1].legend(frameon=False, fontsize=8, labelcolor=INK)
    save(fig, "cycles.png")


def plot_speedup(perf, fmax):
    lanes_list = sorted({int(r["lanes"]) for r in perf})
    fig, (left, right) = plt.subplots(1, 2, figsize=(7.2, 3.3))

    # Left: speed-up in clock cycles over 1 lane of the SAME core.
    left.plot(lanes_list, lanes_list, linestyle="--", color=GREY, linewidth=1.2)
    left.annotate("ideal", (5, 5), textcoords="offset points", xytext=(-6, 6),
                  ha="right", color=INK, fontsize=8)
    for core, label, colour, marker in CORES:
        mean = [statistics.mean(float(find(perf, workload=w, core=core, lanes=n)["speedup"])
                                for w in BENCHMARKS) for n in lanes_list]
        left.plot(lanes_list, mean, marker=marker, markersize=6, color=colour, linewidth=2, label=label)
        left.annotate(f"{mean[-1]:.2f}x", (lanes_list[-1], mean[-1]), textcoords="offset points",
                      xytext=(6, -10 if core == "pipe" else 2), color=INK, fontsize=8)
    left.set_title("In clock cycles, over 1 lane", color=INK, fontsize=10)

    # Right: speed-up in real time over the single-cycle core with 1 lane (the scalar baseline).
    for core, label, colour, marker in CORES:
        mean = [statistics.mean(seconds(find(perf, workload=w, core="single", lanes=1), fmax)
                                / seconds(find(perf, workload=w, core=core, lanes=n), fmax)
                                for w in BENCHMARKS) for n in lanes_list]
        right.plot(lanes_list, mean, marker=marker, markersize=6, color=colour, linewidth=2, label=label)
        right.annotate(f"{mean[-1]:.2f}x", (lanes_list[-1], mean[-1]), textcoords="offset points",
                       xytext=(6, -3), color=INK, fontsize=8)
    right.set_title("In real time, over single-cycle 1 lane", color=INK, fontsize=10)

    for ax in (left, right):
        ax.set_xticks(lanes_list)
        ax.set_xlabel("lanes", color=INK)
        ax.set_xlim(0.5, lanes_list[-1] + 1.6)
        ax.set_ylim(0, None)
        style(ax)
    left.set_ylabel("speed-up (mean of 8 workloads)", color=INK)
    left.legend(frameon=False, fontsize=8, labelcolor=INK, loc="upper left")
    save(fig, "speedup.png")


def plot_breakdown(perf):
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.3), sharey=True)
    for ax, (core, label, _, _) in zip(axes, CORES):
        rows = [find(perf, workload=w, core=core, lanes=8) for w in BENCHMARKS]
        base = [int(r["instructions"]) for r in rows]
        parts = {"loads": [int(r["loads"]) for r in rows],
                 "stalls": [int(r["stalls"]) for r in rows],
                 # every other extra cycle: wrong guesses plus the one fill cycle
                 "guesses": [int(r["cycles"]) - int(r["instructions"]) - int(r["loads"]) - int(r["stalls"])
                             for r in rows]}
        xs = range(len(BENCHMARKS))
        ax.bar(xs, base, 0.62, color=GREY, label="instructions", edgecolor="white", linewidth=1)
        bottom = base[:]
        for key, name, colour in EXTRA:
            if any(parts[key]):
                ax.bar(xs, parts[key], 0.62, bottom=bottom, color=colour, label=name,
                       edgecolor="white", linewidth=1)
                bottom = [b + p for b, p in zip(bottom, parts[key])]
        ax.set_xticks(xs, BENCHMARKS, rotation=35, ha="right")
        ax.set_title(f"{label} core, 8 lanes", color=INK, fontsize=10)
        style(ax)
    axes[0].set_ylabel("clock cycles (N = 256)", color=INK)
    # One legend for both panels, above them, so it never covers a bar.
    handles = [plt.Rectangle((0, 0), 1, 1, color=GREY)] + \
              [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in EXTRA]
    names = ["instructions"] + [n for _, n, _ in EXTRA]
    fig.legend(handles, names, loc="upper center", ncol=2, frameon=False, fontsize=8,
               labelcolor=INK, bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    fig.savefig(FIGURES / "cycle_breakdown.png", dpi=300)
    plt.close(fig)


def plot_problem_size(sweep):
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    sizes = sorted({int(r["n"]) for r in sweep})
    ax.axhline(8, linestyle="--", color=GREY, linewidth=1.2)
    ax.annotate("ideal (8 lanes)", (sizes[0], 8), textcoords="offset points", xytext=(0, 4),
                color=INK, fontsize=8)
    for core, label, colour, marker in CORES:
        for workload, dash in (("vadd", "-"), ("dot", ":")):
            speed = [int(find(sweep, workload=workload, core=core, lanes=1, n=n)["cycles"])
                     / int(find(sweep, workload=workload, core=core, lanes=8, n=n)["cycles"]) for n in sizes]
            ax.plot(sizes, speed, linestyle=dash, marker=marker, markersize=5, color=colour,
                    linewidth=2, label=f"{workload}, {label}")
    ax.set_xscale("log", base=2)
    ax.set_xticks(sizes, [str(n) for n in sizes])
    ax.set_xlabel("numbers per array, N", color=INK)
    ax.set_ylabel("speed-up of 8 lanes over 1 (cycles)", color=INK)
    ax.set_ylim(0, 9)
    style(ax)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK, loc="lower right")
    save(fig, "problem_size.png")


def plot_unrolling(perf, fmax):
    programs = ["vadd", "vadd_unroll", "dot", "dot_unroll"]
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    width = 0.36
    for k, (core, label, colour, _) in enumerate(CORES):
        rows = [find(perf, workload=p, core=core, lanes=8) for p in programs]
        ns = [seconds(r, fmax) * 1e9 / int(r["elements"]) for r in rows]
        xs = [i + (k - 0.5) * (width + 0.03) for i in range(len(programs))]
        ax.bar(xs, ns, width, color=colour, label=label)
        for x, v in zip(xs, ns):
            ax.annotate(f"{v:.1f}", (x, v), textcoords="offset points", xytext=(0, 2),
                        ha="center", color=INK, fontsize=7)
    ax.set_xticks(range(len(programs)), programs)
    ax.set_ylabel("ns per element (8 lanes, N = 256)", color=INK)
    style(ax)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK)
    save(fig, "unrolling.png")


def main():
    FIGURES.mkdir(parents=True, exist_ok=True)
    perf = read_csv(RESULTS / "perf.csv")
    sweep = read_csv(RESULTS / "sweep.csv")
    fmax = {(r["core"], int(r["lanes"])): float(r["fmax_mhz_median"])
            for r in read_csv(RESULTS / "fpga_ecp5.csv")}
    plot_cycles(perf)
    plot_speedup(perf, fmax)
    plot_breakdown(perf)
    plot_problem_size(sweep)
    plot_unrolling(perf, fmax)
    print("wrote cycles, speedup, cycle_breakdown, problem_size and unrolling .png in results/figures/")


if __name__ == "__main__":
    main()
