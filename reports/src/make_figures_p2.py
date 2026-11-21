"""make_figures_p2.py - result figures for the Phase 2 and combined reports.

Reads the measured results (results/perf.csv, results/sweep.csv,
results/fpga_ecp5.csv) and writes two vector PDFs into reports/src/figures/:

    p2_fpga.pdf    both cores against lane count: logic cells, Fmax, and
                   real-time speed-up over the single-cycle 1-lane core
    p2_cycles.pdf  where the cycles go at 8 lanes, speed-up against problem
                   size, and the effect of unrolling

The single-cycle core is blue with circles and the pipelined core orange with
squares in every panel. (The Phase 1 figures, perf.pdf and fpga.pdf, came from
make_figures.py and the Phase 1 results, and are kept as they were.)

Run from the repository root (make report does this for you):
    uv run --python 3.12 --with matplotlib python reports/src/make_figures_p2.py
"""
import csv
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "figures"

LANES = [1, 2, 4, 8]
BENCHMARKS = ["vadd", "saxpy", "relu", "threshold", "dot", "brighten", "abs", "sumpos"]
BLUE, ORANGE, GREY = "#3f7fdc", "#ef6f3c", "#555555"
CORES = [("single", "single-cycle", BLUE, "o"), ("pipe", "pipelined", ORANGE, "s")]
EXTRA = [("loads", "load waits", "#1baf7a"), ("stalls", "stalls", "#eda100"),
         ("other", "wrong guesses + fill", "#e87ba4")]
FULL_WIDTH, HEIGHT = 7.16, 2.0          # inches: IEEE two-column text width


def style():
    plt.rcParams.update({
        "font.family": "serif", "font.size": 8,
        "font.serif": ["Times New Roman", "Times", "Nimbus Roman", "STIXGeneral"],
        "mathtext.fontset": "stix", "pdf.fonttype": 42,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": GREY, "axes.linewidth": 0.6,
        "axes.grid": True, "grid.color": "#e4e4e4", "grid.linewidth": 0.5,
        "axes.axisbelow": True, "legend.frameon": False, "legend.fontsize": 7,
        "xtick.color": GREY, "ytick.color": GREY,
        "xtick.major.width": 0.6, "ytick.major.width": 0.6,
    })


def read(name):
    with open(ROOT / "results" / name) as f:
        return list(csv.DictReader(f))


def find(rows, **want):
    hits = [r for r in rows if all(str(r[k]) == str(v) for k, v in want.items())]
    assert len(hits) == 1, want
    return hits[0]


def lane_axis(ax):
    ax.set_xscale("log", base=2)
    ax.set_xticks(LANES, [str(n) for n in LANES])
    ax.minorticks_off()
    ax.set_xlabel("lanes $L$")


def fpga_figure(perf, ecp5):
    fmax = {(r["core"], int(r["lanes"])): float(r["fmax_mhz_median"]) for r in ecp5}
    secs = lambda r: int(r["cycles"]) / fmax[(r["core"], int(r["lanes"]))]
    fig, (a, b, c) = plt.subplots(1, 3, figsize=(FULL_WIDTH, HEIGHT))
    for core, label, colour, marker in CORES:
        rows = [find(ecp5, core=core, lanes=n) for n in LANES]
        cells = [int(r["logic_cells"]) for r in rows]
        a.plot(LANES, cells, marker + "-", color=colour, ms=4, lw=1.2, label=label)
        a.annotate(f"{cells[-1]:,}", (8, cells[-1]), xytext=(-6, 4 if core == "pipe" else -10),
                   textcoords="offset points", ha="right", fontsize=6.5, color=colour)
        med = [float(r["fmax_mhz_median"]) for r in rows]
        low = [m - float(r["fmax_mhz_min"]) for m, r in zip(med, rows)]
        high = [float(r["fmax_mhz_max"]) - m for m, r in zip(med, rows)]
        b.errorbar(LANES, med, yerr=[low, high], fmt=marker + "-", color=colour, ms=4, lw=1.2,
                   capsize=2.5, label=label)
        for n, v in zip(LANES, med):
            b.annotate(f"{v:.1f}", (n, v), xytext=(4, 3), textcoords="offset points", fontsize=6.5,
                       color=colour)
        rt = [statistics.mean(secs(find(perf, workload=w, core="single", lanes=1))
                              / secs(find(perf, workload=w, core=core, lanes=n)) for w in BENCHMARKS)
              for n in LANES]
        c.plot(LANES, rt, marker + "-", color=colour, ms=4, lw=1.2, label=label)
        c.annotate(f"{rt[-1]:.1f}$\\times$", (8, rt[-1]), xytext=(-6, 4 if core == "pipe" else -11),
                   textcoords="offset points", ha="right", color=colour)
    for ax in (a, b, c):
        lane_axis(ax)
    a.set_ylabel("ECP5 logic cells"); a.set_ylim(0, 10000); a.legend(loc="upper left")
    b.set_ylabel("$F_{\\max}$ on ECP5-25F (MHz)"); b.set_ylim(0, 75); b.legend(loc="lower left")
    c.plot(LANES, LANES, "--", color="#9a9a9a", lw=0.8, label="$L\\times$")
    c.set_ylabel("real-time speed-up"); c.set_ylim(0, 12.5); c.legend(loc="upper left")
    fig.tight_layout(w_pad=1.5)
    fig.savefig(OUT / "p2_fpga.pdf")
    plt.close(fig)


