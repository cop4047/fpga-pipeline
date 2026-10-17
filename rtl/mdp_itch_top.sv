// mdp_itch_top -- ITCH 5.0 / MoldUDP64 market-data -> order pipeline.
//
//   s_axis -> mdp_itch_frame -> msg FIFO* -> mdp_itch_book
//                                                | order request
//                               m_axis <- mdp_order_tx <- order FIFO*
//   (* both FIFOs are bypassed when empty, so an idle pipeline adds no queueing delay)
//
// Single clock domain (target 156.25 MHz, 64-bit datapath == 10G).  The MAC/PCS
// and the clock-domain crossing into this domain are outside this repo.
// Replaces mdp_top (ITCH-lite) for the ITCH 5.0 version; mdp_top is kept so the
// v1/v2 results stay reproducible.
module mdp_itch_top #(
    parameter int SYM_W   = 2,
    parameter int LVL_W   = 6,
    parameter int ORD_AW  = 10,
    parameter int TICK    = 100,
    parameter int MSGQ_AW = 6,
    parameter int ORDQ_AW = 3
) (
    input  logic        clk,
    input  logic        rst,
    // configuration (static while running)
    input  logic [15:0] cfg_md_dport,
    input  logic [79:0] cfg_session,
    input  logic [(1<<SYM_W)*64-1:0] cfg_watch,
    input  logic [31:0] cfg_base_px,
    input  logic [31:0] cfg_buy_px,
    input  logic [31:0] cfg_qty,
    input  logic        cfg_en,
    input  logic [47:0] cfg_dst_mac,
    input  logic [47:0] cfg_src_mac,
    input  logic [31:0] cfg_src_ip,
    input  logic [31:0] cfg_dst_ip,
    input  logic [15:0] cfg_sport,
    input  logic [15:0] cfg_odport,
    input  logic [15:0] cfg_ip_csum,
    // market data in (AXI-Stream, no back-pressure)
    input  logic [63:0] s_tdata,
    input  logic [7:0]  s_tkeep,
    input  logic        s_tvalid,
    input  logic        s_tlast,
    // orders out (AXI-Stream)
    output logic [63:0] m_tdata,
    output logic [7:0]  m_tkeep,
    output logic        m_tvalid,
    output logic        m_tlast,
    // observability taps (also what the testbench scoreboards against)
    output logic        tap_tob_valid,
    output logic [15:0] tap_tob_sym,
    output logic        tap_tob_bid_vld,
    output logic [31:0] tap_tob_bid_px,
    output logic        tap_tob_ask_vld,
    output logic [31:0] tap_tob_ask_px,
    output logic        tap_drop_valid,
    output logic [3:0]  tap_drop_reason,
    // statistics counters
    output logic [31:0] cnt_frm_good,
    output logic [31:0] cnt_frm_bad,
    output logic [31:0] cnt_hb,
    output logic [31:0] cnt_eos,
    output logic [31:0] cnt_gap,
    output logic [31:0] cnt_dup,
    output logic [31:0] cnt_trunc,
    output logic [31:0] cnt_msg,
    output logic [31:0] cnt_ign,
    output logic [31:0] cnt_drop,
    output logic [31:0] cnt_ord,
    output logic [31:0] cnt_ovf,
    output logic        o_ready,         // RAM init sweep done; safe to send traffic
    output logic [MSGQ_AW:0] dbg_msgq_count,
    output logic [ORDQ_AW:0] dbg_ordq_count
);
    // ---- framing -----------------------------------------------------------
    logic         f_valid, ev_good, ev_bad, ev_hb, ev_eos, ev_gap, ev_trunc, ev_dup;
    logic [15:0]  f_len;
    /* verilator lint_off UNUSEDSIGNAL */
    logic [319:0] f_data;          // only bytes 0..35 are used by the book
    /* verilator lint_on UNUSEDSIGNAL */

    mdp_itch_frame u_frame (
        .clk(clk), .rst(rst), .cfg_dport(cfg_md_dport), .cfg_session(cfg_session),
        .s_tdata(s_tdata), .s_tkeep(s_tkeep), .s_tvalid(s_tvalid), .s_tlast(s_tlast),
        .m_valid(f_valid), .m_len(f_len), .m_data(f_data),
        .ev_good(ev_good), .ev_bad(ev_bad), .ev_hb(ev_hb), .ev_eos(ev_eos),
        .ev_gap(ev_gap), .ev_trunc(ev_trunc), .ev_dup(ev_dup)
    );

    // ---- message FIFO with bypass (length + first 36 bytes) -------------------
    logic         mq_empty, mq_rd, mq_wr, mq_ovf;
    logic [303:0] mq_data, f_word, book_word;
    logic         book_in_valid, book_in_ready;

    assign f_word        = {f_len, f_data[319:32]};
    assign book_in_valid = !mq_empty || f_valid;
    assign book_word     = mq_empty ? f_word : mq_data;
    assign mq_rd         = !mq_empty && book_in_ready;
    assign mq_wr         = f_valid && !(mq_empty && book_in_ready);

    mdp_fifo #(.W(304), .AW(MSGQ_AW)) u_msg_fifo (
        .clk(clk), .rst(rst),
        .wr_en(mq_wr), .wr_data(f_word),
        .rd_en(mq_rd), .rd_data(mq_data),
        .empty(mq_empty), .count(dbg_msgq_count), .ovf(mq_ovf)
    );

    // ---- book + trigger ----------------------------------------------------
    logic        ord_valid, ign_valid;
    logic [15:0] ord_sym;
    logic [LVL_W-1:0] ord_lvl;
    logic [31:0] ord_qty;

    mdp_itch_book #(.SYM_W(SYM_W), .LVL_W(LVL_W), .ORD_AW(ORD_AW), .TICK(TICK)) u_book (
        .clk(clk), .rst(rst),
        .in_valid(book_in_valid), .in_len(book_word[303:288]), .in_data(book_word[287:0]),
        .in_ready(book_in_ready), .ready(o_ready),
        .cfg_base_px(cfg_base_px), .cfg_buy_px(cfg_buy_px),
        .cfg_qty(cfg_qty), .cfg_en(cfg_en), .cfg_watch(cfg_watch),
        .ord_valid(ord_valid), .ord_sym(ord_sym), .ord_lvl(ord_lvl), .ord_qty(ord_qty),
        .drop_valid(tap_drop_valid), .drop_reason(tap_drop_reason), .ign_valid(ign_valid),
        .tob_valid(tap_tob_valid), .tob_sym(tap_tob_sym),
        .tob_bid_vld(tap_tob_bid_vld), .tob_bid_px(tap_tob_bid_px),
        .tob_ask_vld(tap_tob_ask_vld), .tob_ask_px(tap_tob_ask_px)
    );

    // ---- order FIFO with bypass + transmitter -------------------------------
    wire [31:0] ord_px   = cfg_base_px + 32'(ord_lvl) * 32'(TICK);
    wire [79:0] ord_word = {ord_sym, ord_px, ord_qty};

    logic        oq_empty, oq_rd, oq_wr, oq_ovf;
    logic [79:0] oq_data;
    logic        tx_req_valid, tx_req_ready;
    logic [79:0] tx_req_data;

    assign tx_req_valid = !oq_empty || ord_valid;
    assign tx_req_data  = oq_empty ? ord_word : oq_data;
    assign oq_rd        = !oq_empty && tx_req_ready;
    assign oq_wr        = ord_valid && !(oq_empty && tx_req_ready);

    mdp_fifo #(.W(80), .AW(ORDQ_AW)) u_ord_fifo (
        .clk(clk), .rst(rst),
        .wr_en(oq_wr), .wr_data(ord_word),
        .rd_en(oq_rd), .rd_data(oq_data),
        .empty(oq_empty), .count(dbg_ordq_count), .ovf(oq_ovf)
    );

    mdp_order_tx u_tx (
        .clk(clk), .rst(rst),
        .req_valid(tx_req_valid), .req_data(tx_req_data), .req_ready(tx_req_ready),
        .cfg_dst_mac(cfg_dst_mac), .cfg_src_mac(cfg_src_mac),
        .cfg_src_ip(cfg_src_ip), .cfg_dst_ip(cfg_dst_ip),
        .cfg_sport(cfg_sport), .cfg_odport(cfg_odport), .cfg_ip_csum(cfg_ip_csum),
        .m_tdata(m_tdata), .m_tkeep(m_tkeep), .m_tvalid(m_tvalid), .m_tlast(m_tlast)
    );

    // ---- statistics --------------------------------------------------------
    always_ff @(posedge clk) begin
        if (rst) begin
            cnt_frm_good <= '0; cnt_frm_bad <= '0; cnt_hb   <= '0; cnt_eos  <= '0;
            cnt_gap      <= '0; cnt_dup     <= '0; cnt_trunc <= '0; cnt_msg <= '0;
            cnt_ign      <= '0; cnt_drop    <= '0; cnt_ord  <= '0; cnt_ovf  <= '0;
        end else begin
            if (ev_good)        cnt_frm_good <= cnt_frm_good + 32'd1;
            if (ev_bad)         cnt_frm_bad  <= cnt_frm_bad  + 32'd1;
            if (ev_hb)          cnt_hb       <= cnt_hb       + 32'd1;
            if (ev_eos)         cnt_eos      <= cnt_eos      + 32'd1;
            if (ev_gap)         cnt_gap      <= cnt_gap      + 32'd1;
            if (ev_dup)         cnt_dup      <= cnt_dup      + 32'd1;
            if (ev_trunc)       cnt_trunc    <= cnt_trunc    + 32'd1;
            if (f_valid)        cnt_msg      <= cnt_msg      + 32'd1;
            if (ign_valid)      cnt_ign      <= cnt_ign      + 32'd1;
            if (tap_drop_valid) cnt_drop     <= cnt_drop     + 32'd1;
            if (ord_valid)      cnt_ord      <= cnt_ord      + 32'd1;
            if (mq_ovf || oq_ovf) cnt_ovf    <= cnt_ovf      + 32'd1;
        end
    end
endmodule
