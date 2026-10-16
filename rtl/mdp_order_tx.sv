// mdp_order_tx -- builds the outbound order frame and streams it as AXI-Stream.
//
// Frame (60 bytes = Ethernet minimum without FCS; the MAC appends the FCS):
//   Ethernet 14 | IPv4 20 (static, checksum supplied in cfg_ip_csum) |
//   UDP 8 (checksum 0, legal for IPv4) | order 16 | 2 pad
// Order payload: 'O' 'B' sym[2] client_id[4] price[4] qty[4]
//
// A request is taken in any cycle where `req_valid && req_ready` (req_ready =
// idle); beat 0 is registered on that same clock edge and the 8 beats leave
// back-to-back.  The static header could instead be streamed while the book is
// still computing (cut-through) -- see docs/SPEC.md, optimisation list.
module mdp_order_tx (
    input  logic        clk,
    input  logic        rst,
    // order request: {sym[15:0], px[31:0], qty[31:0]}
    input  logic        req_valid,
    input  logic [79:0] req_data,
    output logic        req_ready,
    // static configuration
    input  logic [47:0] cfg_dst_mac,
    input  logic [47:0] cfg_src_mac,
    input  logic [31:0] cfg_src_ip,
    input  logic [31:0] cfg_dst_ip,
    input  logic [15:0] cfg_sport,
    input  logic [15:0] cfg_odport,
    input  logic [15:0] cfg_ip_csum,
    // AXI-Stream out
    output logic [63:0] m_tdata,
    output logic [7:0]  m_tkeep,
    output logic        m_tvalid,
    output logic        m_tlast
);
    logic [511:0] frame_q;          // 8 x 64-bit beats (top 4 bytes unused/zero)
    logic         busy;
    logic [2:0]   beat;
    logic [31:0]  cid;

    wire [15:0] o_sym = req_data[79:64];
    wire [31:0] o_px  = req_data[63:32];
    wire [31:0] o_qty = req_data[31:0];

    // network-order frame, first byte in the MSBs
    wire [479:0] f_be = {cfg_dst_mac, cfg_src_mac, 16'h0800,
                         8'h45, 8'h00, 16'h002C, 16'h0000, 16'h4000,
                         8'h40, 8'h11, cfg_ip_csum, cfg_src_ip, cfg_dst_ip,
                         cfg_sport, cfg_odport, 16'h0018, 16'h0000,
                         8'h4F, 8'h42, o_sym, cid, o_px, o_qty, 16'h0000};

    // byte i of the frame -> tdata lane i (little-endian lane order)
    logic [479:0] f_le;
    always_comb begin
        for (int i = 0; i < 60; i++) f_le[8*i +: 8] = f_be[479 - 8*i -: 8];
    end

    assign req_ready = !busy;

    always_ff @(posedge clk) begin
        if (rst) begin
            busy     <= 1'b0;
            beat     <= 3'd0;
            cid      <= 32'd1;
            m_tvalid <= 1'b0;
            m_tlast  <= 1'b0;
            m_tkeep  <= 8'h00;
            m_tdata  <= 64'd0;
            frame_q  <= '0;
        end else if (!busy) begin
            m_tvalid <= 1'b0;
            m_tlast  <= 1'b0;
            if (req_valid) begin
                frame_q  <= {32'd0, f_le};
                m_tdata  <= f_le[63:0];
                m_tkeep  <= 8'hFF;
                m_tvalid <= 1'b1;
                busy     <= 1'b1;
                beat     <= 3'd1;
                cid      <= cid + 32'd1;
            end
        end else begin
            m_tdata  <= frame_q[{beat, 6'd0} +: 64];
            m_tkeep  <= (beat == 3'd7) ? 8'h0F : 8'hFF;
            m_tlast  <= (beat == 3'd7);
            m_tvalid <= 1'b1;
            if (beat == 3'd7) busy <= 1'b0;
            else              beat <= beat + 3'd1;
        end
    end
endmodule
