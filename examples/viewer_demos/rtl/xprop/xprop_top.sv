`timescale 1ns/1ps

// Demo 1: X-propagation root cause
//
// Bug story (intentional): byte_cnt is a shift-counter that is NEVER reset
// (missing from the reset branch, never initialized anywhere else). It
// starts X and stays X forever: X+1=X. The FSM itself works fine because
// the shift-length uses a separately-reset tick counter, but data_out
// embeds byte_cnt in its upper bits, so every packet's output carries X.
//
// Agent workflow this demo exercises:
//   open_session -> signal_values(data_out) -> trace_x(data_out, first X)
//   -> open_wave_view with a marker at the X onset + annotation carrying
//   the trace_x conclusion (un-reset counter).
module xprop_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] din,
    output reg  [7:0] data_out,
    output reg        done
);

    localparam [1:0] IDLE = 2'd0, SHIFT = 2'd1, FLUSH = 2'd2;

    reg [1:0] fsm_state;      // properly reset
    reg [2:0] shift_ticks;    // properly reset (drives FSM)
    reg [2:0] byte_cnt;       // BUG: never reset, never initialized -> X forever
    reg [7:0] shreg;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            fsm_state   <= IDLE;
            shift_ticks <= 3'd0;
            done        <= 1'b0;
        end else begin
            done <= 1'b0;
            case (fsm_state)
                IDLE: if (start) begin
                    fsm_state   <= SHIFT;
                    shift_ticks <= 3'd0;
                    shreg       <= din;
                end
                SHIFT: begin
                    shreg       <= {shreg[6:0], 1'b0};
                    shift_ticks <= shift_ticks + 1'b1;
                    byte_cnt    <= byte_cnt + 1'b1;   // X+1 = X
                    if (shift_ticks == 3'd5)
                        fsm_state <= FLUSH;
                end
                FLUSH: begin
                    data_out  <= {byte_cnt, shreg[7:5]}; // X bits in [7:5]
                    done      <= 1'b1;
                    fsm_state <= IDLE;
                end
                default: fsm_state <= IDLE;
            endcase
        end
    end
endmodule

module xprop_tb;
    reg clk, rst_n, start;
    reg [7:0] din;
    wire [7:0] data_out;
    wire done;

    xprop_top dut (.clk(clk), .rst_n(rst_n), .start(start),
                   .din(din), .data_out(data_out), .done(done));

    always #5 clk = ~clk;

    task packet(input [7:0] d);
        begin
            @(negedge clk); start = 1'b1; din = d;
            @(negedge clk); start = 1'b0;
            @(posedge done);
            @(negedge clk);
        end
    endtask

    initial begin
        clk = 0; rst_n = 0; start = 0; din = 8'h00;
        #12 rst_n = 1;
        packet(8'hA5);            // good packet 1
        packet(8'h3C);            // good packet 2
        #20 rst_n = 0;            // mid-run reset pulse
        #8  rst_n = 1;            // byte_cnt survives as stale value
        packet(8'h81);            // corrupted packet (data_out goes X)
        packet(8'h42);            // recovers by itself
        #30 $finish;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, xprop_tb);
    end
endmodule
