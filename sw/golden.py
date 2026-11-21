"""
golden.py - a Python model of the SIMD processor (the "golden model").

It runs the same machine code as the hardware, one instruction at a time,
and records what the memory and registers should contain at the end, plus
how many instructions and clock cycles the program should take, for either
core: the single-cycle one (simd_core.v) or the pipelined one
(simd_pipe_core.v). Both give the same answers; only the cycle count differs.

The testbench then compares the real hardware against these numbers. If
both agree on every memory word, every scalar register and the exact cycle
count, we can be confident the hardware is right.

This model only describes WHAT each instruction does. It knows nothing about
wires, clocks or lanes running in parallel - that is the point: it is a
completely separate description of the same behaviour.
"""

ALU_OPS = ["ADD", "SUB", "AND", "OR", "XOR", "SLL", "SRL", "SRA",
           "MUL", "SEQ", "SLT", "SLTU", "MIN", "MAX", "ADDS", "SUBS"]


def to_signed(x: int, bits: int) -> int:
    """Read a raw bit pattern as a signed number: 0xFFFF -> -1 for 16 bits."""
    x &= (1 << bits) - 1
    return x - (1 << bits) if x >> (bits - 1) else x


def clamp(x: int, bits: int) -> int:
    """Keep a number inside the signed range: for 16 bits, -32768 .. 32767.
    This is what the saturating jobs ADDS and SUBS do instead of wrapping."""
    low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return max(low, min(high, x))


def alu(op: int, a: int, b: int, w: int = 16) -> int:
    """What one lane computes. a and b are raw w-bit patterns."""
    mask = (1 << w) - 1
    sa, sb = to_signed(a, w), to_signed(b, w)
    shift = b % w
    name = ALU_OPS[op] if op < len(ALU_OPS) else ""   # "" = unused op code, gives 0
    result = {
        "ADD": sa + sb,
        "SUB": sa - sb,
        "AND": a & b,
        "OR": a | b,
        "XOR": a ^ b,
        "SLL": a << shift,
        "SRL": a >> shift,
        "SRA": sa >> shift,               # Python's >> on negative numbers keeps the sign
        "MUL": sa * sb,
        "SEQ": -1 if a == b else 0,
        "SLT": -1 if sa < sb else 0,
        "SLTU": -1 if a < b else 0,
        "MIN": a if sa < sb else b,
        "MAX": b if sa < sb else a,
        "ADDS": clamp(sa + sb, w),
        "SUBS": clamp(sa - sb, w),
    }.get(name, 0)
    return result & mask


def decode(word):
    """Cut an instruction into its fields, and list the registers it reads.
    The read lists follow the same rules as rtl/vec_decoder.v."""
    opcode = word >> 26
    iclass = opcode >> 4
    d = dict(opcode=opcode, iclass=iclass, func=opcode & 0xF,
             rd=(word >> 23) & 7, ra=(word >> 20) & 7, rb=(word >> 17) & 7,
             imm=to_signed(word & 0xFFFF, 16),
             masked=bool(word >> 16 & 1) and iclass != 3)   # bit 16 only counts for lane work
    vmsk = opcode == 0x34
    d["reads_v"] = ({d["ra"]} if iclass != 3 or vmsk else set()) | \
                   ({d["rb"]} if iclass in (0, 2) or opcode == 0x31 else set())
    d["reads_s"] = ({d["ra"]} if opcode in (0x30, 0x31, 0x38, 0x39, 0x3C) else set()) | \
                   ({d["rb"]} if iclass == 1 or opcode in (0x39, 0x3C) else set())
    return d


def must_wait(older, newer):
    """Pipelined core only: does 'newer', right behind 'older', wait one cycle?

    The pipeline hands most results straight to the next instruction, but
    three are not (see rtl/hazard_unit.v):
      * the sum of a reduce, which is only added up in the last stage
      * a masked vector result, whose switched-off lanes still have to be
        merged with the old register value by the register file
      * loaded data, which arrives from block RAM too late in the cycle
    """
    reduce_sum = older["iclass"] == 2 and older["rd"] != 0 and older["rd"] in newer["reads_s"]
    masked_vec = older["iclass"] in (0, 1) and older["masked"] and older["rd"] in newer["reads_v"]
    load_data = older["opcode"] == 0x30 and older["rd"] in newer["reads_v"]
    return reduce_sum or masked_vec or load_data


