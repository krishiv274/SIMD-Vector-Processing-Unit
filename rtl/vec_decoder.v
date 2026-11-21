// =============================================================================
// vec_decoder.v - reads one instruction and says what it needs
// =============================================================================
// Both cores (the single-cycle one and the pipelined one) use this block, so
// they can never disagree about what an instruction means.
//
// It has no clock and no memory: 32 instruction bits go in, and out come
//   * the pieces of the instruction (register numbers, the constant)
//   * the job for the lanes and where operand B comes from
//   * "what kind of instruction is this?" flags
//   * which registers it reads and which kind it writes. The pipelined core
//     uses these to spot when an instruction needs a result that an older
//     instruction has not finished yet.
// =============================================================================

`include "simd_defs.vh"

module vec_decoder (
    /* verilator lint_off UNUSEDSIGNAL */      // instruction bits are only used where needed
    input  wire [31:0] instr,
    /* verilator lint_on UNUSEDSIGNAL */

    // Pieces cut out of the instruction
    output wire [2:0]  rd,            // destination register number
    output wire [2:0]  ra,            // source register A number
    output wire [2:0]  rb,            // source register B number, as written
    output wire [2:0]  rb_read,       // register to read on port B (0 for VMSK, see below)
    output wire [31:0] imm,           // the constant, widened to 32 bits

    // Lane work
    output wire [3:0]  alu_op,        // job for every lane
    output wire        use_scalar,    // operand B is a scalar copied to every lane
    output wire        masked,        // only switched-on lanes take part

    // What kind of instruction
    output wire        is_vec,        // vector ALU op: writes a vector register from the lanes
    output wire        is_red,        // reduce: writes a scalar register with the lane sum
    output wire        is_vld,
    output wire        is_vst,
    output wire        is_addi,
    output wire        is_add,
    output wire        is_bne,
    output wire        is_halt,
    output wire        is_vmsk,

    // Which registers it reads, and which kind it writes
    output wire        reads_va,      // vector register ra
    output wire        reads_vb,      // vector register rb
    output wire        reads_sa,      // scalar register ra
    output wire        reads_sb,      // scalar register rb
    output wire        writes_v,      // vector register rd
    output wire        writes_s       // scalar register rd
);

    // ---- Cut the instruction into its fields ---------------------------------
    wire [5:0] opcode = instr[31:26];
    wire [1:0] iclass = opcode[5:4];

    assign rd  = instr[25:23];
    assign ra  = instr[22:20];
    assign rb  = instr[19:17];
    // Sign-extend: copy bit 15 into the top 16 bits, so -1 stays -1.
    assign imm = {{16{instr[15]}}, instr[15:0]};

    // ---- Which instruction is it? -------------------------------------------
    wire is_vv = (iclass == `CLASS_VV);
    wire is_vx = (iclass == `CLASS_VX);
    assign is_vec  = is_vv || is_vx;
    assign is_red  = (iclass == `CLASS_RED);
    assign is_vld  = (opcode == `OP_VLD);
    assign is_vst  = (opcode == `OP_VST);
    assign is_addi = (opcode == `OP_ADDI);
    assign is_add  = (opcode == `OP_ADD);
    assign is_bne  = (opcode == `OP_BNE);
    assign is_halt = (opcode == `OP_HALT);
    assign is_vmsk = (opcode == `OP_VMSK);

    // ---- Lane work -----------------------------------------------------------
    // VMSK makes the lanes pass va through unchanged (va OR 0), and the
    // result combiner turns "is this lane non-zero?" into the new mask. The
    // 0 comes from scalar register s0, which always reads 0: VMSK reads port
    // B at register 0, whatever its rb field says.
    assign alu_op     = is_vmsk ? `ALU_OR : opcode[3:0];
    assign use_scalar = is_vx || is_vmsk;
    assign rb_read    = is_vmsk ? 3'd0 : rb;

    // Bit 16 only means "masked" for the lane instructions (classes 00-10).
    assign masked = instr[16] && (is_vec || is_red);

    // ---- Reads and writes ----------------------------------------------------
    assign reads_va = is_vec || is_red || is_vmsk;
    assign reads_vb = is_vv  || is_red || is_vst;            // VST stores vector rb
    assign reads_sa = is_vld || is_vst || is_addi || is_add || is_bne;   // address or sum
    assign reads_sb = is_vx  || is_add || is_bne;
    assign writes_v = is_vec || is_vld;
    assign writes_s = is_red || is_addi || is_add;

endmodule
