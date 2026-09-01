`timescale 1ns/1ps

// Demo 2: FSM deadlock
//
// Bug story (intentional): the WREQ handshake uses a req/ack pair, but the
// FSM only samples ack inside WAIT_ACK with no timeout. When the slave
// back-pressures (ack never asserts for this request), the FSM waits
// forever: no more transactions complete, rd_count stops advancing and
// the testbench watchdog kills the sim.
//
// Agent workflow this demo exercises:
//   open_session -> signal_values(rd_count) notices the flatline
//   -> active_drivers/trace_value at the stuck time -> signal_values(req/ack)
//   -> open_wave_view with cursor at the deadlock, markers at
//   req-rise and the last ack, annotation explaining the missing timeout.
module fsm_stuck_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rd_en,        // upstream keeps requesting reads
    input  wire       ack,          // slave handshake ack (stalls in run 2)
    output reg        req,
    output reg  [7:0] rd_count,     // completed reads counter
    output reg  [7:0] rd_data
);

    localparam [1:0] IDLE = 2'd0, WAIT_ACK = 2'd1, CAPTURE = 2'd2;

    reg [1:0] state;
    reg [7:0] fake_data;   // stand-in payload

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= IDLE;
            req      <= 1'b0;
            rd_count <= 8'h00;
            rd_data  <= 8'h00;
        end else begin
            case (state)
                IDLE: begin
                    if (rd_en) begin
                        req   <= 1'b1;
                        state <= WAIT_ACK;
                    end
                end
                WAIT_ACK: begin
                    if (ack) begin          // BUG: no timeout escape
                        req      <= 1'b0;
                        rd_count <= rd_count + 1'b1;
                        rd_data  <= fake_data;
                        state    <= CAPTURE;
                    end
                end
                CAPTURE: begin
                    state <= IDLE;
                end
                default: state <= IDLE;
            endcase
        end
    end

    // toy payload generator so rd_data changes while transactions flow
    always @(posedge clk)
        if (state == IDLE && rd_en) fake_data <= fake_data + 1'b1;
endmodule

module fsm_stuck_tb;
    reg clk, rst_n, rd_en, ack;
    wire req, ack_unused;
    wire [7:0] rd_count, rd_data;

    // run 2 note: ack_wanted below is the failure injection switch
    reg ack_wanted;

    fsm_stuck_top dut (.clk(clk), .rst_n(rst_n), .rd_en(rd_en), .ack(ack),
                       .req(req), .rd_count(rd_count), .rd_data(rd_data));

    always #5 clk = ~clk;

    initial begin
        clk = 0; rst_n = 0; rd_en = 0; ack = 0; ack_wanted = 1;
        #12 rst_n = 1;

        // three transactions, ack arrives every time
        repeat (3) begin
            @(negedge clk); rd_en = 1;
            @(negedge clk); rd_en = 0;
            wait (req); @(negedge clk); ack = 1;   // slave acks
            @(negedge clk); ack = 0;
        end

        // failure injection: slave stops acking
        #10 ack_wanted = 0;
        @(negedge clk); rd_en = 1;
        @(negedge clk); rd_en = 0;
        @(negedge clk); if (ack_wanted) ack = 1;
        // no ack ever -> FSM stuck in WAIT_ACK with req still high

        #80 $finish;   // watchdog-ish end: counter visibly flat after
    end

    // mirror ack_wanted onto ack for the first three transactions
    always @(negedge clk) if (!ack_wanted) ack = 0;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, fsm_stuck_tb);
    end
endmodule
