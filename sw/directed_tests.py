"""
directed_tests.py - small programs that poke at the corners of the design.

The benchmark programs use the processor the normal way. These programs check
the unusual cases, where bugs like to hide:
  * writes to s0 (must be ignored, even from VDOT)
  * memory addresses past the end or below zero (they wrap around)
  * the program counter running past the last instruction (it wraps to 0)
  * two loads in a row, a load right after a jump, a store then a load
  * a scalar wider than 16 bits broadcast to the lanes (only the low 16 bits count)
  * reduce instructions on compare results (each true lane adds -1)
  * the saturating jobs ADDS and SUBS at their limits (32767 + 1 stays 32767)
  * unused class-11 op codes (they do nothing)
  * BNE on numbers that differ only above bit 15 (it must compare all 32 bits)
  * VST, BNE and HALT with their unused fields filled in (no register may change)
  * instructions after HALT (they must never run)
  * branches with offsets far beyond 127, both ways (far_branch)
  * every pipeline hazard: each kind of result used by the very next
    instruction, branches guessed right and wrong, a HALT fetched on the
    wrong path, a store that a wrong guess must throw away (hazards)
  * lane masks: mixed, all off, all on, merges that keep old lanes,
    masked reduces and broadcasts (masks)

Some of these need op codes that the assembler does not write (unused ones,
and the saturating jobs, which are built straight from their numbers), so the
programs are built here in Python. run_workloads.py runs each one on the golden
model and on the RTL, for every lane count, and the two must agree exactly.
Memory starts full of random numbers so every word is meaningful.
"""

import random

import golden
import vasm

HALT = vasm.encode(vasm.OP_HALT)
NOP = vasm.encode(0x3E)            # an unused class-11 op code does nothing


def corner_program():
    words = vasm.assemble("""
            ADDI s0, s0, 5          # write to s0: must be dropped
            ADDI s1, s0, -1         # s1 = 0xFFFFFFFF
            VLD  v1, 0(s1)          # address -1 wraps to the top word
            VLD  v2, -2(s0)         # address -2
            VLD  v2, 3(s0)          # two loads in a row into the same register
            VDOT s0, v1, v2         # reduce into s0: must be dropped
            ADDI s2, s0, 0x1234
            ADD  s2, s2, s2
            ADD  s2, s2, s2
            ADD  s2, s2, s2
            ADD  s2, s2, s2
            ADDI s2, s2, 5          # s2 = 0x12345, wider than 16 bits
            VADD v3, v1, s2         # broadcast uses only the low 16 bits
            VSLL v4, v1, s2         # shift by 5
            VSRA v5, v1, s2
            VREDSEQ s3, v1, v1      # every lane true: s3 = -LANES
            VREDSLT s4, v1, v2
            VREDSLTU s5, v1, v2
            VST  v3, 0(s1)          # store at the top word
            VST  v4, 1(s1)          # 0xFFFFFFFF + 1 wraps to word 0
            ADDI s6, s0, 0x7FFF
            ADD  s6, s6, s6
            ADD  s6, s6, s6
            ADD  s6, s6, s6         # 0x3FFF8: wraps inside memory
            VST  v5, 0(s6)
            VLD  v6, 0(s6)          # store then load the same address
            BNE  s1, s0, fwd        # forward branch, taken
            VST  v6, 5(s0)          # must be skipped
    fwd:    VST  v6, 6(s0)
            BNE  s0, s0, never      # forward branch, not taken
            VLD  v7, 9(s0)
            BNE  s1, s0, tgt        # jump straight to a load
            HALT
    tgt:    VLD  v7, 10(s0)
            BNE  s7, s1, bump       # a load followed by a branch
    never:  HALT
    bump:   ADDI s7, s7, -1
            VST  v7, 7(s0)
            BNE  s7, s1, tgt
            VREDMAX s7, v1, v2
    """)
    # The saturating jobs (ADDS = job 14, SUBS = job 15) in all three forms,
    # each followed by a store or add that makes the result visible. Memory is
    # random, so many lanes go past the limits; s5 = 32767 broadcast to every
    # lane pushes almost every lane over the edge.
    extra = [vasm.assemble("ADDI s5, s0, 0x7FFF")[0]]
    for opcode in (0x0E, 0x0F):                      # VADDS / VSUBS v6, v1, v2
        extra.append(vasm.encode(opcode, rd=6, ra=1, rb=2))
        extra.append(vasm.assemble(f"VST v6, {20 + len(extra)}(s0)")[0])
    for opcode in (0x1E, 0x1F):                      # VADDS / VSUBS v6, v1, s5
        extra.append(vasm.encode(opcode, rd=6, ra=1, rb=5))
        extra.append(vasm.assemble(f"VST v6, {20 + len(extra)}(s0)")[0])
    for opcode in (0x2E, 0x2F):                      # VREDADDS / VREDSUBS s3, v1, v2
        extra.append(vasm.encode(opcode, rd=3, ra=1, rb=2))
        extra.append(vasm.assemble("ADD s4, s4, s3")[0])
    # Unused class-11 op codes with every field set: each must do nothing.
    for opcode in (0x32, 0x33, 0x35, 0x36, 0x37, 0x3A, 0x3B, 0x3D, 0x3E):
        extra.append(vasm.encode(opcode, rd=5, ra=1, rb=2, imm=0x7FFF))   # must do nothing
    extra.append(vasm.assemble("VST v3, 40(s0)")[0])
    extra.append(HALT)
    return words + extra


