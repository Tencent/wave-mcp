// sample testbench top for wave-mcp smoke test
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
