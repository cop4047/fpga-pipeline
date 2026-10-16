// mdp_top -- market-data -> order pipeline.
//
//   s_axis -> mdp_hdr_parse -> mdp_msg_asm -> msg FIFO -> mdp_book
//                                                           | order request
//                                          m_axis <- mdp_order_tx <- order FIFO
//
// Single clock domain (target 156.25 MHz, 64-bit datapath == 10G).  The MAC/PCS
// and the clock-domain crossing into this domain are outside this repo (see
// docs/SPEC.md, "Integration").
module mdp_top #(
    parameter int SYM_W   = 2,
    parameter int LVL_W   = 6,
    parameter int ORD_AW  = 10,
    parameter int MSGQ_AW = 5,
    parameter int ORDQ_AW = 3
) (
    input  logic        clk,
    input  logic        rst,
    // configuration (static while running)
    input  logic [15:0] cfg_md_dport,
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
    output logic [2:0]  tap_drop_reason,
    // statistics counters
    output logic [31:0] cnt_frm_good,
    output logic [31:0] cnt_frm_bad,
    output logic [31:0] cnt_msg,
    output logic [31:0] cnt_drop,
    output logic [31:0] cnt_ord,
    output logic [31:0] cnt_ovf,
    output logic        o_ready,         // RAM init sweep done; safe to send traffic
    output logic [MSGQ_AW:0] dbg_msgq_count,
    output logic [ORDQ_AW:0] dbg_ordq_count
);
    // ---- parse -------------------------------------------------------------
    logic        w_valid, w_first, frm_good, frm_bad;
    logic [63:0] w_data;
    logic [15:0] w_count;

    mdp_hdr_parse u_parse (
        .clk(clk), .rst(rst), .cfg_dport(cfg_md_dport),
        .s_tdata(s_tdata), .s_tvalid(s_tvalid), .s_tlast(s_tlast),
        .w_valid(w_valid), .w_first(w_first),
        .w_data(w_data), .w_count(w_count),
        .frm_good(frm_good), .frm_bad(frm_bad)
    );

    // ---- message assembly --------------------------------------------------
    logic         m_valid;
    logic [127:0] m_data;

    mdp_msg_asm u_asm (
        .clk(clk), .rst(rst),
        .w_valid(w_valid), .w_first(w_first),
        .w_data(w_data), .w_count(w_count),
        .m_valid(m_valid), .m_data(m_data)
    );

    // ---- message FIFO ------------------------------------------------------
    logic         mq_empty, mq_rd, mq_ovf;
    logic [127:0] mq_data;

    mdp_fifo #(.W(128), .AW(MSGQ_AW)) u_msg_fifo (
        .clk(clk), .rst(rst),
        .wr_en(m_valid), .wr_data(m_data),
        .rd_en(mq_rd), .rd_data(mq_data),
        .empty(mq_empty), .count(dbg_msgq_count), .ovf(mq_ovf)
    );

    // ---- book + trigger ----------------------------------------------------
    logic        ord_valid;
    logic [15:0] ord_sym;
    logic [31:0] ord_px, ord_qty;

    mdp_book #(.SYM_W(SYM_W), .LVL_W(LVL_W), .ORD_AW(ORD_AW)) u_book (
        .clk(clk), .rst(rst),
        .f_empty(mq_empty), .f_data(mq_data), .f_rd(mq_rd), .ready(o_ready),
        .cfg_base_px(cfg_base_px), .cfg_buy_px(cfg_buy_px),
        .cfg_qty(cfg_qty), .cfg_en(cfg_en),
        .ord_valid(ord_valid), .ord_sym(ord_sym), .ord_px(ord_px), .ord_qty(ord_qty),
        .drop_valid(tap_drop_valid), .drop_reason(tap_drop_reason),
        .tob_valid(tap_tob_valid), .tob_sym(tap_tob_sym),
        .tob_bid_vld(tap_tob_bid_vld), .tob_bid_px(tap_tob_bid_px),
        .tob_ask_vld(tap_tob_ask_vld), .tob_ask_px(tap_tob_ask_px)
    );

    // ---- order FIFO + transmitter -----------------------------------------
    logic        oq_empty, oq_rd, oq_ovf;
    logic [79:0] oq_data;

    mdp_fifo #(.W(80), .AW(ORDQ_AW)) u_ord_fifo (
        .clk(clk), .rst(rst),
        .wr_en(ord_valid), .wr_data({ord_sym, ord_px, ord_qty}),
        .rd_en(oq_rd), .rd_data(oq_data),
        .empty(oq_empty), .count(dbg_ordq_count), .ovf(oq_ovf)
    );

    mdp_order_tx u_tx (
        .clk(clk), .rst(rst),
        .q_empty(oq_empty), .q_data(oq_data), .q_rd(oq_rd),
        .cfg_dst_mac(cfg_dst_mac), .cfg_src_mac(cfg_src_mac),
        .cfg_src_ip(cfg_src_ip), .cfg_dst_ip(cfg_dst_ip),
        .cfg_sport(cfg_sport), .cfg_odport(cfg_odport), .cfg_ip_csum(cfg_ip_csum),
        .m_tdata(m_tdata), .m_tkeep(m_tkeep), .m_tvalid(m_tvalid), .m_tlast(m_tlast)
    );

    // ---- statistics --------------------------------------------------------
    always_ff @(posedge clk) begin
        if (rst) begin
            cnt_frm_good <= '0;
            cnt_frm_bad  <= '0;
            cnt_msg      <= '0;
            cnt_drop     <= '0;
            cnt_ord      <= '0;
            cnt_ovf      <= '0;
        end else begin
            if (frm_good)       cnt_frm_good <= cnt_frm_good + 32'd1;
            if (frm_bad)        cnt_frm_bad  <= cnt_frm_bad  + 32'd1;
            if (m_valid)        cnt_msg      <= cnt_msg      + 32'd1;
            if (tap_drop_valid) cnt_drop     <= cnt_drop     + 32'd1;
            if (ord_valid)      cnt_ord      <= cnt_ord      + 32'd1;
            if (mq_ovf || oq_ovf) cnt_ovf    <= cnt_ovf      + 32'd1;
        end
    end
endmodule
