`timescale 1ns/1ps

// Regression demo DUT: a small packet CRC block with a seed-dependent bug.
//
// The point of this demo is NOT the bug itself but the triage flow: a
// regression run produces a mix of passing and failing cases, and the
// analysis has to work out which failures share a root cause using only
// the waveforms and the netlist.
//
// The bug: inside the CRC update there is a guard that was meant to handle
// an escape sequence, but it drops the crc[2] XOR term whenever three
// consecutive nibbles are all 2'b11. Whether a run hits that pattern
// depends on the payload, which is derived from the seed, so the SAME RTL
// passes on most seeds and fails on a few. That is exactly the shape of a
// real regression failure: intermittent, seed-dependent, and invisible
// until you compare a passing run against a failing one.
//
// SEED is passed in with -DSEED=n by run_regression.py, so every case is
// a real simulation with real stimulus, not a replayed answer.

module crc_pkt (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       valid,
    input  wire [1:0] data,
    input  wire       sop,
    input  wire       eop,
    output reg  [3:0] residue,
    output reg        done
);

    reg [3:0] crc;
    reg       capturing;
    reg [1:0] data_q, data_q2;   // history, used by the guard below

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            crc       <= 4'h0;
            capturing <= 1'b0;
            data_q    <= 2'b00;
            data_q2   <= 2'b00;
        end else begin
            if (sop) begin
                crc       <= 4'h0;
                capturing <= 1'b1;
                data_q    <= data;
                data_q2   <= 2'b00;
            end else if (eop) begin
                capturing <= 1'b0;
            end else if (valid && capturing) begin
                crc[0] <= crc[3] ^ data[1] ^ data[0];
                crc[1] <= crc[0] ^ data[1];
                // BUG: the crc[2] tap is skipped when three consecutive
                // nibbles are all 2'b11. The intent was a special case for
                // an escape sequence; the effect is a dropped XOR term, so
                // the residue is wrong from that point on. Rare enough that
                // most seeds pass, which is what makes it a realistic
                // intermittent regression failure.
                if (data == 2'b11 && data_q == 2'b11 && data_q2 == 2'b11)
                    crc[2] <= crc[1];
                else
                    crc[2] <= crc[1] ^ data[0];
                crc[3] <= crc[2];
                data_q  <= data;
                data_q2 <= data_q;
            end
        end
    end

    // capture the residue at end of packet
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            residue <= 4'h0;
            done    <= 1'b0;
        end else if (eop) begin
            residue <= crc;
            done    <= 1'b1;
        end
    end
endmodule


module crc_regress_tb;

    // SEED selects the payload. Every regression case compiles this same
    // testbench with a different -DSEED, so the stimulus is genuinely
    // different per case and the pass/fail split is a real outcome.
`ifndef SEED
  `define SEED 1
`endif

    reg clk, rst_n, valid, sop, eop;
    reg [1:0] data;
    wire [3:0] residue;
    wire       done;

    integer seed;
    integer errors;

    // Reference model: the correct CRC, computed in the testbench. The DUT
    // is compared against this, so a case fails only when the RTL bug is
    // actually exercised by that seed's payload.
    reg [3:0] ref_crc;
    reg       ref_capturing;
    reg [3:0] expected_residue;

    crc_pkt dut (.clk(clk), .rst_n(rst_n), .valid(valid), .data(data),
                 .sop(sop), .eop(eop), .residue(residue), .done(done));

    always #5 clk = ~clk;

    // golden CRC update, mirrors the intended RTL (crc[2] uses data[0])
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ref_crc       <= 4'h0;
            ref_capturing <= 1'b0;
        end else begin
            if (sop) begin
                ref_crc       <= 4'h0;
                ref_capturing <= 1'b1;
            end else if (eop) begin
                ref_capturing    <= 1'b0;
                expected_residue <= ref_crc;
            end else if (valid && ref_capturing) begin
                ref_crc[0] <= ref_crc[3] ^ data[1] ^ data[0];
                ref_crc[1] <= ref_crc[0] ^ data[1];
                ref_crc[2] <= ref_crc[1] ^ data[0];
                ref_crc[3] <= ref_crc[2];
            end
        end
    end

    task send_packet(input [63:0] payload, input integer nbits);
        integer i;
        begin
            @(negedge clk); sop = 1; valid = 1; data = payload[1:0];
            @(negedge clk); sop = 0;
            for (i = 2; i < nbits; i = i + 2) begin
                data = payload[i +: 2];
                @(negedge clk);
            end
            valid = 0; eop = 1;
            @(negedge clk); eop = 0;
            @(negedge clk);
        end
    endtask

    reg [63:0] payload;

    initial begin
        clk = 0; rst_n = 0; valid = 0; sop = 0; eop = 0;
        data = 2'b00; errors = 0;
        seed = `SEED;

        // Payload derived from the seed: a simple LCG so each case gets a
        // distinct, reproducible bit pattern.
        payload = 64'h0;
        begin : gen
            integer k;
            integer state;
            state = seed * 1103515245 + 12345;
            for (k = 0; k < 64; k = k + 2) begin
                state = (state * 1103515245 + 12345) & 32'h7fffffff;
                payload[k +: 2] = state[17:16];
            end
        end

        #12 rst_n = 1;
        send_packet(payload, 64);

        if (residue !== expected_residue) begin
            errors = errors + 1;
            $display("FAIL seed=%0d residue=%b expected=%b",
                     seed, residue, expected_residue);
        end else begin
            $display("PASS seed=%0d residue=%b", seed, residue);
        end

        #40;
        if (errors == 0) $display("TEST PASSED");
        else             $display("TEST FAILED (%0d errors)", errors);
        $finish;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, crc_regress_tb);
    end
endmodule
