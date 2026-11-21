# Architecture

<p align="center"><img src="figures/block_diagram.png" width="760" alt="Block diagram of the SIMD vector processing unit"></p>

## The idea in one paragraph

A normal (scalar) processor adds two numbers per instruction. A SIMD (Single Instruction, Multiple Data) processor adds eight pairs at once: one instruction, one control decision, eight calculators working side by side. Images, audio, neural networks and physics all repeat the same operation over long arrays, so this kind of work speeds up almost in proportion to the number of calculators ("lanes"). This project builds such a unit in Verilog and measures that speed-up.

## The blocks

| Block | File | What it does |
| --- | --- | --- |
| Decoder | [`rtl/vec_decoder.v`](../rtl/vec_decoder.v) | Turns one instruction into its fields, its on/off signals and the list of registers it reads and writes. Shared by both cores, so they cannot disagree. |
| Vector control unit | [`rtl/vec_control_unit.v`](../rtl/vec_control_unit.v) | Single-cycle core: uses the decoder and chooses the next instruction (program counter, branches, the extra cycle for loads). |
| Vector register file | [`rtl/vec_regfile.v`](../rtl/vec_regfile.v) | Eight 128-bit registers and the lane mask. Two can be read and one written in the same cycle; a write can change only some lanes; optional write-through for the pipeline. |
| Scalar register file | [`rtl/scalar_regfile.v`](../rtl/scalar_regfile.v) | Eight 32-bit registers for addresses, counters, broadcast values and sums. `s0` is always 0. A debug port lets the outside world read any of them. |
| Lane MUX | [`rtl/lane_mux.v`](../rtl/lane_mux.v) | For every lane, picks operand B: the lane's own element from a vector register, or one scalar value copied into every lane (broadcast). |
| Lane ALU | [`rtl/vec_alu_lane.v`](../rtl/vec_alu_lane.v) | One 16-bit calculator: add, subtract, logic, shifts, multiply, compare, min and max, and saturating add and subtract. |
| Parallel ALU array | [`rtl/vec_alu_array.v`](../rtl/vec_alu_array.v) | `LANES` copies of the lane ALU, all running the same job on different data. |
| Result combiner | [`rtl/result_combiner.v`](../rtl/result_combiner.v) | Hands the lane outputs back as one vector, adds the (switched-on) lanes together with an adder tree (for `VDOT` and the other reduce instructions), and flags the non-zero lanes for `VMSK`. |
| Core (single-cycle) | [`rtl/simd_core.v`](../rtl/simd_core.v) | Wires the blocks together, and counts clock cycles and completed instructions. One instruction per cycle; the Phase 1 design and the baseline. |
| Core (pipelined) | [`rtl/simd_pipe_core.v`](../rtl/simd_pipe_core.v) | The same blocks in a four-stage pipeline (fetch, decode, execute, write), with branch guessing. Same ports, same instructions, same answers. See [pipeline.md](pipeline.md). |
| Hazard unit | [`rtl/hazard_unit.v`](../rtl/hazard_unit.v) | Pipelined core only: forwards results to the next instruction, and makes it wait one cycle when a result is not ready in time. |
| FPGA top | [`rtl/fpga_top.v`](../rtl/fpga_top.v) | The core plus block-RAM instruction and data memories, three switches and eight LEDs. This is the file that goes onto a real FPGA. |

## Following one instruction: `VMUL v3, v1, s2`

1. The instruction memory hands the 32-bit word to the control unit.
2. The control unit sees class `01` (vector–scalar), ALU job `MUL`. It sets `use_scalar = 1`, `alu_op = MUL` and `vreg_write = 1`.
3. The vector register file reads `v1` (read port A). At the same time the scalar register file reads `s2`.
4. The lane MUX copies the low 16 bits of `s2` into all 8 lanes of operand B.
5. All 8 lane ALUs multiply at the same time: `v1[i] × s2`.
6. The result combiner passes the 8 results on as one 128-bit vector.
7. At the next clock edge, that vector is written into `v3`, and the program counter moves on.

In the single-cycle core, steps 1–6 all happen inside one clock cycle. In the pipelined core they are spread over four cycles, and three other instructions are in the other stages at the same time ([pipeline.md](pipeline.md)).

## Timing (single-cycle core)

