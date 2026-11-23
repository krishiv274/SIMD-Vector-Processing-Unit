# Software: assembler, golden model and workloads

Writing programs as raw 32-bit numbers is slow and error-prone, and the hardware needs something to be checked against. This folder provides both.

| File | What it is |
| --- | --- |
| [`vasm.py`](vasm.py) | The **assembler**. It turns text like `VADD v3, v1, v2` into the 32-bit words the hardware runs. Labels, comments and constants such as `ITER` or `B+1` are allowed; constants may only use whole numbers, names and plain arithmetic, so a program can never run other code. It reports mistakes with the line number. Phase 2 adds the saturating jobs (`VADDS`, `VSUBS`, `VREDADDS`, `VREDSUBS`), the `.M` ending that makes any vector or reduce instruction masked (`VADD.M v3, v1, v2` sets bit 16), and `VMSK va`, which sets the lane mask. |
| [`golden.py`](golden.py) | The **golden model**: a Python description of what every instruction does. It runs the same machine code and predicts the final memory, the registers, the instruction count and the exact cycle count. |
| [`run_workloads.py`](run_workloads.py) | Runs every workload on the golden model and on the Verilog core, checks both, and writes the cycle counts to `results/perf.csv`. |
| [`directed_tests.py`](directed_tests.py) | Small hand-built programs for the corner cases (wrap-around, unused op codes, branches on high bits, code after `HALT`). |
| [`fuzz.py`](fuzz.py) | Random programs: makes up programs with every op code and random data, and checks the Verilog core against the golden model on each. |
| [`sweep.py`](sweep.py) | Runs `vadd`, `dot` and their unrolled versions for N = 8 to 256 on both cores and every lane count, and writes `results/sweep.csv` (Phase 2). |
| [`plot_perf.py`](plot_perf.py) | Draws the speed-up figures in `results/figures/` (cycles, speed-up, cycle breakdown, problem size, unrolling). |
| [`programs/`](programs) | The workloads, written in assembly. |
| [`tests/test_vasm.py`](tests/test_vasm.py), [`tests/test_golden.py`](tests/test_golden.py) | Self-checks for the assembler (encodings, branch offsets, errors, every program assembles) and the golden model. |

## Workloads

Each benchmark processes N = 256 random sixteen-bit numbers, over the full range from −32768 to 32767 (`isa_test` uses 32). The loop runs N / LANES times, so the same file is both the scalar version (1 lane, 256 passes) and the SIMD version (8 lanes, 32 passes).

| Program | Computes | Why it is a good test |
| --- | --- | --- |
| [`vadd.s`](programs/vadd.s) | `C[i] = A[i] + B[i]` | The simplest data-parallel loop. |
| [`saxpy.s`](programs/saxpy.s) | `Y[i] = a·X[i] + Y[i]` | Classic BLAS kernel; uses a scalar broadcast to every lane. |
| [`relu.s`](programs/relu.s) | `Y[i] = max(X[i], 0)` | The activation function of most neural networks. |
| [`threshold.s`](programs/threshold.s) | `Y[i] = X[i] if X[i] ≥ T else 0` | An `if` without a branch: compare, flip the mask, AND. |
| [`dot.s`](programs/dot.s) | `Σ A[i]·B[i]` | Uses `VDOT`: multiply in the lanes, add up in the result combiner. Each product keeps its low 16 bits, as with `VMUL`. |
| [`isa_test.s`](programs/isa_test.s) | every instruction | Coverage: all 16 ALU jobs in all three forms on random data, plus `VMSK` and masked vector, vector-scalar and reduce instructions. The FPGA build loads it, so no instruction's logic can be optimised away. |
| [`brighten.s`](programs/brighten.s) | `Y[i] = X[i] + K`, clamped | Phase 2. Saturating add: the brightest pixels stay at 32,767 instead of wrapping to a large negative number. |
| [`abs.s`](programs/abs.s) | `Y[i] = \|X[i]\|` | Phase 2. A lane mask built from a compare (`VSLT`, `VMSK`), then a masked `VSUBS.M` changes only the negative lanes. Saturation makes `\|−32768\|` give 32,767. |
| [`sumpos.s`](programs/sumpos.s) | `Σ X[i]` over `X[i] > 0` | Phase 2. A masked reduce (`VREDADD.M`): the result combiner adds only the switched-on lanes, with no branch. |
| [`vadd_unroll.s`](programs/vadd_unroll.s), [`dot_unroll.s`](programs/dot_unroll.s) | as `vadd`, `dot` | Phase 2. The loop unrolled 4 times with all loads first: fewer instructions per vector, and no stalls on the pipelined core. |

## Commands

```bash
python3 sw/run_workloads.py                              # all workloads, 8 lanes, both cores, golden model + RTL
python3 sw/run_workloads.py --lanes 1 2 4 8 --csv results/perf.csv
python3 sw/run_workloads.py --cores pipe                 # only the pipelined core
python3 sw/sweep.py                                      # problem-size sweep -> results/sweep.csv
python3 sw/run_workloads.py --no-rtl                     # golden model only (no Verilog tools needed)
python3 sw/vasm.py sw/programs/vadd.s -o vadd.hex -D A=0 -D B=256 -D C=512 -D ITER=32
python3 -m unittest discover -s sw/tests -v
```

## Results

<p align="center"><img src="../results/figures/cycles.png" width="680" alt="Clock cycles per workload for 1, 2, 4 and 8 lanes, single-cycle and pipelined cores"></p>

| Workload | single-cycle, 1 lane | single-cycle, 8 lanes | pipelined, 1 lane | pipelined, 8 lanes | 8-lane speed-up (cycles), single / pipelined |
| --- | --- | --- | --- | --- | --- |
| vadd | 2,051 | 259 | 1,797 | 229 | 7.92× / 7.85× |
| saxpy | 2,308 | 292 | 1,798 | 230 | 7.90× / 7.82× |
| relu | 1,539 | 195 | 1,541 | 197 | 7.89× / 7.82× |
| threshold | 2,053 | 261 | 2,055 | 263 | 7.87× / 7.81× |
| dot | 2,052 | 260 | 2,054 | 262 | 7.89× / 7.84× |
| brighten | 1,540 | 196 | 1,542 | 198 | 7.86× / 7.79× |
| abs | 2,052 | 260 | 2,310 | 294 | 7.89× / 7.86× |
| sumpos | 2,053 | 261 | 2,311 | 295 | 7.87× / 7.83× |

The speed-up is just under 8 because a few instructions run only once, whatever the lane count: the setup before the loop and the final `HALT` (and, on the pipelined core, filling the pipeline and the wrong branch guess at the loop exit). That fixed part does not shrink with more lanes. This is Amdahl's law: speed-up is limited by the part of the work that cannot be done in parallel. The real-time comparison of the two cores, the effect of the problem size and of unrolling are in [docs/speedup_analysis.md](../docs/speedup_analysis.md).
