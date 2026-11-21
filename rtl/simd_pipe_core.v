// =============================================================================
// simd_pipe_core.v - the SIMD vector processing unit, pipelined (Phase 2)
// =============================================================================
// Same ports, same instructions and same answers as simd_core.v, but the
// work of each instruction is split into four stages, like an assembly line:
//
//   F  fetch    the instruction memory (block RAM) reads the instruction
//   D  decode   decode it and read its registers
//   X  execute  lane mux, lane ALUs, scalar adder, data memory, branch check
//   W  write    adder tree for reduces, then save the result
//
// Each stage works on a different instruction at the same time. The clock
// only has to be slow enough for ONE stage, not for the whole path, so it
// can run much faster. (In the single-cycle core one clock had to cover
// memory + decode + registers + multiply + adder tree + write: 34.7 ns at
// 8 lanes on the ECP5 in the Phase 1 build.)
//
//   cycle:        1    2    3    4    5    6
//   instr 1       F    D    X    W
//   instr 2            F    D    X    W
//   instr 3                 F    D    X    W
//
// The price is that instructions now overlap, which causes three problems,
// each with a standard fix:
//
//   1. An instruction needs a result that is not saved yet
//      -> forwarding, or a one-cycle stall (hazard_unit.v), and
//         write-through register files (BYPASS = 1 in the register files).
//   2. A branch is only checked in X, but the next instruction has to be
//      fetched long before that
//      -> GUESS: a jump backwards (a loop) is guessed "taken", a jump
//         forwards "not taken". A wrong guess throws away the one wrong
//         instruction in D and costs one cycle.
//   3. HALT must not let any instruction after it change anything
//      -> D stops passing instructions on once HALT has gone into X.
//
// Loads: VLD sends its address in X, and the data arrives from block RAM at
// the start of W, where it is saved. So a load takes one cycle instead of
// two. The data is NOT forwarded to the next instruction: block RAM is slow
// to hand over its data (5.6 ns on the ECP5), and feeding it straight into
// the multiplier made that the slowest path in the chip (42.8 MHz instead of
// 50.6 MHz at 8 lanes). An instruction that needs a just-loaded register waits
// one cycle instead, as in the classic MIPS pipeline.
//
// Cycle count (checked against the golden model for every program):
//   cycles = instructions + 1 (filling the pipeline) + stalls + wrong guesses
// =============================================================================

