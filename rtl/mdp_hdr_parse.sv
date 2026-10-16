// mdp_hdr_parse -- Ethernet/IPv4/UDP screening + payload realignment.
//
// Input is a 64-bit AXI-Stream as produced by a 10G MAC (byte 0 in
// tdata[7:0]); there is NO back-pressure -- a market-data feed cannot be
// stalled, so downstream must keep up or count the loss.
//
// Frame layout assumed (byte offsets):
//   0-5 dst MAC | 6-11 src MAC | 12-13 ethertype (0x0800) | 14 IPv4 ver/IHL
//   (0x45) | 23 protocol (17) | 36-37 UDP dst port | 42-43 message count |
//   44.. messages (16 bytes each).
// The payload starts 4 bytes into a beat, so the stage keeps the previous beat
// and emits {cur[31:0], prev[63:32]} -- i.e. a 4-byte realignment -- from
// beat 6 onward.  All header checks resolve by beat 4, well before the first
// payload word is emitted at beat 6, so the checks add no latency.
module mdp_hdr_parse (
    input  logic        clk,
    input  logic        rst,
    input  logic [15:0] cfg_dport,
    // AXI-Stream in
    input  logic [63:0] s_tdata,
    input  logic        s_tvalid,
    input  logic        s_tlast,
    // realigned payload words out
    output logic        w_valid,
    output logic        w_first,
    output logic [63:0] w_data,
    output logic [15:0] w_count,
    // per-frame verdict pulses
    output logic        frm_good,
    output logic        frm_bad
);
    logic [2:0]  bidx;        // beat index within frame, saturates at 6
    logic [31:0] prev_hi;   // bytes 4-7 of the previous beat
    logic        ok;
    logic        sent_first;
    logic [15:0] cnt_r;

    always_ff @(posedge clk) begin
        w_valid  <= 1'b0;
        w_first  <= 1'b0;
        frm_good <= 1'b0;
        frm_bad  <= 1'b0;
        if (rst) begin
            bidx       <= 3'd0;
            ok         <= 1'b1;
            sent_first <= 1'b0;
            cnt_r      <= 16'd0;
            prev_hi    <= 32'd0;
            w_data     <= 64'd0;
            w_count    <= 16'd0;
        end else if (s_tvalid) begin
            prev_hi <= s_tdata[63:32];
            if (bidx != 3'd6) bidx <= bidx + 3'd1;
            case (bidx)
                3'd1: ok <= ok && (s_tdata[47:32] == 16'h0008)   // ethertype 0x0800
                               && (s_tdata[55:48] == 8'h45);     // IPv4, IHL=5
                3'd2: ok <= ok && (s_tdata[63:56] == 8'h11);     // UDP
                3'd4: ok <= ok && ({s_tdata[39:32], s_tdata[47:40]} == cfg_dport);
                3'd5: cnt_r <= {s_tdata[23:16], s_tdata[31:24]};
                3'd6: begin
                    w_valid    <= ok;
                    w_data     <= {s_tdata[31:0], prev_hi};
                    w_first    <= ok && !sent_first;
                    w_count    <= cnt_r;
                    sent_first <= 1'b1;
                end
                default: ;
            endcase
            if (s_tlast) begin
                bidx       <= 3'd0;
                ok         <= 1'b1;
                sent_first <= 1'b0;
                frm_good   <= ok && (bidx == 3'd6);
                frm_bad    <= !(ok && (bidx == 3'd6));
            end
        end
    end
endmodule