class SimdModel:
    """The processor state: registers, lane mask, data memory and counters.

    Sizes match the simulation testbench: 256 instructions (the program
    counter wraps after 255) and 1,024 data-memory words (addresses wrap).
    pipelined=True gives the cycle count of the pipelined core.
    """

    def __init__(self, lanes, w=16, dmem_words=1024, pipelined=False):
        self.lanes, self.w = lanes, w
        self.pipelined = pipelined
        self.v = [[0] * lanes for _ in range(8)]       # vector registers
        self.s = [0] * 8                               # scalar registers (32-bit)
        self.mask = [1] * lanes                        # lane mask: every lane on at power-up
        self.mem = [[0] * lanes for _ in range(dmem_words)]
        self.instructions = 0
        self.cycles = 0
        # Where the extra cycles come from (for the analysis). Each is only
        # counted for the core where it costs a cycle:
        self.loads = 0          # single-cycle core: each VLD costs one extra cycle
        self.stalls = 0         # pipelined core: waits for a result (see must_wait)
        self.mispredicts = 0    # pipelined core: wrong guess about a branch

    def run(self, program, max_steps=1_000_000):
        """Run machine code until HALT. Returns (instructions, cycles)."""
        pc = 0
        mask32 = 0xFFFFFFFF
        older = None                                      # the instruction just before this one
        for _ in range(max_steps):
            # Past the end of the program counts as HALT (memory is padded with HALT).
            word = program[pc] if pc < len(program) else 0x3F << 26
            d = decode(word)
            opcode, iclass, rd, ra, rb, imm = (d[k] for k in ("opcode", "iclass", "rd", "ra", "rb", "imm"))
            s = self.s
            next_pc = pc + 1
            self.instructions += 1
            if self.pipelined and older is not None and must_wait(older, d):
                self.stalls += 1

            if iclass in (0, 1, 2):                       # vector ALU work
                a = self.v[ra]
                b = [s[rb] & ((1 << self.w) - 1)] * self.lanes if iclass == 1 else self.v[rb]
                out = [alu(d["func"], a[i], b[i], self.w) for i in range(self.lanes)]
                on = self.mask if d["masked"] else [1] * self.lanes
                if iclass == 2:                           # reduce: add the switched-on lanes
                    self.write_s(rd, sum(to_signed(x, self.w) for x, e in zip(out, on) if e))
                else:                                     # switched-off lanes keep their value
                    self.v[rd] = [x if e else old for x, old, e in zip(out, self.v[rd], on)]
            elif opcode == 0x30:                          # VLD
                self.v[rd] = list(self.mem[self.addr(s[ra] + imm)])
                self.loads += not self.pipelined
            elif opcode == 0x31:                          # VST
                self.mem[self.addr(s[ra] + imm)] = list(self.v[rb])
            elif opcode == 0x34:                          # VMSK: on where the lane is not zero
                self.mask = [int(x != 0) for x in self.v[ra]]
            elif opcode == 0x38:                          # ADDI
                self.write_s(rd, s[ra] + imm)
            elif opcode == 0x39:                          # ADD
                self.write_s(rd, s[ra] + s[rb])
            elif opcode == 0x3C:                          # BNE
                taken = s[ra] != s[rb]
                if taken:
                    next_pc = pc + imm
                # The pipelined core guesses "taken" for a jump backwards (a loop)
                # and "not taken" for a jump forwards; a wrong guess costs a cycle.
                if self.pipelined and taken != (imm < 0):
                    self.mispredicts += 1
            elif opcode == 0x3F:                          # HALT
                self.cycles = self.count_cycles()
                return self.instructions, self.cycles
            pc = next_pc & 0xFF
            for i in range(8):
                s[i] &= mask32
            older = d
        raise RuntimeError("program did not reach HALT")

    def count_cycles(self):
        """Clock cycles from reset to HALT.
        Single-cycle core: one per instruction, plus one more per load.
        Pipelined core: one per instruction, one to fill the pipeline at the
        start, plus one per stall and one per wrong branch guess."""
        if self.pipelined:
            return self.instructions + 1 + self.stalls + self.mispredicts
        return self.instructions + self.loads

    def write_s(self, index, val):
        if index != 0:                                    # s0 always stays 0
            self.s[index] = val & 0xFFFFFFFF

    def addr(self, a):
        return a % len(self.mem)


# ---- Memory layout helpers ---------------------------------------------------
# Element i of an array lives in vector word (i // lanes), lane (i % lanes).
# With 8 lanes, elements 0..7 share word 0, elements 8..15 share word 1, ...

def store_array(model, base, values):
    """Place a list of numbers into memory starting at vector word 'base'."""
    lanes, mask = model.lanes, (1 << model.w) - 1
    for i, x in enumerate(values):
        model.mem[base + i // lanes][i % lanes] = x & mask


def load_array(model, base, count):
    """Read 'count' signed numbers back out of memory."""
    lanes = model.lanes
    return [to_signed(model.mem[base + i // lanes][i % lanes], model.w) for i in range(count)]


def hex_lines(words, w):
    """One hex line per vector, lane 0 in the lowest (rightmost) digits."""
    digits = w // 4
    return ["".join(f"{x:0{digits}x}" for x in reversed(word)) for word in words]


def memory_hex_lines(model):
    """The whole data memory, one line per word."""
    return hex_lines(model.mem, model.w)


def vector_hex_lines(model):
    """The eight vector registers v0..v7, one line each."""
    return hex_lines(model.v, model.w)


def mask_value(model):
    """The lane mask as one number: lane i is bit i."""
    return sum(bit << i for i, bit in enumerate(model.mask))
