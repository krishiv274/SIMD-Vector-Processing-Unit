// =============================================================================
// vec_control_unit.v - the "brain" of the single-cycle core
// =============================================================================
// The control unit does two jobs every clock cycle:
//
//   1. DECODE: read the current 32-bit instruction and turn it into simple
//      on/off signals for the rest of the chip ("write a vector register",
//      "use the scalar value", "write memory", ...). The decoding itself is
//      in vec_decoder.v, shared with the pipelined core.
//
//   2. SEQUENCE: work out which instruction comes next (the program counter,
//      PC), including jumps for loops and the extra wait for memory loads.
//
// Timing in plain words:
//   * Most instructions take 1 clock cycle.
//   * VLD (load from memory) takes 2 cycles. Memory on an FPGA (block RAM)
//     gives its data one clock AFTER it receives the address, so on the
//     first cycle we send the address and hold the PC, and on the second
//     cycle the data has arrived and is written into the register.
//   * HALT stops the program; the PC then stays where it is forever.
//
// The instruction memory also has a one-clock delay. To hide it, we hand
// it the address of the NEXT instruction (next_pc) a cycle early, so the
// right instruction is always waiting at the start of each cycle. That is
// why jumps cost no extra cycles.
// =============================================================================

module vec_control_unit #(
    parameter IADDR_W = 8                  // instruction address bits (2^8 = 256 instructions)
) (
    input  wire               clk,
    input  wire               rst,           // 1 = restart the program from address 0
    input  wire [31:0]        instr,         // the instruction being run this cycle
    input  wire               regs_differ,   // s[ra] != s[rb], used by BNE

    output wire [IADDR_W-1:0] imem_addr,     // which instruction to fetch next

    // Pieces cut out of the instruction
    output wire [2:0]         rd,            // destination register number
    output wire [2:0]         ra,            // source register A number
    output wire [2:0]         rb,            // register to read on port B
    output wire [31:0]        imm,           // the constant, widened to 32 bits

    // On/off signals for the datapath
    output wire [3:0]         alu_op,        // job for every lane
    output wire               use_scalar,    // lanes take operand B from a scalar register
    output wire               masked,        // only lanes switched on in the mask take part
    output wire               vreg_write,    // save a result into vector register rd
    output wire               vreg_from_mem, // ... and that result comes from memory
    output wire               sreg_write,    // save a result into scalar register rd
    output wire               sreg_from_sum, // ... and that result is the lane sum
    output wire               add_use_imm,   // scalar adder adds imm (else adds s[rb])
    output wire               mem_write,     // write a vector into data memory
    output wire               mask_write,    // save a new lane mask (VMSK)
    output wire               halted,        // the program has finished
    output wire               retire         // an instruction completed this cycle
);

    // ---- Decode ----------------------------------------------------------------
    wire is_vec, is_red, is_vld, is_vst, is_addi, is_add, is_bne, is_halt, is_vmsk;
    /* verilator lint_off UNUSEDSIGNAL */      // the read/write flags are for the pipelined core
    wire [2:0] rb_field;
    wire reads_va, reads_vb, reads_sa, reads_sb, writes_v, writes_s;
    /* verilator lint_on UNUSEDSIGNAL */

    vec_decoder u_dec (
        .instr(instr),
        .rd(rd), .ra(ra), .rb(rb_field), .rb_read(rb), .imm(imm),
        .alu_op(alu_op), .use_scalar(use_scalar), .masked(masked),
        .is_vec(is_vec), .is_red(is_red), .is_vld(is_vld), .is_vst(is_vst),
        .is_addi(is_addi), .is_add(is_add), .is_bne(is_bne), .is_halt(is_halt),
        .is_vmsk(is_vmsk),
        .reads_va(reads_va), .reads_vb(reads_vb), .reads_sa(reads_sa), .reads_sb(reads_sb),
        .writes_v(writes_v), .writes_s(writes_s)
    );

    // ---- State kept between cycles ------------------------------------------
    reg [IADDR_W-1:0] pc;          // address of the instruction running now
    reg               load_wait;   // 1 = second cycle of a VLD (data is here)
    reg               halted_r;    // 1 = HALT has been reached

    // The datapath may only change things while the program is running.
    wire running = !rst && !halted_r;

    // First cycle of a load: send the address, hold the PC.
    wire load_first_cycle = is_vld && !load_wait;

    // ---- Work out the next PC -----------------------------------------------
    wire                branch_taken = is_bne && regs_differ;
    wire [IADDR_W-1:0]  pc_plus_one  = pc + 1'b1;
    wire [IADDR_W-1:0]  pc_jump      = pc + imm[IADDR_W-1:0];   // imm can be negative (jump back)

    wire hold_pc = halted_r || is_halt || load_first_cycle;

    wire [IADDR_W-1:0] next_pc = hold_pc      ? pc :
                                 branch_taken ? pc_jump :
                                                pc_plus_one;

    // During reset, fetch address 0 so the first instruction is ready.
    assign imem_addr = rst ? {IADDR_W{1'b0}} : next_pc;

    always @(posedge clk) begin
        if (rst) begin
            pc        <= {IADDR_W{1'b0}};
            load_wait <= 1'b0;
            halted_r  <= 1'b0;
        end else begin
            pc        <= next_pc;
            load_wait <= load_first_cycle;
            if (is_halt)
                halted_r <= 1'b1;
        end
    end

    // ---- Control signals for the datapath -------------------------------------
    // Vector register write: every vector ALU op, and a load on its second cycle.
    assign vreg_write    = running && (is_vec || (is_vld && load_wait));
    assign vreg_from_mem = is_vld;

    // Scalar register write: ADDI, ADD and the reduce (sum) instructions.
    assign sreg_write    = running && (is_addi || is_add || is_red);
    assign sreg_from_sum = is_red;

    // The scalar adder computes s[ra] + imm for ADDI, VLD and VST,
    // and s[ra] + s[rb] for ADD.
    assign add_use_imm   = !is_add;

    assign mem_write     = running && is_vst;
    assign mask_write    = running && is_vmsk;

    assign halted        = halted_r;

    // An instruction completes on every running cycle except the first half of a load.
    assign retire        = running && !load_first_cycle;

endmodule
