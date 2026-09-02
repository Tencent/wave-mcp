// fst_dump_cfg.sv — ready-to-use FST dump control module for Xcelium (xrun).
//
// Purpose: dump FST waveforms from xrun without touching your existing
// testbench. Add this file to the xrun command line as a second top; all
// three knobs are compile-time macros, so one copy serves every case.
//
// Usage (see docs/XCELIUM_FST_GUIDE.md for the full story):
//
//   xrun -64bit +access+r \
//     -loadvpi /path/to/fstdumper.so:vlog_startup_routines_bootstrap \
//     -f your_filelist.f \
//     examples/xcelium_fst/fst_dump_cfg.sv \
//     -top your_tb -top fst_dump \
//     -define 'FST_DUMP_TOP=your_tb' \
//     -define 'FST_DUMP_FILE="waves.fst"'
//
// Three rules this file cannot enforce for you:
//   1. FST_DUMP_TOP must match your actual tb top instance name. A wrong
//      scope dumps nothing and reports no error.
//   2. The second `-top fst_dump` is mandatory, otherwise this module is
//      never elaborated and no FST appears (silent failure).
//   3. Pass macros with `-define`, not `+define+`: the latter only applies at
//      elaboration, so the `ifndef`s below would not see it.
//
// Keep this file pure ASCII: xmvlog floods *W,NONPRT warnings otherwise.

`ifndef FST_DUMP_TOP
`define FST_DUMP_TOP top_tb           // default: dump the whole testbench
`endif

`ifndef FST_DUMP_LEVEL
`define FST_DUMP_LEVEL 0              // default: 0 = unlimited depth
`endif

`ifndef FST_DUMP_FILE
`define FST_DUMP_FILE "waves.fst"
`endif

module fst_dump;

  initial begin
    $fstDumpfile(`FST_DUMP_FILE);
    $fstDumpvars(`FST_DUMP_LEVEL, `FST_DUMP_TOP);
  end

endmodule