def pc_wrap_program():
    words = [NOP] * 256
    words[0] = vasm.assemble("ADDI s1, s1, 1")[0]
    words[1] = vasm.assemble("ADDI s2, s0, 3")[0]
    words[2] = vasm.encode(vasm.OP_BNE, ra=1, rb=2, imm=-10)   # 2 - 10 wraps to 248
    words[3] = HALT
    words[248] = vasm.assemble("ADDI s3, s3, 1")[0]
    words[254] = vasm.assemble("VLD v1, 0(s3)")[0]
    words[255] = vasm.assemble("VST v1, 100(s3)")[0]           # then the PC wraps 255 -> 0
    return words


def branch_halt_program():
    # s2 and s3 have the same low 16 bits; s5 and s3 differ only in bit 31.
    # Each BNE must jump. If it does not, the VST right after it marks memory.
    doubling = "\n".join(["ADD s1, s1, s1"] * 16)            # s1 = 1 -> 0x10000
    words = vasm.assemble(f"""
            ADDI s1, s0, 1
            {doubling}
            ADDI s2, s1, 5          # s2 = 0x00010005
            ADDI s3, s0, 5          # s3 = 0x00000005
            VLD  v1, 1(s0)
            VLD  v2, 2(s0)
            VLD  v3, 3(s0)          # v3, v5, v6, v7 hold random values, so a stray
            VLD  v5, 4(s0)          # write into any of them would show up
            VLD  v6, 5(s0)
            VLD  v7, 6(s0)
            BNE  s2, s3, bit16
            VST  v1, 30(s0)         # only reached if BNE ignored bit 16
    bit16:  ADD  s4, s1, s1
    """) + vasm.assemble("\n".join(["ADD s4, s4, s4"] * 14) + """
            ADDI s5, s4, 5          # s5 = 0x80000005
            BNE  s5, s3, bit31
            VST  v1, 31(s0)         # only reached if BNE ignored bit 31
    bit31:  ADDI s6, s0, 1
    """)
    # Unused fields filled in: rd on VST and BNE, every field on HALT.
    words += [vasm.encode(vasm.OP_VST, rd=6, ra=0, rb=2, imm=32),          # store v2, must not write v6
              vasm.encode(vasm.OP_BNE, rd=5, ra=3, rb=3, imm=3),           # equal: not taken, must not write v5
              vasm.encode(vasm.OP_HALT, rd=7, ra=1, rb=2, imm=0x1234)]      # must not write v7
    # Nothing below may run: each line would change memory or a register.
    words += vasm.assemble("""
            ADDI s6, s0, 99
            VADD v3, v1, v2
            VST  v1, 33(s0)
            HALT
    """)
    return words


def hazards_program():
    """Every case the pipelined core has to handle, each right behind its
    producer. On the single-cycle core the same program simply runs; both
    must give the golden model's answers and cycle count."""
    return vasm.assemble("""
            ADDI s1, s0, 3          # s1 = 3
            ADDI s2, s1, 4          # needs s1 at once: forwarded from W
            ADD  s3, s1, s2         # s1 from the write-through file, s2 forwarded
            VLD  v1, 0(s1)          # address uses s1
            VADD v2, v1, v1         # needs v1 right after the load: one stall
            VST  v2, 40(s0)         # needs v2 at once: forwarded vector
            VLD  v3, 40(s0)         # load what was just stored
            VST  v3, 41(s0)         # store a register loaded one cycle ago: one stall
            VLD  v4, 41(s0)
            ADDI s7, s0, 1          # one instruction in between ...
            VADD v4, v4, v3         # ... and the loaded v4 comes from the register file: no stall
            VDOT s4, v1, v2         # a reduce ...
            ADD  s5, s4, s4         # ... used at once: one stall
            VREDADD s6, v1, v1
            BNE  s6, s0, fwd1       # branch on a fresh sum: stall, then a forward jump (wrong guess)
            VST  v1, 42(s0)         # skipped
    fwd1:   VREDADD s0, v1, v2      # a reduce into s0 writes nothing ...
            ADD  s7, s0, s0         # ... so no stall here
            VSLT v4, v1, v2
            VMSK v4                 # a new mask ...
            VADD.M v5, v1, v2       # ... used at once (write-through mask)
            VST  v5, 43(s0)         # a masked result used at once: one stall
            VSUB.M v5, v5, v1       # masked, reading its own masked result
            VREDADD.M s1, v5, v2    # masked vector into a masked reduce: stall
            VMUL.M v5, v5, s1       # reduce sum into a masked broadcast: stall
            VST  v5, 44(s0)
            VMSK v0                 # v0 = 0 (power-up): every lane off
            VADD.M v6, v1, v1       # changes nothing
            VREDADD.M s2, v1, v1    # adds nothing: s2 = 0
            VSEQ v7, v0, v0         # all ones ...
            VMSK v7                 # ... every lane on again
            ADDI s2, s0, 0
            ADDI s3, s0, 4
    back:   ADDI s2, s2, 1          # a loop: the backward branch is guessed taken,
            VMUL v6, v6, s2         # and guessed wrong only once, at the exit
            BNE  s2, s3, back
            ADDI s2, s0, 3
            VADD v5, v0, s2         # every lane 3 (v0 is still 0)
            VREDADD s7, v5, v0      # s7 = 3 * LANES
            ADDI s2, s0, 0
    back3:  ADDI s2, s2, 1
            VADD v5, v0, s2         # every lane s2
            VREDADD s4, v5, v0      # s4 = s2 * LANES ...
            BNE  s4, s7, back3      # ... tested at once: a stall, then a backward branch (runs 3 times)
            VDOT s4, v6, v1
            BNE  s4, s3, back2      # forward, on a fresh sum: stall, then taken
            HALT
    back2:  BNE  s0, s0, trap       # backward but NOT taken: wrong guess; the
            BNE  s0, s0, never      # store at "trap" must never happen
            BNE  s3, s0, last       # forward taken: wrong guess ...
            HALT                    # ... this HALT is fetched but must be thrown away
    never:  VST  v1, 45(s0)
    trap:   VST  v1, 46(s0)
    last:   VST  v6, 47(s0)
            VST  v5, 48(s0)
            HALT
    """)


