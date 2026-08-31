// Demo 3: CDC (clock-domain crossing) metastability
//
// Bug story (intentional): a pulse generated in the 50 MHz "fast" domain
// crosses into the 25 MHz "slow" domain with NO synchronizer. Depending
// on the phase relationship the slow domain either misses the pulse
// entirely or samples it while it is toggling (metastable capture), so
// pulse_seen toggles erratically and pulse_count misses pulses.
//
// Agent workflow this demo exercises:
//   open_session -> list_signals on both domains -> signal_values(pulse_seen)
//   shows irregular capture -> signal_connectivity(pulse_seen) reveals it is
//   driven straight from the other domain (no sync stage)
//   -> open_wave_view with BOTH clocks displayed, markers at missed pulses,
//   annotation recommending a 2-FF synchronizer.
module cdc_top (
    input  wire clk_fast,     // 50 MHz domain
    input  wire rst_n,
    input  wire trigger,      // stimulus
    input  wire clk_slow,     // 25 MHz domain
    output reg  pulse_seen,   // BUG: direct crossing, no synchronizer
    output reg  [7:0] pulse_count
);

    reg pulse_fast;

    // fast domain: emit a 1-cycle pulse per trigger (20 units with #10;
    // narrower than the 50-unit slow clock so alignment decides catch/miss)
    always @(posedge clk_fast or negedge rst_n) begin
        if (!rst_n)
            pulse_fast <= 1'b0;
        else
            pulse_fast <= trigger && !pulse_fast;
    end

    // slow domain: direct (unsynchronized) capture — the demo bug
    always @(posedge clk_slow or negedge rst_n) begin
        if (!rst_n) begin
            pulse_seen  <= 1'b0;
            pulse_count <= 8'h00;
        end else begin
            pulse_seen <= pulse_fast;           // crossing with no sync
            if (pulse_fast && !pulse_seen)
                pulse_count <= pulse_count + 1'b1;
        end
    end
endmodule

module cdc_tb;
    reg clk_fast, clk_slow, rst_n, trigger;
    wire pulse_seen;
    wire [7:0] pulse_count;

    cdc_top dut (.clk_fast(clk_fast), .rst_n(rst_n), .trigger(trigger),
                 .clk_slow(clk_slow), .pulse_seen(pulse_seen),
                 .pulse_count(pulse_count));

    // 50 MHz fast and 20 MHz slow (period 50, offset 13). A 20-unit pulse
    // crosses cleanly only when a slow rising edge lands inside it; with
    // the #125 spacing the alignment slides every trigger: some pulses are
    // captured, others vanish entirely. Classic unsynchronized-pulse CDC.
    initial clk_fast = 0;
    always #10 clk_fast = ~clk_fast;
    initial begin clk_slow = 0; #13; end
    always #25 clk_slow = ~clk_slow;

    integer k;
    initial begin
        rst_n = 0; trigger = 0;
        #15 rst_n = 1;
        for (k = 0; k < 6; k = k + 1) begin
            @(posedge clk_fast); trigger = 1;
            @(posedge clk_fast); trigger = 0;
            #125;                // slides phase relative to the slow clock
        end
        #120 $finish;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, cdc_tb);
    end
endmodule