module simd_pipe_core #(
    parameter LANES   = 8,     // elements processed per instruction
    parameter W       = 16,    // bits per element
    parameter IADDR_W = 8,     // instruction memory: 2^8  = 256 instructions
    parameter DADDR_W = 10     // data memory:        2^10 = 1024 vectors
) (
    input  wire               clk,
    input  wire               rst,

    // Instruction memory (data arrives one clock after the address)
    output wire [IADDR_W-1:0] imem_addr,
    input  wire [31:0]        imem_data,

    // Data memory: each memory word holds one whole vector (LANES*W bits)
    output wire [DADDR_W-1:0] dmem_addr,
    output wire [LANES*W-1:0] dmem_wdata,
    output wire               dmem_we,
    input  wire [LANES*W-1:0] dmem_rdata,

    // Scalar interface: lets the host read any scalar register
    input  wire [2:0]         debug_sel,
    output wire [31:0]        debug_data,

    // Status and performance counters
    output wire               halted,
    output reg  [31:0]        cycle_count,   // clock cycles the program has run
    output reg  [31:0]        instr_count    // instructions completed
);

    localparam [LANES-1:0] ALL_LANES = {LANES{1'b1}};

    reg halted_r;                  // 1 = HALT has finished
    assign halted = halted_r;

    // =========================================================================
    // Stage D: decode and read registers
    // =========================================================================
    // The block RAM output IS the instruction in D (it was fetched last cycle).
    reg  [IADDR_W-1:0] d_pc;       // its address
    reg                fetch_stop; // 1 = HALT has passed D: stop taking instructions
    wire               d_valid = !fetch_stop;

    wire [2:0]  d_rd, d_ra, d_rb_read;
    wire [31:0] d_imm;
    wire [3:0]  d_alu_op;
    wire d_use_scalar, d_masked, d_is_red, d_is_vld, d_is_vst,
         d_is_add, d_is_bne, d_is_halt, d_is_vmsk,
         d_reads_va, d_reads_vb, d_reads_sa, d_reads_sb, d_writes_v, d_writes_s;
    /* verilator lint_off UNUSEDSIGNAL */      // covered by writes_v / writes_s / rb_read
    wire [2:0]  d_rb;
    wire        d_is_vec, d_is_addi;
    /* verilator lint_on UNUSEDSIGNAL */

    vec_decoder u_dec (
        .instr(imem_data),
        .rd(d_rd), .ra(d_ra), .rb(d_rb), .rb_read(d_rb_read), .imm(d_imm),
        .alu_op(d_alu_op), .use_scalar(d_use_scalar), .masked(d_masked),
        .is_vec(d_is_vec), .is_red(d_is_red), .is_vld(d_is_vld), .is_vst(d_is_vst),
        .is_addi(d_is_addi), .is_add(d_is_add), .is_bne(d_is_bne), .is_halt(d_is_halt),
        .is_vmsk(d_is_vmsk),
        .reads_va(d_reads_va), .reads_vb(d_reads_vb), .reads_sa(d_reads_sa), .reads_sb(d_reads_sb),
        .writes_v(d_writes_v), .writes_s(d_writes_s)
    );

    // Register files. Their write ports are driven from stage W (further
    // down). BYPASS = 1: a read in D already sees what W writes this cycle.
    wire [LANES*W-1:0] d_va, d_vb;
    wire [31:0]        d_sa, d_sb;

    wire               w_vreg_we, w_sreg_we, w_mask_we;
    wire [LANES*W-1:0] w_vreg_data;
    wire [31:0]        w_sreg_data;
    wire [LANES-1:0]   lane_mask, lane_flags;

    // The X/W pipeline register, declared here because D needs it
    reg               w_valid, w_writes_v, w_writes_s, w_is_red, w_is_vld, w_is_vmsk;
    reg  [2:0]        w_rd;
    reg  [LANES-1:0]  w_lanes;
    reg  [LANES*W-1:0] w_vec;
    reg  [31:0]       w_scalar;

    vec_regfile #(.LANES(LANES), .W(W), .BYPASS(1)) u_vregs (
        .clk             (clk),
        .read_addr_a     (d_ra),
        .read_data_a     (d_va),
        .read_addr_b     (d_rb_read),
        .read_data_b     (d_vb),
        .write_en        (w_vreg_we),
        .write_lanes     (w_lanes),
        .write_addr      (w_rd),
        .write_data      (w_vreg_data),
        .mask            (lane_mask),
        .mask_write_en   (w_mask_we),
        .mask_write_data (lane_flags)
    );

    scalar_regfile #(.BYPASS(1)) u_sregs (
        .clk         (clk),
        .read_addr_a (d_ra),
        .read_data_a (d_sa),
        .read_addr_b (d_rb_read),
        .read_data_b (d_sb),
        .write_en    (w_sreg_we),
        .write_addr  (w_rd),
        .write_data  (w_sreg_data),
        .debug_addr  (debug_sel),
        .debug_data  (debug_data)
    );

    // Branch guess: a jump backwards (negative offset) is usually a loop, so
    // guess "taken" and fetch the target straight away.
    wire d_guess_taken = d_valid && d_is_bne && d_imm[15];

    // =========================================================================
    // Stage X: execute
    // =========================================================================
    // The D/X pipeline register: everything X needs, copied from D. The x_*
    // values are copied every cycle, even into a bubble, so they only count
    // when x_valid is 1. Everything that changes state (memory writes, branch
    // fixes, the X/W register's valid bit) checks x_valid first. The same goes
    // for w_* and w_valid one stage later.
    reg                x_valid;
    reg  [IADDR_W-1:0] x_pc;
    reg  [2:0]         x_rd, x_ra, x_rb;
    reg  [31:0]        x_imm;
    reg  [3:0]         x_alu_op;
    reg                x_use_scalar, x_masked, x_is_red, x_is_vld, x_is_vst, x_is_add,
                       x_is_bne, x_is_halt, x_is_vmsk, x_writes_v, x_writes_s, x_guess_taken;
    reg  [LANES*W-1:0] x_va, x_vb;
    reg  [31:0]        x_sa, x_sb;

    // ---- Hazards: forwarding and stalls ----------------------------------------
    wire stall, fwd_va, fwd_vb, fwd_sa, fwd_sb;

    hazard_unit u_hazard (
        .d_valid(d_valid), .d_ra(d_ra), .d_rb(d_rb_read),
        .d_reads_va(d_reads_va), .d_reads_vb(d_reads_vb),
        .d_reads_sa(d_reads_sa), .d_reads_sb(d_reads_sb),
        .x_valid(x_valid), .x_rd(x_rd), .x_ra(x_ra), .x_rb(x_rb),
        .x_is_red(x_is_red), .x_is_vld(x_is_vld), .x_masked_vec(x_masked && x_writes_v),
        .w_valid(w_valid), .w_rd(w_rd), .w_writes_v(w_writes_v), .w_writes_s(w_writes_s),
        .stall(stall), .fwd_va(fwd_va), .fwd_vb(fwd_vb), .fwd_sa(fwd_sa), .fwd_sb(fwd_sb)
    );

    // The operands X really uses: the copies read in D, or the newer value
    // that W is writing right now (forwarding).
    wire [LANES*W-1:0] va = fwd_va ? w_vec : x_va;      // w_vec: the lane results in W
    wire [LANES*W-1:0] vb = fwd_vb ? w_vec : x_vb;
    wire [31:0]        sa = fwd_sa ? w_scalar    : x_sa;
    wire [31:0]        sb = fwd_sb ? w_scalar    : x_sb;

    // ---- Lanes ------------------------------------------------------------------
    wire [LANES*W-1:0] lane_b, alu_out;

    lane_mux #(.LANES(LANES), .W(W)) u_lane_mux (
        .use_scalar (x_use_scalar),
        .vector_b   (vb),
        .scalar_b   (sb[W-1:0]),
        .lane_b     (lane_b)
    );

    vec_alu_array #(.LANES(LANES), .W(W)) u_alus (
        .op (x_alu_op),
        .a  (va),
        .b  (lane_b),
        .y  (alu_out)
    );

    // Which lanes this instruction may change (masked: only the switched-on ones).
    // If VMSK is in W right now, the write-through register file already gives
    // its new mask.
    wire [LANES-1:0] x_lanes = x_masked ? lane_mask : ALL_LANES;

    // ---- Scalar adder, data memory ----------------------------------------------
    // s[ra] + imm for ADDI, VLD and VST; s[ra] + s[rb] for ADD.
    wire [31:0] scalar_sum = sa + (x_is_add ? sb : x_imm);

    assign dmem_addr  = scalar_sum[DADDR_W-1:0];
    assign dmem_wdata = vb;                                 // VST stores vector rb
    assign dmem_we    = !rst && x_valid && x_is_vst;

    // ---- Branch check -------------------------------------------------------------
    wire                x_taken    = x_is_bne && (sa != sb);
    wire                wrong_guess = x_valid && x_is_bne && (x_taken != x_guess_taken);
    wire [IADDR_W-1:0]  x_target   = x_pc + x_imm[IADDR_W-1:0];
    wire [IADDR_W-1:0]  x_next     = x_pc + 1'b1;
    wire [IADDR_W-1:0]  fix_addr   = x_taken ? x_target : x_next;   // where we should have gone

    // =========================================================================
    // Stage W: add up the lanes and save the result
    // =========================================================================
    wire [LANES*W-1:0] w_vector_result;
    wire [31:0]        w_sum;

    result_combiner #(.LANES(LANES), .W(W)) u_combiner (
        .lane_results  (w_vec),
        .lane_enable   (w_lanes),            // masked reduce: switched-off lanes add 0
        .vector_result (w_vector_result),
        .sum_result    (w_sum),
        .lane_flags    (lane_flags)          // VMSK: the non-zero lanes become the mask
    );

    // A load's data arrives from block RAM now; everything else was computed in X.
    assign w_vreg_data = w_is_vld ? dmem_rdata : w_vector_result;
    assign w_sreg_data = w_is_red ? w_sum      : w_scalar;

    wire w_commit = w_valid && !rst;
    assign w_vreg_we = w_commit && w_writes_v;
    assign w_sreg_we = w_commit && w_writes_s;
    assign w_mask_we = w_commit && w_is_vmsk;

    // =========================================================================
    // Fetch: which instruction comes next
    // =========================================================================
    // In order of importance:
    //   reset        start again at 0
    //   wrong guess  go where the branch in X really goes
    //   stall / stop fetch the instruction in D again (it waits)
    //   guess taken  jump to the target of the backward branch in D
    //   otherwise    the next instruction
    wire d_moves = d_valid && !stall && !wrong_guess;        // D passes its instruction to X

    wire [IADDR_W-1:0] fetch_addr =
        rst                    ? {IADDR_W{1'b0}} :
        wrong_guess            ? fix_addr :
        (stall || !d_valid)    ? d_pc :
        d_guess_taken          ? d_pc + d_imm[IADDR_W-1:0] :
                                 d_pc + 1'b1;

    assign imem_addr = fetch_addr;

    // =========================================================================
    // The pipeline registers move forward on every clock edge
    // =========================================================================
    always @(posedge clk) begin
        d_pc <= fetch_addr;

        if (rst) begin
            fetch_stop <= 1'b0;
            halted_r   <= 1'b0;
            x_valid    <= 1'b0;
            w_valid    <= 1'b0;
        end else begin
            // Once HALT leaves D, nothing after it may enter the pipeline.
            if (d_moves && d_is_halt)
                fetch_stop <= 1'b1;
            // HALT finishes in X: every older instruction is in W and is
            // being saved at this same edge.
            if (x_valid && x_is_halt)
                halted_r <= 1'b1;

            // D -> X (a stall or a wrong guess sends a bubble instead)
            x_valid <= d_moves;
            w_valid <= x_valid;
        end

        // The data moves every cycle; the valid bits say whether it counts.
        x_pc          <= d_pc;
        x_rd          <= d_rd;
        x_ra          <= d_ra;
        x_rb          <= d_rb_read;
        x_imm         <= d_imm;
        x_alu_op      <= d_alu_op;
        x_use_scalar  <= d_use_scalar;
        x_masked      <= d_masked;
        x_is_red      <= d_is_red;
        x_is_vld      <= d_is_vld;
        x_is_vst      <= d_is_vst;
        x_is_add      <= d_is_add;
        x_is_bne      <= d_is_bne;
        x_is_halt     <= d_is_halt;
        x_is_vmsk     <= d_is_vmsk;
        x_writes_v    <= d_writes_v;
        x_writes_s    <= d_writes_s;
        x_guess_taken <= d_guess_taken;
        x_va          <= d_va;
        x_vb          <= d_vb;
        x_sa          <= d_sa;
        x_sb          <= d_sb;

        w_rd          <= x_rd;
        w_writes_v    <= x_writes_v;
        w_writes_s    <= x_writes_s;
        w_is_red      <= x_is_red;
        w_is_vld      <= x_is_vld;
        w_is_vmsk     <= x_is_vmsk;
        w_lanes       <= x_lanes;
        w_vec         <= alu_out;
        w_scalar      <= scalar_sum;
    end

    // ---- Performance counters ---------------------------------------------------
    // An instruction counts as done when it passes X (HALT included), the
    // same count the single-cycle core gives.
    always @(posedge clk) begin
        if (rst) begin
            cycle_count <= 32'd0;
            instr_count <= 32'd0;
        end else if (!halted_r) begin
            cycle_count <= cycle_count + 1'b1;
            if (x_valid)
                instr_count <= instr_count + 1'b1;
        end
    end

endmodule