def masks_program():
    """Lane-mask corner cases: masks from loaded data, merges that keep old
    lanes, masked reduces and masked broadcasts."""
    return vasm.assemble("""
            VLD  v1, 1(s0)          # random data
            VLD  v2, 2(s0)
            VLD  v3, 3(s0)
            VMSK v1                 # on where v1 is not zero (random data: nearly all)
            VAND v4, v1, v2         # some lanes zero, some not
            VMSK v4                 # a genuinely mixed mask
            VADD.M v3, v1, v2       # switched-off lanes of v3 keep their loaded value
            VST  v3, 10(s0)
            VSUBS.M v3, v3, v1
            VST  v3, 11(s0)
            ADDI s1, s0, -7
            VMUL.M v3, v3, s1       # masked broadcast
            VST  v3, 12(s0)
            VREDADD.M s2, v1, v2    # sum of the switched-on lanes only
            VDOT.M s3, v1, v2
            VREDSEQ.M s4, v1, v1    # counts the switched-on lanes (each adds -1)
            VXOR v5, v5, v5         # v5 = 0
            VMSK v5                 # every lane off
            VADD.M v3, v1, v2       # changes nothing
            VST  v3, 13(s0)
            VREDADD.M s5, v1, v2    # adds nothing: s5 = 0
            VLD  v6, 0(s1)          # address -7: wraps
            VMSK v6
            VMAX.M v6, v6, v1
            VST  v6, 14(s0)
            VADD.M v7, v7, v7       # the mask stays as VMSK left it at the end
            HALT
    """)


def far_branch_program():
    """Branches with offsets far beyond 127 in both directions, guessed right
    and wrong. The branch guess must look at the sign of the whole 16-bit
    offset (bit 15), not just its low byte."""
    words = [NOP] * 256
    one = lambda text: vasm.assemble(text)[0]
    words[0] = one("ADDI s1, s0, 1")
    words[1] = vasm.encode(vasm.OP_BNE, ra=1, rb=0, imm=253)     # forward to 254, taken: guessed wrong
    words[2] = HALT                                              # skipped
    words[254] = one("ADDI s2, s2, 1")
    words[255] = vasm.encode(vasm.OP_BNE, ra=2, rb=4, imm=-250)  # backward to 5, taken: guessed right
    words[5] = one("ADDI s5, s0, 200")
    words[6] = vasm.encode(vasm.OP_BNE, ra=5, rb=0, imm=200)     # forward to 206, taken: guessed wrong
    words[206] = one("ADDI s6, s6, 7")
    words[207] = vasm.encode(vasm.OP_BNE, ra=0, rb=0, imm=-150)  # backward, never taken: guessed wrong
    words[208] = one("VLD v1, 3(s6)")
    words[209] = one("VST v1, 50(s5)")
    words[210] = HALT
    return words


DIRECTED = {
    "corners": corner_program,
    "pc_wrap": pc_wrap_program,
    "branch_halt": branch_halt_program,
    "hazards": hazards_program,
    "far_branch": far_branch_program,
    "masks": masks_program,
}


def prepare_directed(name, lanes, w=16, pipelined=False):
    """Golden-model run of one directed program, in the same form as
    run_workloads.prepare() returns."""
    rng = random.Random(f"{name}-seed")
    model = golden.SimdModel(lanes, w, pipelined=pipelined)
    for i in range(len(model.mem)):
        model.mem[i] = [rng.randrange(1 << w) for _ in range(lanes)]
    start_memory = golden.memory_hex_lines(model)
    words = DIRECTED[name]()
    instructions, cycles = model.run(words)
    return dict(words=words, start_memory=start_memory, model=model,
                instructions=instructions, cycles=cycles, elements=0)
