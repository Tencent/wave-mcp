// fst_dump_cfg.sv - FST dump control module for Xcelium (xrun) + fstdumper.
//
// Dumps FST waveforms without touching your testbench: compile this file
// together with the design and add it as a second top (-top fst_dump).
// One copy serves every test and every simulation directory.
//
// -top fst_dump must come AFTER your testbench's -top: the first -top names
// the snapshot and is the default scope of -input Tcl scripts, so with
// fst_dump first a scope-less "probe -create -shm -all" (SimVision dump
// scripts) probes only this module and fails with *SE,PRNONE / PRBFAIL.
//
// Output file, first match wins:
//   1. run time   +fst_wave_file=<path>      (e.g. added by a Makefile)
//   2. compile    -define 'FST_DUMP_FILE="<path>"'
//   3. neither    nothing is dumped, one line is printed
// So a snapshot compiled with this module costs nothing in runs that do not
// ask for a waveform, unless FST_DUMP_FILE was given at compile time.
//
// Compile-time knobs (optional):
//   FST_DUMP_TOP    dump scope, default top_tb. Must be the real instance
//                   path; a wrong scope dumps nothing and reports no error.
//   FST_DUMP_LEVEL  dump depth, default 0 (unlimited)
// -define and +define+ both work. They are compile options: changing one
// needs a recompile.
//
// Example (see docs/XCELIUM_FST_GUIDE.md):
//   xrun -64bit -access +r \
//     -loadvpi /path/to/fstdumper.so:vlog_startup_routines_bootstrap \
//     -f your_filelist.f examples/xcelium_fst/fst_dump_cfg.sv \
//     -top your_tb -top fst_dump \
//     -define 'FST_DUMP_TOP=your_tb' -define 'FST_DUMP_FILE="waves.fst"'
//
// Keep this file pure ASCII: xmvlog floods *W,NONPRT warnings otherwise.

`ifndef FST_DUMP_TOP
  `define FST_DUMP_TOP top_tb
`endif
`ifndef FST_DUMP_LEVEL
  `define FST_DUMP_LEVEL 0
`endif

`timescale 1ns/1ps

module fst_dump;

  string fst_file;

  initial begin
    // format string has no leading '+'
    if (!$value$plusargs("fst_wave_file=%s", fst_file)) begin
`ifdef FST_DUMP_FILE
      fst_file = `FST_DUMP_FILE;
`else
      fst_file = "";
`endif
    end
    if (fst_file == "") begin
      $display("[fst_dump] no +fst_wave_file=<path> or FST_DUMP_FILE given, FST dump off");
    end else begin
      $display("[fst_dump] FST waveform will be dumped to: %s", fst_file);
      $fstDumpfile(fst_file);
      $fstDumpvars(`FST_DUMP_LEVEL, `FST_DUMP_TOP);
      $fstDumpon;
    end
  end

endmodule
