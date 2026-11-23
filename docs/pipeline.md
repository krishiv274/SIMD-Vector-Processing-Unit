# The pipelined core (Phase 2)

Files: [`rtl/simd_pipe_core.v`](../rtl/simd_pipe_core.v), [`rtl/hazard_unit.v`](../rtl/hazard_unit.v), [`rtl/vec_decoder.v`](../rtl/vec_decoder.v); the register files' write-through mode is in [register_files.md](register_files.md).

## Why

In the single-cycle core one clock period has to cover everything an instruction does. In the Phase 1 build on the ECP5 at 8 lanes that took 34.7 ns, so the clock could not go above about 29 MHz. The timing report showed where the time goes:

| Piece of the path | Time |
| --- | --- |
| instruction block RAM, decode, register read | about 12.8 ns |
| lane multiplier | about 9 ns |
| adder tree (for reduces) and writing the result | about 12.7 ns |

The three pieces are nearly equal, so putting a register between each pair lets each clock period cover only one piece. That is a pipeline: like an assembly line, each stage works on a different instruction at the same time.

## The four stages

| Stage | Does | Hardware |
| --- | --- | --- |
| **F** fetch | the instruction memory reads the next instruction | block RAM |
| **D** decode | decode the instruction, read its registers, guess branches | `vec_decoder`, register files, hazard unit |
| **X** execute | lane mux, the lane ALUs, the scalar adder, data memory, branch check | `lane_mux`, `vec_alu_array`, data memory port |
| **W** write | adder tree for reduces, lane flags for `VMSK`, save the result | `result_combiner`, register file write ports |

```text
cycle:      1    2    3    4    5    6    7    8
VLD  v1     F    D    X    W
VLD  v2          F    D    X    W
VADD v3               F    D    D    X    W      <- waits one cycle for the loaded v2
VST  v3                    F    F    D    X    W <- v3 is forwarded from W
```

Between the stages are the **pipeline registers** (`x_*` and `w_*` in the code): everything the next stage needs is copied there on each clock edge, with a `valid` bit that says whether it is a real instruction or a bubble.

## Problem 1: a result the next instruction needs is not saved yet

An instruction writes its result in W, but the instruction right behind it is already in X and needs it now. Three mechanisms handle this.

**Forwarding** (`hazard_unit.v`). If the instruction in X reads a register that the instruction in W is writing, it takes the value straight from W instead of the old copy it read in D:

```text
ADDI s1, s1, 1        # in W: s1 + 1 is ready at the start of the cycle
BNE  s1, s2, loop     # in X: compares the new s1, taken from W
```

**Write-through register files** (`BYPASS = 1`). An instruction *two* places behind reads its registers in D while the producer is in W, writing. Write-through makes that read return the value being written, so nothing else is needed.

**Stalls** (one cycle). Three kinds of result are not forwarded:

| Producer | Why | Example that waits one cycle |
| --- | --- | --- |
| a reduce (`VDOT`, `VREDADD` ...) | the adder tree adds the lanes up *during* W | `VDOT s4, v1, v2` then `ADD s3, s3, s4` |
| a masked vector op | its switched-off lanes must keep the old register value, which only the register file has | `VADD.M v3, v1, v2` then `VST v3, ...` |
| a load (`VLD`) | the data arrives from block RAM during W, and forwarding it into the multiplier would slow the whole clock (see below) | `VLD v2, ...` then `VADD v3, v1, v2` |

When the instruction in D needs such a result from the instruction in X, D waits one cycle and a bubble goes into X. One cycle later the producer is in W and the write-through register file hands D the right value. A reduce into `s0` writes nothing, so it never causes a stall.

## Problem 2: branches

A branch is only checked in X, but the next instruction has to be fetched while the branch is still in D. The core therefore **guesses**:

* a branch **backwards** (negative offset) is guessed **taken**: it is almost always the end of a loop;
* a branch **forwards** is guessed **not taken**.

The guess is made in D, so a correctly guessed taken branch costs nothing. In X the branch is checked; if the guess was wrong, the one instruction fetched by mistake (now in D) is turned into a bubble and the right one is fetched. A wrong guess costs one cycle. A loop's branch is wrong only once, when the loop ends.

This is static "backward taken, forward not taken" prediction, the scheme of early RISC pipelines. It needs no tables, only the sign bit of the offset.

