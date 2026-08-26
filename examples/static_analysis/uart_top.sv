// Static-analysis example DUT: a simple UART transmitter.
// Used by examples/static_analysis/run.py — no waveform, no simulator needed.
// Structure:
//   uart_top
//     └── u_tx (uart_tx)
//           └── u_baud_gen (baud_gen)

// Baud-rate tick generator: produces one tick per bit period while enabled.
module baud_gen #(
    parameter int CLK_HZ = 50_000_000,
    parameter int BAUD   = 115200
) (
    input  wire clk,
    input  wire rst_n,
    input  wire enable,
    output wire tick
);
    localparam int DIV = CLK_HZ / BAUD;
    reg [$clog2(DIV)-1:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)          cnt <= '0;
        else if (enable)     cnt <= (cnt == DIV - 1) ? '0 : cnt + 1'b1;
        else                 cnt <= '0;
    end

    assign tick = enable && (cnt == DIV - 1);
endmodule

// UART TX core: IDLE -> START -> DATA[7:0] -> STOP state machine.
module uart_tx (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       tx_valid,
    input  wire [7:0] tx_data,
    output reg        tx_ready,
    output reg        tx_serial
);
    localparam [1:0] IDLE  = 2'd0,
                     START = 2'd1,
                     DATA  = 2'd2,
                     STOP  = 2'd3;

    reg [1:0] state, state_next;
    reg [2:0] bit_cnt;
    reg [7:0] shift_reg;
    reg [7:0] latch_reg;
    wire      tick;

    baud_gen u_baud_gen (
        .clk    (clk),
        .rst_n  (rst_n),
        .enable (~tx_ready),
        .tick   (tick)
    );

    // Latch the byte when a new transfer is accepted.
    always @(posedge clk) begin
        if (tx_valid && tx_ready) latch_reg <= tx_data;
    end

    // Next-state logic.
    always @(*) begin
        case (state)
            IDLE:   state_next = tx_valid                    ? START : IDLE;
            START:  state_next = tick                         ? DATA  : START;
            DATA:   state_next = (tick && bit_cnt == 3'd7)    ? STOP  : DATA;
            STOP:   state_next = tick                         ? IDLE  : STOP;
            default: state_next = IDLE;
        endcase
    end

    // Registered logic + serial output.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= IDLE;
            bit_cnt   <= 3'd0;
            shift_reg <= 8'd0;
            tx_serial <= 1'b1;
            tx_ready  <= 1'b1;
        end else begin
            state <= state_next;
            if (tick) begin
                case (state)
                    START: begin
                        tx_serial <= 1'b0;
                        bit_cnt   <= 3'd0;
                        shift_reg <= latch_reg;
                    end
                    DATA: begin
                        tx_serial <= shift_reg[0];
                        shift_reg <= shift_reg >> 1;
                        bit_cnt   <= bit_cnt + 1'b1;
                    end
                    STOP: begin
                        tx_serial <= 1'b1;
                        tx_ready  <= 1'b1;
                    end
                endcase
            end
            if (tx_valid && tx_ready) tx_ready <= 1'b0;
        end
    end
endmodule

// Top wrapper.
module uart_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       tx_valid,
    input  wire [7:0] tx_data,
    output wire       tx_ready,
    output wire       tx_serial
);
    uart_tx u_tx (
        .clk      (clk),
        .rst_n    (rst_n),
        .tx_valid (tx_valid),
        .tx_data  (tx_data),
        .tx_ready (tx_ready),
        .tx_serial(tx_serial)
    );
endmodule
