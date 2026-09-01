`timescale 1ns/1ps

// Demo 4: pass/fail diff — CRC residue mismatch on a serial stream
//
// Bug story (intentional): the CRC LFSR misses one data bit whenever two
// data bits are back-to-back (a pipelining bug triggered by the payload
// pattern, not by every packet). Deterministic stimulus sends several
// identical packets, so the "pass" waveform (bug masked: pattern A) and
// the "fail" waveform (bug exposed: pattern B) differ ONLY in the CRC
// residue captured at packet end — perfect input for diff_waveforms.
//
// Files: same RTL compiled twice with +define+PATTERN_A / PATTERN_B.
// The FAIL run asserts crc_err at the first bad packet; PASS never does.
//
// Agent workflow this demo exercises:
//   diff_waveforms(pass.fst, fail.fst, clock=clk, after=reset)
//   -> first_divergence + diverging_signals -> open_wave_view with BOTH
//   FSTs (labels pass/fail), diff reference, red marker at divergence,
//   cursor there, annotation with the fanin conclusion.
module crc_diff_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       valid,
    input  wire [1:0] data,          // 2-bit nibble stream
    input  wire       sop,           // start of packet
    input  wire       eop,           // end of packet
    output reg  [3:0] crc_residue,   // captured CRC at eop
    output reg        crc_err        // residue != expected
);

    reg [3:0] crc;
    reg       capturing;

    // CRC-4 (ITU) style LFSR over the 2-bit data stream.
    // PATTERN_B (fail build) drops the crc[0] tap of data[0] — a classic
    // single-typo logic bug. Stimulus is byte-identical between the two
    // builds, so the waveforms stay identical until the first crc update.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            crc       <= 4'h0;
            capturing <= 1'b0;
        end else begin
            if (sop) begin
                crc       <= 4'h0;
                capturing <= 1'b1;
            end else if (eop) begin
                capturing <= 1'b0;
            end else if (valid && capturing) begin
`ifdef PATTERN_B
                crc[0] <= crc[3] ^ data[1];              // BUG: ^ data[0] missing
`else
                crc[0] <= crc[3] ^ data[1] ^ data[0];
`endif
                crc[1] <= crc[0] ^ data[1];
                crc[2] <= crc[1] ^ data[0];
                crc[3] <= crc[2];
            end
        end
    end

    // capture residue at eop and flag mismatch against the golden residue
    localparam [3:0] GOLDEN = 4'b0110;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            crc_residue <= 4'h0;
            crc_err     <= 1'b0;
        end else if (eop) begin
            crc_residue <= crc;
            crc_err     <= (crc != GOLDEN);
        end
    end
endmodule

module crc_diff_tb;
    reg clk, rst_n, valid, sop, eop;
    reg [1:0] data;
    wire [3:0] crc_residue;
    wire crc_err;

    crc_diff_top dut (.clk(clk), .rst_n(rst_n), .valid(valid), .data(data),
                      .sop(sop), .eop(eop), .crc_residue(crc_residue),
                      .crc_err(crc_err));

    always #5 clk = ~clk;

    task packet(input [79:0] bits, input integer nbits);
        integer i;
        begin
            @(negedge clk); sop = 1; valid = 1; data = bits[1:0];
            @(negedge clk); sop = 0;
            for (i = 2; i < nbits; i = i + 2) begin
                data = bits[i +: 2];
                @(negedge clk);
            end
            valid = 0; eop = 1;
            @(negedge clk); eop = 0;
        end
    endtask

    // three packets; stimulus is identical in pass and fail builds
    initial begin
        clk = 0; rst_n = 0; valid = 0; sop = 0; eop = 0; data = 2'b00;
        #12 rst_n = 1;
        packet({80'hECEC_ECEC_ECEC_ECEC_ECEC}, 80);
        packet({80'h3737_3737_3737_3737_3737}, 80);
        packet({80'hA5A5_A5A5_A5A5_A5A5_A5A5}, 80);
        #60 $finish;
    end

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, crc_diff_tb);
    end
endmodule
