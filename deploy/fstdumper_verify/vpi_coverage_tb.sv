// vpi_coverage_tb.sv - test design for deploy/verify_fstdumper.sh (wave-mcp, Apache-2.0).
//
// Exercises the object kinds a patched fstdumper must dump on Xcelium:
//   generate blocks (named / genblkN / for / if / nested, with module and
//   interface instances inside), module / interface instance arrays,
//   named begin / fork, static tasks, clocking blocks, enum / packed
//   struct / packed union / packed array (var and net), `input bit` ports,
//   integer / real / time / event, ascending and offset ranges, ranges with
//   function calls, names with '$' and escaped identifiers, port-collapsed
//   nets,
// plus objects that must be skipped quietly: unpacked struct, string,
// queue / dynamic array / memory, class handle, automatic variables.
// Dumping is driven by examples/xcelium_fst/fst_dump_cfg.sv.
`timescale 1ns/1ps

package probe_pkg;
  typedef enum logic [2:0] {S_IDLE, S_A, S_B, S_C} state_e;
  typedef struct packed { logic [3:0] hi; logic [3:0] lo; } pair_t;
  typedef union packed { logic [7:0] raw; pair_t pr; } word_u;
  typedef struct { int a; logic [3:0] b; } unpk_t;
endpackage

interface bit_if (input bit clk, input bit rst_n, input bit [3:0] tag);
  logic [7:0] data;
  bit         flag;
  clocking cb @(posedge clk);
    input data;
    output flag;
  endclocking
  task static if_st_task();
    logic [7:0] if_loc;
    if_loc = data;
  endtask
  task automatic if_au_task();
    logic [7:0] if_aloc;
    if_aloc = data;
  endtask
  for (genvar ig = 0; ig < 2; ig++) begin : g_in_if
    logic igl;
    assign igl = tag[ig];
  end
  always @(posedge clk) begin
    if_st_task();
    if_au_task();
  end
endinterface

module leaf (input logic clk, input logic [7:0] d, output logic [7:0] q);
  always_ff @(posedge clk) q <= d;
endmodule

module vpi_coverage_tb;
  import probe_pkg::*;

  bit clk = 0;
  always #5 clk = ~clk;
  bit rst_n;

  state_e          st;           // enum var
  pair_t           pr;           // packed struct var
  word_u           wu;           // packed union var
  unpk_t           up;           // unpacked struct var (should stay skipped)
  logic [1:0][3:0] pa;           // packed array var
  wire  pair_t     pr_w;         // packed struct net
  wire  state_e    st_w;         // enum net
  logic [7:0]      cnt;
  logic [0:3]      asc_r;        // ascending range: FST name must say [0:3]
  wire  [8:1]      off_w;        // range not starting at 0: [8:1]
  always @(posedge clk) asc_r <= cnt[3:0];
  assign off_w = cnt;
  localparam int   PW = $clog2(32);
  wire  [$clog2(16)-1:0] clog_w; // range with a function call: no VPI EXPRWFC error
  wire  [PW-1:0]   pw_w;         // range from a parameter expression: [4:0]
  function automatic int f_msb(input int w); return w - 1; endfunction
  wire  [f_msb(6):0] fn_w;       // range with a user constant function
  assign clog_w = cnt[3:0];
  assign pw_w   = cnt[4:0];
  assign fn_w   = cnt[5:0];
  wire  [3:0]      dly$dotret;   // '$' inside a simple identifier: no backslash
  wire             \esc+id ;     // escaped identifier: written as in the native VCD (no backslash)
  leaf             \u+esc (.clk(clk), .d(cnt), .q());   // escaped instance name
  assign dly$dotret = cnt[7:4];
  assign \esc+id = cnt[0] ^ cnt[1];

  wire  word_u           wu_w;   // packed union net
  wire  integer          wi;     // integer net
  wire  logic [1:0][3:0] pan;    // packed array net
  assign pr_w = pr;
  assign st_w = st;
  assign wu_w = wu;
  assign wi   = cnt;
  assign pan  = pa;

  // TB-style objects that FST cannot represent / arrays
  class obj_c; int v; endclass
  string           s_str;
  int              q_int[$];
  int              d_int[];
  obj_c            h_obj;
  logic [3:0]      mem [0:3];
  pair_t [1:0]     pas;           // packed array of packed struct
  state_e [1:0]    pae;           // packed array of enum
  integer          iv;
  real             rv;
  time             tv;
  event            ev;
  leaf             u_arr [1:0] (.clk(clk), .d(cnt), .q());
  bit_if           u_ifa [1:0] (.clk(clk), .rst_n(rst_n), .tag(cnt[3:0]));
  always @(posedge clk) begin
    pas <= {pr, pr};
    pae <= {st, st};
    iv  <= cnt;
    rv  <= cnt * 0.5;
    tv  <= $time;
    -> ev;
  end
  task static st_task2();
    integer ti; real tr; time tt; string ts;
    ti = cnt; tr = 1.5; tt = $time; ts = "x";
  endtask
  task automatic au_task2();
    integer ai; real ar; time at;
    ai = cnt; ar = 1.5; at = $time;
  endtask
  always @(posedge clk) begin st_task2(); au_task2(); end

  // automatic variable inside a static named block
  initial begin : nb_auto
    automatic logic [3:0] av = 4'd1;
    logic [3:0] sv_in_auto;
    sv_in_auto = av;
    #30 sv_in_auto = av + 1;
  end

  bit_if u_if (.clk(clk), .rst_n(rst_n), .tag(cnt[3:0]));

  // generate: for loop with regs and module instances
  genvar gi;
  generate
    for (gi = 0; gi < 2; gi++) begin : g_loop
      logic [7:0] r;
      logic [7:0] q;
      always_ff @(posedge clk) r <= cnt + gi;
      leaf u_leaf (.clk(clk), .d(r), .q(q));
    end
  endgenerate

  // generate: unnamed for loop (genblk)
  for (genvar gj = 0; gj < 2; gj++) begin
    logic gb;
    assign gb = cnt[gj];
  end

  // generate: if
  if (1) begin : g_if
    logic [3:0] gif;
    always_ff @(posedge clk) gif <= cnt[7:4];
    // nested generate
    for (genvar gk = 0; gk < 2; gk++) begin : g_nest
      logic nb;
      assign nb = gif[gk];
    end
  end

  // direct module instance + interface inside generate
  logic [7:0] top_q;
  logic [3:0] fn_q;
  leaf u_top_leaf (.clk(clk), .d(cnt), .q(top_q));
  for (genvar gm = 0; gm < 1; gm++) begin : g_ifc
    bit_if u_gif (.clk(clk), .rst_n(rst_n), .tag(cnt[7:4]));
  end

  // static / automatic tasks, function
  task static st_task(input logic [3:0] a);
    logic [3:0] st_loc;
    st_loc = a;
  endtask
  task automatic au_task(input logic [3:0] a);
    logic [3:0] au_loc;
    au_loc = a;
  endtask
  function automatic logic [3:0] fn(input logic [3:0] a);
    logic [3:0] fn_loc;
    fn_loc = a + 1;
    return fn_loc;
  endfunction
  always @(posedge clk) begin
    st_task(cnt[3:0]);
    au_task(cnt[3:0]);
    fn_q <= fn(cnt[3:0]);
  end

  // named fork
  initial fork : nf_blk
    begin : nf_a
      logic nfa;
      nfa = 0;
      #20 nfa = 1;
    end
  join

  // named begin with static var
  initial begin : nb_blk
    logic [3:0] nbv;
    nbv = 0;
    repeat (8) begin
      @(posedge clk);
      nbv = nbv + 1;
    end
  end

  initial begin
    rst_n = 0;
    st  = S_IDLE;
    pr  = '0;
    wu  = '0;
    pa  = '0;
    cnt = 0;
    up.a = 0; up.b = 0;
    u_if.data = 0;
    #12 rst_n = 1;
    repeat (8) begin
      @(posedge clk);
      cnt = cnt + 1;
      st  = state_e'(cnt[1:0]);
      pr  = {cnt[3:0], ~cnt[3:0]};
      wu.raw = cnt * 3;
      pa  = {cnt[3:0], cnt[7:4]};
      u_if.data = cnt;
      up.a = cnt;
    end
    @(posedge clk);
    $finish;
  end

endmodule
