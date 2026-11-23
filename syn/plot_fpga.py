"""
plot_fpga.py - how the FPGA cost and clock speed change with the lane count,
for both cores.

Reads results/fpga_ecp5.csv (from run_syn.py) and draws two small charts
side by side in results/figures/fpga_scaling.png:
  left : logic cells used (ECP5-25F), single-cycle and pipelined side by side
  right: maximum clock frequency, median of the seeds, with the min-max range

They are two separate charts on purpose: cells and MHz are different units,
so they should not share one axis. The single-cycle core is always blue and
the pipelined core always orange, with different marker shapes too, so the
two can be told apart without colour.

Usage:  uv run --python 3.12 --with matplotlib python syn/plot_fpga.py
"""

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GREY, INK, GRID = "#8a8984", "#52514e", "#e4e3df"
CORES = [("single", "single-cycle", "#2a78d6", "o"),     # name, label, colour, marker
         ("pipe", "pipelined", "#eb6834", "s")]


def style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GREY)
    ax.spines["bottom"].set_color(GREY)
    ax.tick_params(colors=INK, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def main():
    with open(ROOT / "results" / "fpga_ecp5.csv") as f:
        rows = list(csv.DictReader(f))
    by_core = {name: [r for r in rows if r["core"] == name] for name, *_ in CORES}
    lanes = [int(r["lanes"]) for r in by_core["single"]]

    fig, (left, right) = plt.subplots(1, 2, figsize=(7.2, 3.1))

    # ---- Left: logic cells, two bars per lane count -------------------------
    width = 0.36
    for k, (name, label, colour, _) in enumerate(CORES):
        cells = [int(r["logic_cells"]) for r in by_core[name]]
        xs = [i + (k - 0.5) * (width + 0.03) for i in range(len(lanes))]   # small gap between bars
        left.bar(xs, cells, width=width, color=colour, label=label)
        # Label only the 8-lane bars: the headline numbers.
        left.annotate(f"{cells[-1]:,}", (xs[-1], cells[-1]), textcoords="offset points",
                      xytext=(0, 3), ha="center", color=INK, fontsize=8)
    left.set_xticks(range(len(lanes)), [str(n) for n in lanes])
    left.set_title("Logic cells (ECP5-25F)", color=INK, fontsize=10)
    left.set_xlabel("lanes", color=INK)
    left.legend(frameon=False, fontsize=8, labelcolor=INK, loc="upper left")
    style(left)

    # ---- Right: Fmax, one line per core ----------------------------------------
    top = 0.0
    for name, label, colour, marker in CORES:
        fmax = [float(r["fmax_mhz_median"]) for r in by_core[name]]
        low = [f - float(r["fmax_mhz_min"]) for f, r in zip(fmax, by_core[name])]
        high = [float(r["fmax_mhz_max"]) - f for f, r in zip(fmax, by_core[name])]
        right.errorbar(lanes, fmax, yerr=[low, high], marker=marker, markersize=7,
                       color=colour, linewidth=2, capsize=3, label=label)
        for n, f in ((lanes[0], fmax[0]), (lanes[-1], fmax[-1])):
            right.annotate(f"{f:.1f}", (n, f), textcoords="offset points", xytext=(7, 3),
                           color=INK, fontsize=8)
        top = max(top, max(fmax))
    right.set_title("Max clock (MHz), median of 3 seeds", color=INK, fontsize=10)
    right.set_xlabel("lanes", color=INK)
    right.set_xticks(lanes)
    right.set_ylim(0, top * 1.3)
    right.legend(frameon=False, fontsize=8, labelcolor=INK, loc="lower left")
    style(right)

    fig.tight_layout()
    out = ROOT / "results" / "figures" / "fpga_scaling.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=300)
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
