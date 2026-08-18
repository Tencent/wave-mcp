// Extended four-state test design: constructs NOT covered by fourstate_top.
//   guard_unit    - always-block if/else guards evaluated under X reset
//   case_unit     - case + casez (wildcard) selection, X selector
//   bitsel_unit   - per-bit / part-select drivers, partial-X bus
//   latch_unit    - level-sensitive latch (X enable -> latched X)
//   gen_unit      - for-generate array of per-bit tri-state drivers
//   wor_unit      - wired-OR resolution net
// Verilog-2001 style so Icarus and pyslang both handle it identically.

module guard_unit (
  input  wire       clk,
  input  wire       rst_n,      // held X for a while by the tb
  input  wire       en,
  input  wire [7:0] d,
  output reg  [7:0] q
);
  always @(posedge clk) begin
    if (!rst_n)
      q <= 8'h00;
    else if (en)
      q <= d;
  end
endmodule

module case_unit (
  input  wire [1:0] sel,        // driven X in one phase
  input  wire [3:0] w0,
  input  wire [3:0] w1,
  input  wire [3:0] w2,
  output reg  [3:0] y,          // plain case
  output reg  [3:0] yz          // casez with wildcard
);
  always @(*) begin
    case (sel)
      2'b00: y = w0;
      2'b01: y = w1;
      default: y = w2;
    endcase
  end

  always @(*) begin
    casez (sel)
      2'b0?: yz = w0;           // matches 00 and 01
      2'b10: yz = w1;
      default: yz = w2;
    endcase
  end
endmodule

module bitsel_unit (
  input  wire       clk,
  input  wire       lo_en,      // when 0, low nibble never written -> stays X
  input  wire [3:0] lo_val,
  input  wire [3:0] hi_val,
  output reg  [7:0] mixed       // [3:0] gated write, [7:4] always written
);
  // no initialisation: whole bus X at t=0; upper half clears on first edge,
  // lower half clears only after lo_en goes high -> partial-X bus.
  always @(posedge clk) begin
    mixed[7:4] <= hi_val;
    if (lo_en)
      mixed[3:0] <= lo_val;
  end
endmodule

module latch_unit (
  input  wire       g,          // gate, driven X in one phase
  input  wire [3:0] d,
  output reg  [3:0] q
);
  always @(*) begin
    if (g)
      q = d;                    // g=X -> simulator keeps/latches per X rules
  end
endmodule

module gen_unit (
  input  wire [3:0] en,         // per-bit enables
  input  wire [3:0] v,
  output wire [3:0] o           // each bit tri-stated independently
);
  genvar i;
  generate
    for (i = 0; i < 4; i = i + 1) begin : g_bit
      assign o[i] = en[i] ? v[i] : 1'bz;
    end
  endgenerate
endmodule

module wor_unit (
  input  wire a_en,
  input  wire b_en,
  output wor  w                 // wired-OR: 1 wins over 0/Z
);
  assign w = a_en ? 1'b1 : 1'bz;
  assign w = b_en ? 1'b0 : 1'bz;
endmodule

module fourstate_ext_top (
  input  wire       clk,
  input  wire       rst_n,
  input  wire       en,
  input  wire [7:0] d,
  input  wire [1:0] sel,
  input  wire [3:0] w0,
  input  wire [3:0] w1,
  input  wire [3:0] w2,
  input  wire       lo_en,
  input  wire [3:0] lo_val,
  input  wire [3:0] hi_val,
  input  wire       lg,
  input  wire [3:0] ld,
  input  wire [3:0] gen_en,
  input  wire [3:0] gen_v,
  input  wire       wa_en,
  input  wire       wb_en,
  output wire [7:0] q,
  output wire [3:0] y,
  output wire [3:0] yz,
  output wire [7:0] mixed,
  output wire [3:0] lq,
  output wire [3:0] go,
  output wire       w
);
  guard_unit  u_guard  (.clk(clk), .rst_n(rst_n), .en(en), .d(d), .q(q));
  case_unit   u_case   (.sel(sel), .w0(w0), .w1(w1), .w2(w2), .y(y), .yz(yz));
  bitsel_unit u_bitsel (.clk(clk), .lo_en(lo_en), .lo_val(lo_val),
                        .hi_val(hi_val), .mixed(mixed));
  latch_unit  u_latch  (.g(lg), .d(ld), .q(lq));
  gen_unit    u_gen    (.en(gen_en), .v(gen_v), .o(go));
  wor_unit    u_wor    (.a_en(wa_en), .b_en(wb_en), .w(w));
endmodule