def cycles_figure(perf, sweep, ecp5):
    fmax = {(r["core"], int(r["lanes"])): float(r["fmax_mhz_median"]) for r in ecp5}
    fig, (a, b, c) = plt.subplots(1, 3, figsize=(FULL_WIDTH, HEIGHT + 0.25),
                                  gridspec_kw={"width_ratios": [1.5, 1, 1]})
    # (a) cycle breakdown at 8 lanes: two thin bars per workload
    width = 0.38
    for k, (core, label, colour, _) in enumerate(CORES):
        rows = [find(perf, workload=w, core=core, lanes=8) for w in BENCHMARKS]
        xs = [i + (k - 0.5) * (width + 0.04) for i in range(len(BENCHMARKS))]
        base = [int(r["instructions"]) for r in rows]
        a.bar(xs, base, width, color="#c9c8c4")
        parts = {"loads": [int(r["loads"]) for r in rows], "stalls": [int(r["stalls"]) for r in rows]}
        parts["other"] = [int(r["cycles"]) - b0 - parts["loads"][i] - parts["stalls"][i]
                          for i, (r, b0) in enumerate(zip(rows, base))]
        bottom = base[:]
        for key, _, col in EXTRA:
            a.bar(xs, parts[key], width, bottom=bottom, color=col, edgecolor="white", linewidth=0.4)
            bottom = [x + y for x, y in zip(bottom, parts[key])]
    a.set_xticks(range(len(BENCHMARKS)), BENCHMARKS, rotation=30, ha="right")
    a.grid(axis="x", visible=False)
    a.set_ylabel("cycles, 8 lanes, $N = 256$")
    a.set_ylim(0, 420)
    a.set_title("left bar: single-cycle, right bar: pipelined", fontsize=7, color=GREY)
    handles = [plt.Rectangle((0, 0), 1, 1, color="#c9c8c4")] + \
              [plt.Rectangle((0, 0), 1, 1, color=col) for _, _, col in EXTRA]
    names = ["instructions"] + [n for _, n, _ in EXTRA]
    a.legend(handles, names, loc="upper left", ncol=2, fontsize=6, columnspacing=0.8, handlelength=1)
    # (b) speed-up of 8 lanes over 1 against N
    sizes = sorted({int(r["n"]) for r in sweep})
    for core, label, colour, marker in CORES:
        for w, ls in (("vadd", "-"), ("dot", ":")):
            sp = [int(find(sweep, workload=w, core=core, lanes=1, n=n)["cycles"])
                  / int(find(sweep, workload=w, core=core, lanes=8, n=n)["cycles"]) for n in sizes]
            b.plot(sizes, sp, marker + ls, color=colour, ms=3, lw=1.1, label=f"{w}, {label}")
    b.axhline(8, color="#9a9a9a", lw=0.8, ls="--")
    b.set_xscale("log", base=2)
    b.set_xticks(sizes, [str(n) for n in sizes])
    b.minorticks_off()
    b.set_xlabel("elements $N$")
    b.set_ylabel("speed-up, 8 lanes over 1")
    b.set_ylim(0, 8.8)
    b.legend(loc="lower right", fontsize=6)
    # (c) unrolling: ns per element at 8 lanes
    progs = ["vadd", "vadd_unroll", "dot", "dot_unroll"]
    for k, (core, label, colour, _) in enumerate(CORES):
        rows = [find(perf, workload=p, core=core, lanes=8) for p in progs]
        ns = [int(r["cycles"]) / fmax[(core, 8)] * 1e3 / int(r["elements"]) for r in rows]
        xs = [i + (k - 0.5) * (width + 0.04) for i in range(len(progs))]
        c.bar(xs, ns, width, color=colour, label=label)
        for x, v in zip(xs, ns):
            c.text(x, v + 0.6, f"{v:.1f}", ha="center", va="bottom", fontsize=6, color=GREY)
    c.set_xticks(range(len(progs)), ["vadd", "vadd\nunrolled", "dot", "dot\nunrolled"])
    c.grid(axis="x", visible=False)
    c.set_ylabel("ns per element, 8 lanes")
    c.set_ylim(0, 52)
    c.legend(loc="upper center", ncol=2, columnspacing=0.8, handlelength=1)
    fig.tight_layout(w_pad=1.2)
    fig.savefig(OUT / "p2_cycles.pdf")
    plt.close(fig)


def main():
    style()
    perf, sweep, ecp5 = read("perf.csv"), read("sweep.csv"), read("fpga_ecp5.csv")
    OUT.mkdir(exist_ok=True)
    fpga_figure(perf, ecp5)
    cycles_figure(perf, sweep, ecp5)
    print("wrote", OUT / "p2_fpga.pdf", "and", OUT / "p2_cycles.pdf")


if __name__ == "__main__":
    main()
