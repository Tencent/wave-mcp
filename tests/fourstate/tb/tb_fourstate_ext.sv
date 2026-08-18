// Testbench for fourstate_ext_top: drives X through guards, case selectors,
// latch gates, per-bit tri-states and a wired-OR net.
`timescale 1ns/1ps

module tb_fourstate_ext;
  reg        clk;
  reg        rst_n, en, lo_en, lg, wa_en, wb_en;
  reg  [7:0] d;
  reg  [1:0] sel;
  reg  [3:0] w0, w1, w2, lo_val, hi_val, ld, gen_en, gen_v;
  wire [7:0] q, mixed;
  wire [3:0] y, yz, lq, go;
  wire       w;

  fourstate_ext_top dut (
    .clk(clk), .rst_n(rst_n), .en(en), .d(d), .sel(sel),
    .w0(w0), .w1(w1), .w2(w2),
    .lo_en(lo_en), .lo_val(lo_val), .hi_val(hi_val),
    .lg(lg), .ld(ld), .gen_en(gen_en), .gen_v(gen_v),
    .wa_en(wa_en), .wb_en(wb_en),
    .q(q), .y(y), .yz(yz), .mixed(mixed), .lq(lq), .go(go), .w(w)
  );

  initial clk = 1'b0;
  always #5 clk = ~clk;

  initial begin
    $dumpfile("fourstate_ext.vcd");
    $dumpvars(0, tb_fourstate_ext);

    // phase 1 (0-40ns): rst_n / sel / lg all X (undriven), data known
    en     = 1'b0;
    d      = 8'hA7;
    w0     = 4'h1; w1 = 4'h2; w2 = 4'h3;
    lo_en  = 1'b0;
    lo_val = 4'hC; hi_val = 4'hE;
    ld     = 4'h9;
    gen_en = 4'b0000; gen_v = 4'b1111;
    wa_en  = 1'b0; wb_en = 1'b0;
    #40;

    // phase 2 (40-80ns): reset asserted -> q clears; sel=X keeps y on default
    rst_n = 1'b0;
    #40;

    // phase 3 (80-120ns): reset off, en=1 -> q loads d; sel=00 -> y=w0
    rst_n = 1'b1;
    en    = 1'b1;
    sel   = 2'b00;
    lg    = 1'b1;              // latch transparent: lq = ld
    #40;

    // phase 4 (120-160ns): sel=01 (casez 0? still w0); lo_en on -> low nibble
    // finally written; per-bit gen: only bits 0,2 driven -> others Z
    sel    = 2'b01;
    lo_en  = 1'b1;
    gen_en = 4'b0101;
    #40;

    // phase 5 (160-200ns): latch closes (lg=0) holding last value; then gate
    // goes X -> latch output X per X-pessimism; wired-OR: A drives 1
    lg    = 1'b0;
    #10;
    lg    = 1'bx;              // explicit X on the latch gate
    wa_en = 1'b1;
    #30;

    // phase 6 (200-240ns): wired-OR contention: A=1, B=0 -> wor resolves 1
    wb_en = 1'b1;
    #40;

    $finish;
  end
endmodule
