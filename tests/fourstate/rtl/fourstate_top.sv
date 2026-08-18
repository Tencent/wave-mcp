// Four-state (0/1/X/Z) test design for wave-mcp.
// Deliberately produces REAL X and Z values in the waveform:
//   - un-reset registers stay X until first meaningful clock edge
//   - tri-state bus floats to Z when no driver enabled
//   - bus contention (two enabled drivers with conflicting values) -> X
//   - X propagation through combinational logic
// Written in conservative Verilog-2001 style so Icarus Verilog compiles it
// unchanged, while pyslang elaborates it for the netlist.

module xprop_unit (
  input  wire       clk,
  input  wire       rst_n,
  input  wire [7:0] din,
  output reg  [7:0] dout
);
  // never initialised -> X until the first posedge captures din
  reg [7:0] stage;

  always @(posedge clk) begin
    stage <= din;                 // X while din is X
    if (rst_n)
      dout <= stage ^ 8'h5A;      // X propagates through XOR
    // no else: dout keeps its X before reset deasserts
  end
endmodule

module tri_unit (
  input  wire       drv_a_en,
  input  wire       drv_b_en,
  input  wire [3:0] a_val,
  input  wire [3:0] b_val,
  output wire [3:0] bus
);
  // two tri-state drivers on one net:
  //   both off            -> bus = Z
  //   one on              -> bus = that value
  //   both on, conflicting -> bus = X (per-bit resolution)
  assign bus = drv_a_en ? a_val : 4'bzzzz;
  assign bus = drv_b_en ? b_val : 4'bzzzz;
endmodule

module hidden_inner (
  input  wire       clk,
  output reg  [3:0] shadow
);
  initial shadow = 4'd0;
  always @(posedge clk)
    shadow <= shadow + 4'd1;
endmodule

module hidden_unit (
  input  wire       clk,
  output reg  [3:0] cnt
);
  // u_inner is EXCLUDED from the dump (depth-limited $dumpvars on
  // u_hidden) -> "RTL has it, waveform does not" partial-dump scenario.
  wire [3:0] shadow;

  hidden_inner u_inner (
    .clk    (clk),
    .shadow (shadow)
  );

  initial cnt = 4'd0;

  always @(posedge clk)
    cnt <= shadow;
endmodule

module fourstate_top (
  input  wire       clk,
  input  wire       rst_n,
  input  wire [7:0] din,
  input  wire       drv_a_en,
  input  wire       drv_b_en,
  input  wire [3:0] a_val,
  input  wire [3:0] b_val,
  output wire [7:0] dout,
  output wire [3:0] bus,
  output wire [3:0] hidden_cnt
);
  xprop_unit u_xprop (
    .clk   (clk),
    .rst_n (rst_n),
    .din   (din),
    .dout  (dout)
  );

  tri_unit u_tri (
    .drv_a_en (drv_a_en),
    .drv_b_en (drv_b_en),
    .a_val    (a_val),
    .b_val    (b_val),
    .bus      (bus)
  );

  hidden_unit u_hidden (
    .clk (clk),
    .cnt (hidden_cnt)
  );
endmodule
