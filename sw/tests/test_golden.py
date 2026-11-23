"""
Self-checks for the golden model.

Run:  python3 -m unittest discover -s sw/tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import golden  # noqa: E402

# Some checks assemble a program, so they wait until the assembler and the
# programs are in the repository.
try:
    import vasm  # noqa: E402
except ImportError:
    vasm = None
VADD = Path(__file__).resolve().parents[1] / "programs" / "vadd.s"
needs_assembler = unittest.skipIf(vasm is None or not VADD.exists(),
                                  "needs sw/vasm.py and sw/programs/vadd.s")


class GoldenModelTests(unittest.TestCase):

    def test_alu_corner_cases(self):
        add, sra, slt, sltu, mul = (golden.ALU_OPS.index(n) for n in
                                    ["ADD", "SRA", "SLT", "SLTU", "MUL"])
        self.assertEqual(golden.alu(add, 0xFFFF, 1), 0)          # wraps around
        self.assertEqual(golden.alu(sra, 0x8000, 15), 0xFFFF)    # keeps the sign
        self.assertEqual(golden.alu(slt, 0xFFFF, 1), 0xFFFF)     # -1 < 1
        self.assertEqual(golden.alu(sltu, 0xFFFF, 1), 0)         # 65535 > 1
        self.assertEqual(golden.alu(mul, 300, 300), 90000 & 0xFFFF)

    def test_saturating_jobs(self):
        adds, subs = golden.ALU_OPS.index("ADDS"), golden.ALU_OPS.index("SUBS")
        self.assertEqual(golden.alu(adds, 0x7FFF, 1), 0x7FFF)       # 32767 + 1 sticks at 32767
        self.assertEqual(golden.alu(adds, 0x8000, 0xFFFF), 0x8000)  # -32768 + -1 sticks at -32768
        self.assertEqual(golden.alu(adds, 0xC000, 0xC000), 0x8000)  # -16384 * 2 just fits
        self.assertEqual(golden.alu(subs, 0x8000, 1), 0x8000)       # -32768 - 1
        self.assertEqual(golden.alu(subs, 0x7FFF, 0xFFFF), 0x7FFF)  # 32767 - (-1)
        self.assertEqual(golden.alu(subs, 5, 7), 0xFFFE)            # no overflow: plain -2

    @needs_assembler
    def test_s0_stays_zero(self):
        model = golden.SimdModel(lanes=1)
        model.run(vasm.assemble("ADDI s0, s0, 5\nHALT"))
        self.assertEqual(model.s[0], 0)

    @needs_assembler
    def test_vadd_program(self):
        source = VADD.read_text()
        for lanes in (1, 8):
            model = golden.SimdModel(lanes)
            a, b = list(range(16)), list(range(100, 116))
            golden.store_array(model, 0, a)
            golden.store_array(model, 100, b)
            words = vasm.assemble(source, dict(A=0, B=100, C=200, ITER=16 // lanes))
            instructions, cycles = model.run(words)
            self.assertEqual(golden.load_array(model, 200, 16), [x + y for x, y in zip(a, b)])
            self.assertEqual(cycles, instructions + 2 * (16 // lanes))   # loads cost 1 extra cycle


    @needs_assembler
    def test_masked_merge_and_reduce(self):
        model = golden.SimdModel(lanes=4)
        golden.store_array(model, 0, [5, -3, 0, 7])       # word 0
        golden.store_array(model, 1, [10, 20, 30, 40])    # word 1
        model.run(vasm.assemble("""
            VLD  v1, 0(s0)
            VLD  v2, 1(s0)
            VMSK v1                 # on where v1 is not zero: lanes 0, 1, 3
            VADD.M v2, v2, v1       # lane 2 keeps its old value, 30
            VREDADD.M s1, v1, v0    # 5 - 3 + 7 (lane 2 left out)
            HALT"""))
        self.assertEqual(model.mask, [1, 1, 0, 1])
        self.assertEqual([golden.to_signed(x, 16) for x in model.v[2]], [15, 17, 30, 47])
        self.assertEqual(model.s[1], 9)

    @needs_assembler
    def test_pipelined_cycle_count(self):
        # vadd: one load-use stall per pass (VADD needs the register loaded
        # just before it); the loop branch is guessed "taken" and is wrong
        # once, at the exit. Plus one cycle to fill the pipeline.
        for lanes in (1, 8):
            passes = 16 // lanes
            model = golden.SimdModel(lanes, pipelined=True)
            words = vasm.assemble(VADD.read_text(), dict(A=0, B=100, C=200, ITER=passes))
            instructions, cycles = model.run(words)
            self.assertEqual((model.stalls, model.mispredicts), (passes, 1))
            self.assertEqual(cycles, instructions + 1 + passes + 1)

    @needs_assembler
    def test_reduce_then_use_stalls(self):
        words = vasm.assemble("VDOT s1, v1, v2\nADD s2, s1, s1\nVDOT s0, v1, v2\nADD s3, s0, s0\nHALT")
        model = golden.SimdModel(lanes=8, pipelined=True)
        model.run(words)
        self.assertEqual(model.stalls, 1)          # the sum into s0 is never waited for


if __name__ == "__main__":
    unittest.main()
