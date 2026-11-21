// =============================================================================
// tb_simd_core.v - system test: runs a whole program on the complete core
// =============================================================================
// This testbench plays the role of the "computer around the chip":
//   * it holds the instruction memory and the data memory
//   * it loads a program and its input data from hex files
//   * it releases reset, lets the core run until HALT, keeps the clock going
//     for 50 more cycles (nothing may be written after HALT), and then
//   * compares EVERYTHING with the Python golden model:
//       - every word of data memory
//       - every scalar register (read through the debug port)
//       - every vector register and the lane mask
//       - the number of instructions and clock cycles
//
// The hex files are made by sw/run_workloads.py, which also runs this
// testbench for every workload and lane count. Files are passed in as
// "plusargs" on the command line:
//
//   vvp tb.vvp +prog=prog.hex +data=data.hex +expect_mem=mem.hex
//              +expect_scalar=scalar.hex [+vcd=wave.vcd] [+reset_at=N]
//
// +reset_at=N presses reset N cycles into the run, for 3 cycles, as the
// board's reset button would. Nothing may be written to memory or to any
// register while reset is held; then the program starts again from the
// beginning. (run_workloads.py uses it with programs that give the same
// result when they are started again, so the expected files do not change.)
//
// LANES is a parameter, so one testbench covers the scalar (LANES = 1)
// and SIMD (LANES = 2, 4, 8) versions. PIPELINED picks the core:
// 0 = single-cycle (simd_core.v), 1 = pipelined (simd_pipe_core.v). Both
// have the same ports, so everything else here is shared.
// =============================================================================

