"""Generate a tiny sample session (VCD->FST + log + filelist) for smoke testing.

Run:  python examples/make_sample.py
Produces examples/sample/{dump.vcd,dump.fst,xrun.log,rtl.f,counter.sv}
"""
from __future__ import annotations

import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sample")

VCD = r"""$date Mon Jan 1 2026 $end
$version wave-mcp sample $end
$timescale 1ns $end
$scope module top_tb $end
$var wire 1 ! clk $end
$var wire 1 " rst_n $end
$scope module u_counter $end
$var wire 1 # clk $end
$var wire 1 $ rst_n $end
$var reg 8 % count [7:0] $end
$var wire 1 & overflow $end
$upscope $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
0"
0#
0$
bxxxxxxxx %
x&
$end
#0
0!
0"
0#
0$
bxxxxxxxx %
x&
#5
1!
1#
#10
0!
0#
1"
1$
b00000000 %
0&
#15
1!
1#
#20
0!
0#
b00000001 %
#25
1!
1#
#30
0!
0#
b00000010 %
#35
1!
1#
#40
0!
0#
b11111111 %
#45
1!
1#
#50
0!
0#
b00000000 %
1&
"""

TB = """// sample testbench top for wave-mcp smoke test
module top_tb;
    reg        clk;
    reg        rst_n;
    wire [7:0] count;
    wire       overflow;

    counter u_counter (
        .clk      (clk),
        .rst_n    (rst_n),
        .count    (count),
        .overflow (overflow)
    );
endmodule
"""

SV = """// sample RTL for wave-mcp smoke test
module counter (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [7:0] count,
    output wire       overflow
);
    assign overflow = (count == 8'hFF);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 8'h00;
        else
            count <= count + 1'b1;
    end
endmodule
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    vcd_path = os.path.join(OUT, "dump.vcd")
    fst_path = os.path.join(OUT, "dump.fst")
    sv_path = os.path.join(OUT, "counter.sv")
    tb_path = os.path.join(OUT, "top_tb.sv")
    flist = os.path.join(OUT, "rtl.f")

    with open(vcd_path, "w") as f:
        f.write(VCD)
    with open(sv_path, "w") as f:
        f.write(SV)
    with open(tb_path, "w") as f:
        f.write(TB)
    with open(flist, "w") as f:
        f.write("counter.sv\ntop_tb.sv\n")

    vcd2fst = shutil.which("vcd2fst")
    if vcd2fst:
        subprocess.run([vcd2fst, vcd_path, fst_path], check=True)
        print(f"[ok] FST: {fst_path}")
    else:
        print("[warn] vcd2fst not found; only VCD produced")

    print(f"[ok] sample written under {OUT}")


if __name__ == "__main__":
    main()