* **One instruction per clock cycle.** Every instruction except a load finishes in the cycle it starts. There is no pipeline, so there are no hazards to manage. (The pipelined core's rules are in [pipeline.md](pipeline.md).)
* **Loads take two cycles.** FPGA block RAM returns data one clock after it receives the address. During the first cycle of `VLD`, the control unit sends the address and holds the program counter. During the second, the data has arrived and is written into the register.
* **Jumps are free.** The instruction memory also has a one-clock delay. To hide it, the control unit gives the memory the address of the *next* instruction, which it works out in the current cycle (including any branch decision). The right instruction is therefore always ready at the start of each cycle.

As a result, the cycle count of a program is exact and simple to predict: **cycles = instructions + loads**. The Python golden model predicts it, and the testbench checks the hardware against it.

## Why the scalar baseline is fair

`LANES` is a parameter. With `LANES = 1`, the same design is an ordinary one-number-at-a-time processor: one 16-bit ALU, 16-bit vector registers and 16-bit memory words (the scalar registers stay 32 bits wide). The scalar and SIMD versions run the **same program** with the same instruction set and the same timing rules. Only the number of loop passes changes, because each pass covers `LANES` elements. Any difference in cycle count therefore comes from data-level parallelism alone, not from a better compiler or a different microarchitecture.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `LANES` | 8 | lanes working in parallel (1, 2, 4 or 8 are tested) |
| `W` | 16 | bits per element; a power of two below 32 (8 or 16), because shifts use `b mod W` and sums are 32 bits wide |
| `IADDR_W` | 8 | instruction memory size, 2^8 = 256 instructions |
| `DADDR_W` | 10 | data memory size: 1,024 words of `LANES*W` bits, in simulation and on the FPGA |

## Design choices

| Choice | Reason |
| --- | --- |
| 8 lanes × 16 bits = 128-bit vectors | The same vector width as Intel SSE and ARM NEON. 16-bit multipliers fit FPGA DSP blocks, so all 8 lanes fit a mid-range FPGA. |
| Single-cycle core kept next to the pipelined one | The single-cycle core is easy to verify and explain, and its cycle count is trivial. Keeping it, with the same instruction set, makes the Phase 2 comparison fair: the only difference between the two builds is the pipeline. |
| Four-stage pipeline (Phase 2) | The measured critical path of the single-cycle core falls into three nearly equal pieces (fetch + decode + register read, the multiplier, the adder tree + write). A register between each piece lets the clock run about twice as fast. |
| Merge masking with one mask register (Phase 2) | A masked write only needs per-lane write enables in the register file, not a third read port for the old value. One mask register (instead of AVX-512's eight) keeps the encoding to one spare bit. |
| Saturating `ADDS`/`SUBS` (Phase 2) | They use the last two free job codes, and image and audio code needs them. |
| Compare results are all ones / all zeros | Can be used directly as masks with `VAND`, so an `if` needs no branch (see `sw/programs/threshold.s`). |
| Reduce instructions sum lane results | A dot product step (multiply and add up) becomes one instruction. This is what the result combiner's adder tree is for. |
| Memories outside the core | The same core is tested in simulation and built for the FPGA without changes. |

## How it compares with real SIMD architectures

The design borrows its main conventions from instruction sets in everyday use.

| | Intel SSE2 | Intel AVX2 | Arm Neon (AArch64) | MIPS MSA | RISC-V V 1.0 | This unit |
| --- | --- | --- | --- | --- | --- | --- |
| Vector width | 128 bits | 256 bits | 128 bits | 128 bits | `VLEN`, chosen by each chip | 128 bits (`LANES` × 16) |
| Vector registers | 8 (16 in 64-bit mode) | 16 | 32 | 32 | 32 | 8 |
| Integer element sizes | 8, 16, 32, 64 | 8, 16, 32, 64 | 8, 16, 32, 64 | 8, 16, 32, 64 | 8, 16, 32, 64 | 16 |
| Compare result | all-ones mask per lane | all-ones mask | all-ones mask | all-ones mask | one mask bit per element | all-ones mask |
| Masked execution | no (blend with masks) | no (blend with masks) | no (`BSL` select) | no | yes, mask in `v0` | yes, one mask register, `.M` forms |
| Saturating add | yes (`PADDSW`) | yes | yes (`SQADD`) | yes (`ADDS_S`) | yes (`vsadd`) | yes (`VADDS`) |
| Vector op scalar | broadcast first, then op | broadcast first | by-element multiply | splat first | `.vx` forms (`vadd.vx`) | `.VX` form (`VADD v3, v1, s2`) |
| Sum across lanes | pairs only (`PMADDWD`) | pairs only (`VPMADDWD`) | full (`ADDV`) | pairs only (`DOTP_S`) | full (`vredsum`) | full (`VREDADD`, `VDOT`) |
| Program for any width | no | no | no | no | yes (`vsetvli`) | no: reassemble with a new `ITER` |

What this project takes from each:

* **128-bit registers and all-ones compare masks** come from SSE, Neon and MSA. A mask can be used directly with `VAND` to keep or clear a lane, so conditionals need no branch (`sw/programs/threshold.s`).
* **The vector–scalar form** follows RISC-V V's `.vx` instructions: the scalar register is broadcast inside the instruction, so no separate splat instruction or spare vector register is needed.
* **Full reductions** follow Neon's `ADDV` and RISC-V V's `vredsum`: the adder tree adds all eight lane results into one 32-bit sum, where x86 `PMADDWD` and MSA `DOTP_S` add products in pairs. Those two keep each product at full 32-bit width, though; `VDOT` keeps only its low 16 bits (as `VMUL` does), so its inputs must be small enough for every product to fit. A widening multiply is a Phase 2 candidate.
* **The `LANES` parameter** plays the role of RISC-V V's implementation-chosen `VLEN`: the same design is built at 1, 2, 4 or 8 lanes. Unlike RISC-V V, a program has to be reassembled for a new lane count (`ITER = N / LANES`). A `vsetvli`-style instruction would remove that step.

* **Masked execution** (Phase 2) follows RISC-V V and AVX-512 merge masking: switched-off lanes keep their old value. As in RISC-V V, one mask register and one bit in the instruction say "masked"; `VMSK` turns a compare result into the mask.

Where it is deliberately smaller: 8 vector registers instead of 32 (3-bit register fields keep the encoding and the FPGA register file small); one element size instead of four; one mask register.