`timescale 1ns/1ps

module tb_simd_core;

    parameter  LANES      = 8;
    parameter  PIPELINED  = 0;
    localparam W          = 16;
    localparam IADDR_W    = 8;
    localparam DADDR_W    = 10;
    localparam IDEPTH     = 1 << IADDR_W;
    localparam DDEPTH     = 1 << DADDR_W;
    localparam MAX_CYCLES = 200000;      // give up if HALT never comes
    localparam AFTER_HALT = 50;          // clocks to keep running after HALT

    // ---- Clock and reset ---------------------------------------------------------
    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;                // 10 ns period = 100 MHz in simulation

    // ---- The core ------------------------------------------------------------------
    wire [IADDR_W-1:0] imem_addr;
    reg  [31:0]        imem_data;
    wire [DADDR_W-1:0] dmem_addr;
    wire [LANES*W-1:0] dmem_wdata;
    wire               dmem_we;
    reg  [LANES*W-1:0] dmem_rdata;
    reg  [2:0]         debug_sel = 3'd0;
    wire [31:0]        debug_data;
    wire               halted;
    wire [31:0]        cycle_count, instr_count;

    // Both branches are called "core", so the checks below can look inside
    // with the same name (core.dut.u_vregs ...) whichever core is built.
    generate
        if (PIPELINED) begin : core
            simd_pipe_core #(.LANES(LANES), .W(W), .IADDR_W(IADDR_W), .DADDR_W(DADDR_W)) dut (
                .clk(clk), .rst(rst),
                .imem_addr(imem_addr), .imem_data(imem_data),
                .dmem_addr(dmem_addr), .dmem_wdata(dmem_wdata), .dmem_we(dmem_we), .dmem_rdata(dmem_rdata),
                .debug_sel(debug_sel), .debug_data(debug_data),
                .halted(halted), .cycle_count(cycle_count), .instr_count(instr_count)
            );
        end else begin : core
            simd_core #(.LANES(LANES), .W(W), .IADDR_W(IADDR_W), .DADDR_W(DADDR_W)) dut (
                .clk(clk), .rst(rst),
                .imem_addr(imem_addr), .imem_data(imem_data),
                .dmem_addr(dmem_addr), .dmem_wdata(dmem_wdata), .dmem_we(dmem_we), .dmem_rdata(dmem_rdata),
                .debug_sel(debug_sel), .debug_data(debug_data),
                .halted(halted), .cycle_count(cycle_count), .instr_count(instr_count)
            );
        end
    endgenerate

    // ---- Memories --------------------------------------------------------------------
    // Both behave like FPGA block RAM: the data for an address appears one
    // clock edge after the address is given.
    reg [31:0]        imem       [0:IDEPTH-1];
    reg [LANES*W-1:0] dmem       [0:DDEPTH-1];
    reg [LANES*W-1:0] expect_mem [0:DDEPTH-1];
    reg [LANES*W-1:0] expect_vec [0:7];      // v0..v7
    reg [31:0]        expect_scalar [0:10];  // s0..s7, instructions, cycles, lane mask

    always @(posedge clk) begin
        imem_data  <= imem[imem_addr];
        if (dmem_we)
            dmem[dmem_addr] <= dmem_wdata;
        dmem_rdata <= dmem[dmem_addr];
    end

    // ---- The test ----------------------------------------------------------------------
    reg [8*256-1:0] prog_file, data_file, mem_file, vec_file, scalar_file, vcd_file;
    integer errors = 0;
    integer i;
    integer reset_at;
    reg     mid_reset = 1'b0;          // 1 while the reset pressed during the run is held

    // While that reset is held, no write of any kind may happen.
    always @(posedge clk)
        if (mid_reset && (dmem_we === 1'b1 || core.dut.u_vregs.write_en === 1'b1 ||
                          core.dut.u_vregs.mask_write_en === 1'b1 ||
                          core.dut.u_sregs.write_en === 1'b1)) begin
            errors = errors + 1;
            $display("  WRITE DURING RESET at %0t", $time);
        end

    initial begin
        if (!$value$plusargs("prog=%s", prog_file) ||
            !$value$plusargs("data=%s", data_file) ||
            !$value$plusargs("expect_mem=%s", mem_file) ||
            !$value$plusargs("expect_vec=%s", vec_file) ||
            !$value$plusargs("expect_scalar=%s", scalar_file)) begin
            $display("usage: +prog= +data= +expect_mem= +expect_vec= +expect_scalar= [+vcd=]");
            $display("TEST FAILED");
            $finish;
        end
        if ($value$plusargs("vcd=%s", vcd_file)) begin
            $dumpfile(vcd_file);
            $dumpvars(0, tb_simd_core);
        end

        $readmemh(prog_file,   imem);
        $readmemh(data_file,   dmem);
        $readmemh(mem_file,    expect_mem);
        $readmemh(vec_file,    expect_vec);
        $readmemh(scalar_file, expect_scalar);

        // Hold reset for two clocks, then let the program run.
        repeat (2) @(posedge clk);
        @(negedge clk) rst = 1'b0;

        // Optionally press reset again in the middle of the run.
        if ($value$plusargs("reset_at=%d", reset_at) && reset_at > 0) begin
            repeat (reset_at) @(posedge clk);
            @(negedge clk) begin rst = 1'b1; mid_reset = 1'b1; end
            repeat (3) @(posedge clk);
            @(negedge clk) begin rst = 1'b0; mid_reset = 1'b0; end
        end

        // Wait for HALT (or give up).
        i = 0;
        while (!halted && i < MAX_CYCLES) begin
            @(posedge clk);
            i = i + 1;
        end
        @(negedge clk);

        if (!halted) begin
            $display("  TIMEOUT: no HALT after %0d cycles", MAX_CYCLES);
            errors = errors + 1;
        end

        // HALT must really stop the core: keep the clock running and make sure
        // nothing is written to memory. Anything else that changed (registers,
        // counters) shows up in the checks below.
        for (i = 0; i < AFTER_HALT; i = i + 1) begin
            @(negedge clk);
            if (dmem_we !== 1'b0) begin
                errors = errors + 1;
                $display("  MEMORY WRITE %0d cycles after HALT", i + 1);
            end
        end

        // 1. Data memory must match word for word.
        for (i = 0; i < DDEPTH; i = i + 1)
            if (dmem[i] !== expect_mem[i]) begin
                errors = errors + 1;
                if (errors <= 10)
                    $display("  MEMORY MISMATCH word %0d: got %h want %h", i, dmem[i], expect_mem[i]);
            end

        // 2. Scalar registers, read through the debug port (the scalar interface).
        for (i = 0; i < 8; i = i + 1) begin
            debug_sel = i;
            #1;
            if (debug_data !== expect_scalar[i]) begin
                errors = errors + 1;
                $display("  SCALAR MISMATCH s%0d: got %h want %h", i, debug_data, expect_scalar[i]);
            end
        end

        // 3. Vector registers must match too (catches a stray write to a register
        //    that no later instruction reads).
        for (i = 0; i < 8; i = i + 1)
            if (core.dut.u_vregs.regs[i] !== expect_vec[i]) begin
                errors = errors + 1;
                $display("  VECTOR MISMATCH v%0d: got %h want %h", i, core.dut.u_vregs.regs[i], expect_vec[i]);
            end
        if (core.dut.u_vregs.mask_reg !== expect_scalar[10][LANES-1:0]) begin
            errors = errors + 1;
            $display("  LANE MASK MISMATCH: got %b want %b", core.dut.u_vregs.mask_reg,
                     expect_scalar[10][LANES-1:0]);
        end

        // 4. Instruction and cycle counts must match the golden model exactly.
        if (instr_count !== expect_scalar[8]) begin
            errors = errors + 1;
            $display("  INSTRUCTION COUNT: got %0d want %0d", instr_count, expect_scalar[8]);
        end
        if (cycle_count !== expect_scalar[9]) begin
            errors = errors + 1;
            $display("  CYCLE COUNT: got %0d want %0d", cycle_count, expect_scalar[9]);
        end

        // One machine-readable line for run_workloads.py.
        $display("PERF lanes=%0d cycles=%0d instructions=%0d pipelined=%0d",
                 LANES, cycle_count, instr_count, PIPELINED);
        if (errors == 0) $display("TEST PASSED");
        else             $display("TEST FAILED (%0d errors)", errors);
        $finish;
    end

endmodule
