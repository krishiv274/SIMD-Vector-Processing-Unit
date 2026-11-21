// =============================================================================
// hazard_unit.v - keeps the pipelined core's answers right
// =============================================================================
// In the pipelined core (simd_pipe_core.v) four instructions are in flight
// at once, one per stage. Fetch (F) only reads the instruction memory; the
// three stages that matter here are:
//
//     D (decode)  reads its registers
//     X (execute) the lanes, the scalar adder and memory do the work
//     W (write)   the lane sum is added up, the result is saved
//
// The problem: an instruction often needs the answer of the one just ahead
// of it, and that answer is not in the register file yet. This block solves
// it in two ways.
//
// 1. FORWARDING. When the instruction in X needs a register that the
//    instruction in W is about to write, take the value straight from W
//    instead of the stale copy read earlier. Example:
//        ADDI s1, s1, 1      <- in W: s1 + 1 is ready
//        BNE  s1, s2, loop   <- in X: uses the new s1 from W at once
//    (An answer two instructions ahead needs nothing here: the register
//    files are "write-through", so D already reads the value W is writing.)
//
// 2. STALLING (waiting one cycle). Three kinds of answer are not forwarded:
//      * the sum of a reduce (VDOT, VREDADD ...): the adder tree only adds
//        the lanes up during W
//      * a masked vector result: its switched-off lanes must keep the old
//        register value, and only the register file has that
//      * loaded data (VLD): it only arrives from block RAM during W, and
//        block RAM is slow to hand it over, so sending it straight on into
//        the multiplier would make the whole clock slower
//    If the instruction in D needs one of these from the instruction in X,
//    D waits one cycle (a "bubble" goes into X instead). One cycle later the
//    producer is in W, and the write-through register file hands D the value.
// =============================================================================

module hazard_unit (
    // The instruction in D: which registers it reads
    input  wire       d_valid,
    input  wire [2:0] d_ra,
    input  wire [2:0] d_rb,          // the register read on port B
    input  wire       d_reads_va, d_reads_vb, d_reads_sa, d_reads_sb,

    // The instruction in X: what it will write
    input  wire       x_valid,
    input  wire [2:0] x_rd,
    input  wire [2:0] x_ra,
    input  wire [2:0] x_rb,
    input  wire       x_is_red,      // writes s[rd] with the lane sum
    input  wire       x_is_vld,      // writes v[rd] with data from memory
    input  wire       x_masked_vec,  // writes v[rd] with some lanes switched off

    // The instruction in W: what it is writing now
    input  wire       w_valid,
    input  wire [2:0] w_rd,
    input  wire       w_writes_v, w_writes_s,

    output wire       stall,         // 1 = D waits this cycle
    output wire       fwd_va,        // 1 = X takes operand va from W
    output wire       fwd_vb,
    output wire       fwd_sa,
    output wire       fwd_sb
);

    // ---- 1. Forwarding: X reads a register that W is writing -----------------
    // s0 is never written (it is always 0), so it is never forwarded.
    // Load data never needs forwarding: an instruction that reads a loaded
    // register waits one cycle (below), so by the time it reaches X the load
    // has already left W.
    wire w_vec    = w_valid && w_writes_v;
    wire w_scalar = w_valid && w_writes_s && (w_rd != 3'd0);

    assign fwd_va = w_vec    && (w_rd == x_ra);
    assign fwd_vb = w_vec    && (w_rd == x_rb);
    assign fwd_sa = w_scalar && (w_rd == x_ra);
    assign fwd_sb = w_scalar && (w_rd == x_rb);

    // ---- 2. Stalling: D needs a result from X that will not be ready ----------
    wire needs_sum    = x_is_red && (x_rd != 3'd0) &&
                        ((d_reads_sa && d_ra == x_rd) || (d_reads_sb && d_rb == x_rd));
    wire needs_masked = x_masked_vec &&
                        ((d_reads_va && d_ra == x_rd) || (d_reads_vb && d_rb == x_rd));
    wire needs_load   = x_is_vld &&
                        ((d_reads_va && d_ra == x_rd) || (d_reads_vb && d_rb == x_rd));

    assign stall = d_valid && x_valid && (needs_sum || needs_masked || needs_load);

endmodule
