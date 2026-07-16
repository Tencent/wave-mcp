// Self-contained testbench for the wave-mcp quickstart.
//
// The --trace-fst flag makes $dumpfile/$dumpvars write a real FST directly, so
// this needs NO commercial simulator (xrun) and NO vcd2fst. Build with
// `verilator --binary --trace-fst` and run the produced binary; it drops
// `counter.fst` in the current directory.
module top_tb;
    logic       clk = 1'b0;
    logic       rst_n;
    logic [7:0] count;
    logic       overflow;

    counter u_counter (
        .clk      (clk),
        .rst_n    (rst_n),
        .count    (count),
        .overflow (overflow)
    );

    // 10 time-unit clock period
    always #5 clk = ~clk;

    initial begin
        $dumpfile("counter.fst");
        $dumpvars(0, top_tb);

        rst_n = 1'b0;
        #12 rst_n = 1'b1;   // release reset after a couple of edges
        #600;               // ~60 clocks of counting
        $finish;
    end
endmodule
