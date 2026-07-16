// sample RTL for wave-mcp smoke test
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
