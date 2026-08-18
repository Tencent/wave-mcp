// Testbench for the four-state design.
// Drives real X / Z / contention scenarios and dumps VCD via Icarus (4-state).
// $dumpvars depth is limited for u_hidden -> partial-dump scenario.
`timescale 1ns/1ps

module tb_fourstate;
  reg        clk;
  reg        rst_n;
  reg  [7:0] din;
  reg        drv_a_en, drv_b_en;
  reg  [3:0] a_val, b_val;
  wire [7:0] dout;
  wire [3:0] bus;
  wire [3:0] hidden_cnt;

  fourstate_top dut (
    .clk        (clk),
    .rst_n      (rst_n),
    .din        (din),
    .drv_a_en   (drv_a_en),
    .drv_b_en   (drv_b_en),
    .a_val      (a_val),
    .b_val      (b_val),
    .dout       (dout),
    .bus        (bus),
    .hidden_cnt (hidden_cnt)
  );

  initial clk = 1'b0;
  always #5 clk = ~clk;

  initial begin
    // dump tb + dut + u_xprop/u_tri fully, but u_hidden only 1 level
    // (its internal "shadow" reg is NOT in the waveform).
    $dumpfile("fourstate.vcd");
    $dumpvars(0, tb_fourstate.dut.u_xprop);
    $dumpvars(0, tb_fourstate.dut.u_tri);
    $dumpvars(1, tb_fourstate.dut.u_hidden);
    $dumpvars(1, tb_fourstate.dut);
    $dumpvars(1, tb_fourstate);

    // phase 1 (0-40ns): everything undriven -> din=X, regs=X, bus=Z
    rst_n    = 1'b0;
    drv_a_en = 1'b0;
    drv_b_en = 1'b0;
    a_val    = 4'hA;
    b_val    = 4'h5;
    // din intentionally left X
    #42;

    // phase 2 (42-80ns): reset released, din still X -> X propagates
    rst_n = 1'b1;
    #38;

    // phase 3 (80-120ns): drive din -> X clears through the pipeline
    din = 8'h3C;
    #40;

    // phase 4 (120-160ns): single tri-state driver A -> bus = a_val
    drv_a_en = 1'b1;
    #40;

    // phase 5 (160-200ns): both drivers on with conflicting values -> bus = X
    drv_b_en = 1'b1;
    #40;

    // phase 6 (200-240ns): both off again -> bus floats back to Z
    drv_a_en = 1'b0;
    drv_b_en = 1'b0;
    #40;

    $finish;
  end
endmodule