## Problem 3: HALT

Instructions after `HALT` are fetched before `HALT` reaches X. Once `HALT` leaves D, D stops passing instructions on. A `HALT` fetched by a wrong guess never leaves D, because the wrong guess turns it into a bubble first. `halted` goes high when `HALT` leaves X; at that edge the last older instruction is being saved in W, so everything before `HALT` is complete and nothing after it has changed anything.

## Loads, and a decision made from the timing report

`VLD` sends its address to data memory in X, and the data arrives at the start of W, where it is written. The single-cycle core had to wait in place for that data; the pipeline keeps working, so a load itself costs one cycle instead of two.

The first version also **forwarded** the loaded data to the next instruction, so even `VLD v2` followed at once by `VADD v3, v1, v2` cost nothing. The timing report then showed the new slowest path: block RAM takes 5.6 ns to hand over its data, and from there the data went through the forwarding switch and the multiplier in the same cycle. Dropping that one forwarding path and letting the instruction wait a cycle instead (the "load-use stall" of the classic MIPS pipeline) changed the 8-lane build like this:

| Version (8 lanes, ECP5, median of 3 seeds) | Fmax | `vadd` cycles | `vadd` time |
| --- | --- | --- | --- |
| single-cycle core | 27.1 MHz | 259 | 9.57 µs |
| pipeline, load data forwarded | 42.8 MHz | 197 | 4.61 µs |
| pipeline, load-use stall (kept) | 50.6 MHz | 229 | 4.53 µs |

The stall version clocks 18% faster and costs some cycles. Over the eight workloads it is faster on six and 4.5% faster on average; on `relu` and `brighten`, short loops whose every pass has one load-use pair, forwarding is about 1% faster. A program that puts one independent instruction after each load pays nothing at all: `saxpy` already does (its `VMUL` uses the register loaded two instructions earlier), so it gains the full 18%.

## Cycle count

```text
cycles = instructions + 1 (filling the pipeline) + stalls + wrong guesses
```

| Workload, 8 lanes | Single-cycle | Pipelined | Why |
| --- | --- | --- | --- |
| `vadd` | 259 | 229 | loads no longer cost a cycle; one load-use stall per pass (32); 1 wrong guess at the loop exit |
| `saxpy` | 292 | 230 | loads free and no stalls: nothing uses a register right after it is loaded |
| `dot` | 260 | 262 | a load-use stall and a `VDOT`-then-`ADD` stall per pass (64) |
| `abs` | 260 | 294 | a load-use stall and a masked `VSUBS.M`-then-`VST` stall per pass (64) |

The golden model ([`sw/golden.py`](../sw/golden.py)) applies these rules, and the testbench checks the hardware's count against it for every program. Reordering instructions would remove most of these stalls (in `dot`, for example, moving the `ADDI` between `VDOT` and `ADD`); the programs are kept as in Phase 1 so the two cores run identical code, and the effect of scheduling and unrolling is measured separately.

## How it is checked

* Every workload and directed test, at 1, 2, 4 and 8 lanes, on both cores, against the golden model: memory, registers, lane mask, instruction count and cycle count.
* `sw/directed_tests.py` `hazards`: every kind of result used by the very next instruction, branches guessed right and wrong in both directions, a wrong-path `HALT`, and a wrong-path store that must not happen.
* Random programs on both cores (`sw/fuzz.py`, 25 per lane count and core in `make test`, 1,000 in `make fuzz`).
* A reset pressed in the middle of `vadd`, `dot` and `abs`, at ten different cycles, on both cores: nothing may be written while it is held, and the restarted program must end exactly as a clean run.
* `far_branch`: branch offsets far beyond 127 in both directions, so the guess must read the sign of the whole offset.
* Twenty-three planted bugs, one at a time (forwarding paths switched off, each stall rule removed, write-through off, guesses always "not taken" or made from the wrong offset bit, no flush after a wrong guess, a store allowed in a bubble or during reset, `HALT` one stage late, ...): the tests catch every one. Three of them came from an independent review, which also checked the stall rules against the golden model on 150,000 random instruction pairs, ran about 3,400 extra random programs (hazard-heavy loops included) and about 1,100 mid-run resets, and found no bug in the RTL.
