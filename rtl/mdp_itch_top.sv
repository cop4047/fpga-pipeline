// mdp_itch_top -- ITCH 5.0 / MoldUDP64 market-data -> order pipeline, with a PTP-style
// hardware timestamp clock and an optional clock-domain crossing into the core.
//
//   s_axis -> mdp_itch_frame -> msg FIFO -> mdp_itch_book -> order FIFO -> mdp_order_tx -> m_axis
//   |_______ network clock `clk` ______|  |__ core clock __|  |______ network clock `clk` ______|
//
// ASYNC_CORE = 0  everything runs on `clk`; both FIFOs are synchronous and bypassed when
//                 empty, so an idle pipeline adds no queueing delay (the v3 behaviour;
//                 `clk_core` is ignored).
// ASYNC_CORE = 1  the book and trigger run on `clk_core`, unrelated to `clk`.  The two FIFOs
//                 become Gray-coded asynchronous FIFOs (mdp_async_fifo) -- the only places
//                 data crosses clocks -- and each domain gets its own reset synchroniser.
//
// Timestamps: one free-running real-time clock (mdp_ptp_rtc) lives in the network domain.
// Each message is stamped when its last byte is presented on s_axis; the stamp rides with the
// message through the book into the order request and across both FIFOs as ordinary data,
// so no timestamp ever needs a clock crossing.  When the order transmitter accepts the
// request, the latency is (clock now) - (stamp), both from the same clock, and is
// accumulated by mdp_lat_stats.  Latency therefore includes the CDC cost, in real time.
//
// Static configuration (cfg_*) is quasi-static: written while the pipeline is idle and
// read in whichever domain uses it, without synchronisers (constrain as false paths).
// Statistics counters sit in the domain of the event they count and are meant to be read
// when traffic has stopped.
module mdp_itch_top #(
    parameter int SYM_W      = 2,
    parameter int LVL_W      = 6,
    parameter int ORD_AW     = 10,
    parameter int TICK       = 100,
    parameter int MSGQ_AW    = 6,
    parameter int ORDQ_AW    = 3,
    parameter int ASYNC_CORE = 0,
    parameter int TS_W       = 40       // timestamp = rtc[63 -: TS_W]: 32 bits of ns + 8 fractional bits
) (
    input  logic        clk,            // network clock (156.25 MHz for 10G)
    input  logic        clk_core,       // core clock (used only when ASYNC_CORE = 1)
    input  logic        rst,            // active high; asynchronous when ASYNC_CORE = 1
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
    // hardware clock control (network domain)
    input  logic [39:0] cfg_ptp_inc,    // ns per clock, 8.32 fixed point (156.25 MHz: 0x666666666)
    input  logic signed [31:0] cfg_ptp_trim,   // rate trim, 2^-32 ns per clock
    input  logic        ptp_set_en,     // step the clock to ptp_set_time at this edge
    input  logic [63:0] ptp_set_time,   // 32.32 ns
    input  logic        stat_clear,     // restart the latency statistics
    output logic [63:0] ptp_time,
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
    // observability taps (also what the testbench scoreboards against); core domain
    output logic        tap_tob_valid,
    output logic [15:0] tap_tob_sym,
    output logic        tap_tob_bid_vld,
    output logic [31:0] tap_tob_bid_px,
    output logic        tap_tob_ask_vld,
    output logic [31:0] tap_tob_ask_px,
    output logic        tap_drop_valid,
    output logic [3:0]  tap_drop_reason,
    // hardware latency measurement (network domain; units of 2^-(TS_W-32) ns)
    output logic        lat_valid,      // one pulse per order
    output logic [TS_W-1:0] lat_value,
    output logic [TS_W-1:0] lat_last,
    output logic [TS_W-1:0] lat_min,
    output logic [TS_W-1:0] lat_max,
    output logic [63:0] lat_sum,
    output logic [31:0] lat_cnt,
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
    // ---- clocks and resets ------------------------------------------------------
    wire  clk_c = (ASYNC_CORE != 0) ? clk_core : clk;       // clock of the book / core domain
    logic rst_n, rst_c;                              // reset of the network / core domain
    if (ASYNC_CORE != 0) begin : g_rst
        mdp_rst_sync u_rn (.clk(clk),      .arst(rst), .rst_o(rst_n));
        mdp_rst_sync u_rc (.clk(clk_core), .arst(rst), .rst_o(rst_c));
    end else begin : g_rst
        assign rst_n = rst;
        assign rst_c = rst;
    end

    // ---- hardware clock ---------------------------------------------------------
    mdp_ptp_rtc u_rtc (
        .clk(clk), .rst(rst_n), .cfg_inc(cfg_ptp_inc), .cfg_trim(cfg_ptp_trim),
        .set_en(ptp_set_en), .set_time(ptp_set_time), .time_q(ptp_time)
    );
    wire [TS_W-1:0] ts_now = ptp_time[63 -: TS_W];

    // ---- framing (network domain) ---------------------------------------------------
    logic         f_valid, ev_good, ev_bad, ev_hb, ev_eos, ev_gap, ev_trunc, ev_dup;
    logic [15:0]  f_len;
    logic [TS_W-1:0] f_ts;
    /* verilator lint_off UNUSEDSIGNAL */
    logic [319:0] f_data;          // only bytes 0..35 are used by the book
    /* verilator lint_on UNUSEDSIGNAL */

    mdp_itch_frame #(.TS_W(TS_W)) u_frame (
        .clk(clk), .rst(rst_n), .cfg_dport(cfg_md_dport), .cfg_session(cfg_session),
        .s_tdata(s_tdata), .s_tkeep(s_tkeep), .s_tvalid(s_tvalid), .s_tlast(s_tlast),
        .ts_now(ts_now),
        .m_valid(f_valid), .m_len(f_len), .m_ts(f_ts), .m_data(f_data),
        .ev_good(ev_good), .ev_bad(ev_bad), .ev_hb(ev_hb), .ev_eos(ev_eos),
        .ev_gap(ev_gap), .ev_trunc(ev_trunc), .ev_dup(ev_dup)
    );

    // ---- message FIFO: {timestamp, length, first 36 bytes} -------------------------
    localparam int MW = TS_W + 304;
    logic          mq_empty, mq_rd, mq_ovf;
    logic [MW-1:0] mq_data, f_word, book_word;
    logic          book_in_valid, book_in_ready;
    assign f_word = {f_ts, f_len, f_data[319:32]};

    /* verilator lint_off PINCONNECTEMPTY */
    if (ASYNC_CORE != 0) begin : g_mq
        // network -> core crossing
        mdp_async_fifo #(.W(MW), .AW(MSGQ_AW)) u_msg_fifo (
            .wclk(clk), .wrst(rst_n), .wr_en(f_valid), .wr_data(f_word),
            .wfull(), .wovf(mq_ovf), .wcount(dbg_msgq_count),
            .rclk(clk_core), .rrst(rst_c), .rd_en(mq_rd), .rd_data(mq_data),
            .rempty(mq_empty), .rcount()
        );
        assign book_in_valid = !mq_empty;
        assign book_word     = mq_data;
        assign mq_rd         = !mq_empty && book_in_ready;
    end else begin : g_mq
        logic mq_wr;
        assign book_in_valid = !mq_empty || f_valid;
        assign book_word     = mq_empty ? f_word : mq_data;
        assign mq_rd         = !mq_empty && book_in_ready;
        assign mq_wr         = f_valid && !(mq_empty && book_in_ready);
        mdp_fifo #(.W(MW), .AW(MSGQ_AW)) u_msg_fifo (
            .clk(clk), .rst(rst_n),
            .wr_en(mq_wr), .wr_data(f_word),
            .rd_en(mq_rd), .rd_data(mq_data),
            .empty(mq_empty), .count(dbg_msgq_count), .ovf(mq_ovf)
        );
    end

    /* verilator lint_on PINCONNECTEMPTY */

    // ---- book + trigger (core domain) -----------------------------------------------
    logic        ord_valid, ign_valid;
    logic [15:0] ord_sym;
    logic [LVL_W-1:0] ord_lvl;
    logic [31:0] ord_qty;
    logic [TS_W-1:0] ord_ts;

    mdp_itch_book #(.SYM_W(SYM_W), .LVL_W(LVL_W), .ORD_AW(ORD_AW), .TICK(TICK), .TS_W(TS_W)) u_book (
        .clk(clk_c), .rst(rst_c),
        .in_valid(book_in_valid), .in_len(book_word[303:288]), .in_data(book_word[287:0]),
        .in_ts(book_word[MW-1:304]),
        .in_ready(book_in_ready), .ready(o_ready),
        .cfg_base_px(cfg_base_px), .cfg_buy_px(cfg_buy_px),
        .cfg_qty(cfg_qty), .cfg_en(cfg_en), .cfg_watch(cfg_watch),
        .ord_valid(ord_valid), .ord_sym(ord_sym), .ord_lvl(ord_lvl), .ord_qty(ord_qty), .ord_ts(ord_ts),
        .drop_valid(tap_drop_valid), .drop_reason(tap_drop_reason), .ign_valid(ign_valid),
        .tob_valid(tap_tob_valid), .tob_sym(tap_tob_sym),
        .tob_bid_vld(tap_tob_bid_vld), .tob_bid_px(tap_tob_bid_px),
        .tob_ask_vld(tap_tob_ask_vld), .tob_ask_px(tap_tob_ask_px)
    );

    // ---- order FIFO: {timestamp, sym, price, qty} ------------------------------------
    localparam int OWD = 80 + TS_W;
    wire [31:0]     ord_px   = cfg_base_px + 32'(ord_lvl) * 32'(TICK);
    wire [OWD-1:0]  ord_word = {ord_ts, ord_sym, ord_px, ord_qty};

    logic           oq_empty, oq_rd, oq_ovf;
    logic [OWD-1:0] oq_data;
    logic           tx_req_valid, tx_req_ready;
    logic [OWD-1:0] tx_req_word;

    /* verilator lint_off PINCONNECTEMPTY */
    if (ASYNC_CORE != 0) begin : g_oq
        // core -> network crossing
        mdp_async_fifo #(.W(OWD), .AW(ORDQ_AW)) u_ord_fifo (
            .wclk(clk_core), .wrst(rst_c), .wr_en(ord_valid), .wr_data(ord_word),
            .wfull(), .wovf(oq_ovf), .wcount(),
            .rclk(clk), .rrst(rst_n), .rd_en(oq_rd), .rd_data(oq_data),
            .rempty(oq_empty), .rcount(dbg_ordq_count)
        );
        assign tx_req_valid = !oq_empty;
        assign tx_req_word  = oq_data;
        assign oq_rd        = !oq_empty && tx_req_ready;
    end else begin : g_oq
        logic oq_wr;
        assign tx_req_valid = !oq_empty || ord_valid;
        assign tx_req_word  = oq_empty ? ord_word : oq_data;
        assign oq_rd        = !oq_empty && tx_req_ready;
        assign oq_wr        = ord_valid && !(oq_empty && tx_req_ready);
        mdp_fifo #(.W(OWD), .AW(ORDQ_AW)) u_ord_fifo (
            .clk(clk), .rst(rst_n),
            .wr_en(oq_wr), .wr_data(ord_word),
            .rd_en(oq_rd), .rd_data(oq_data),
            .empty(oq_empty), .count(dbg_ordq_count), .ovf(oq_ovf)
        );
    end

    /* verilator lint_on PINCONNECTEMPTY */

    // ---- order transmitter (network domain) -----------------------------------------
    mdp_order_tx u_tx (
        .clk(clk), .rst(rst_n),
        .req_valid(tx_req_valid), .req_data(tx_req_word[79:0]), .req_ready(tx_req_ready),
        .cfg_dst_mac(cfg_dst_mac), .cfg_src_mac(cfg_src_mac),
        .cfg_src_ip(cfg_src_ip), .cfg_dst_ip(cfg_dst_ip),
        .cfg_sport(cfg_sport), .cfg_odport(cfg_odport), .cfg_ip_csum(cfg_ip_csum),
        .m_tdata(m_tdata), .m_tkeep(m_tkeep), .m_tvalid(m_tvalid), .m_tlast(m_tlast)
    );

    // ---- hardware latency: egress time - ingress stamp, same clock ---------------------
    // Taken in the cycle in which the transmitter accepts the request (the edge that follows
    // launches beat 0).  Ingress is stamped in the cycle the message's last byte is presented
    // (the edge that follows captures it), so the difference is exactly "capture edge of the
    // last input byte -> launch edge of the first output beat".
    wire tx_start = tx_req_valid && tx_req_ready;
    mdp_lat_stats #(.W(TS_W)) u_lat (
        .clk(clk), .rst(rst_n), .clear(stat_clear),
        .valid(tx_start), .lat(ts_now - tx_req_word[OWD-1:80]),
        .o_valid(lat_valid), .o_lat(lat_value),
        .s_last(lat_last), .s_min(lat_min), .s_max(lat_max), .s_sum(lat_sum), .s_cnt(lat_cnt)
    );

    // ---- statistics ----------------------------------------------------------------
    logic [31:0] cnt_ovf_n, cnt_ovf_c;
    always_ff @(posedge clk) begin
        if (rst_n) begin
            cnt_frm_good <= '0; cnt_frm_bad <= '0; cnt_hb   <= '0; cnt_eos  <= '0;
            cnt_gap      <= '0; cnt_dup     <= '0; cnt_trunc <= '0; cnt_msg <= '0;
            cnt_ovf_n    <= '0;
        end else begin
            if (ev_good)        cnt_frm_good <= cnt_frm_good + 32'd1;
            if (ev_bad)         cnt_frm_bad  <= cnt_frm_bad  + 32'd1;
            if (ev_hb)          cnt_hb       <= cnt_hb       + 32'd1;
            if (ev_eos)         cnt_eos      <= cnt_eos      + 32'd1;
            if (ev_gap)         cnt_gap      <= cnt_gap      + 32'd1;
            if (ev_dup)         cnt_dup      <= cnt_dup      + 32'd1;
            if (ev_trunc)       cnt_trunc    <= cnt_trunc    + 32'd1;
            if (f_valid)        cnt_msg      <= cnt_msg      + 32'd1;
            if (mq_ovf)         cnt_ovf_n    <= cnt_ovf_n    + 32'd1;
        end
    end
    // order-FIFO overflow is counted where the write happens: the core domain (network when synchronous)
    wire clk_oq = (ASYNC_CORE != 0) ? clk_core : clk;
    always_ff @(posedge clk_oq) begin
        if (rst_c) begin
            cnt_ign <= '0; cnt_drop <= '0; cnt_ord <= '0; cnt_ovf_c <= '0;
        end else begin
            if (ign_valid)      cnt_ign      <= cnt_ign      + 32'd1;
            if (tap_drop_valid) cnt_drop     <= cnt_drop     + 32'd1;
            if (ord_valid)      cnt_ord      <= cnt_ord      + 32'd1;
            if (oq_ovf)         cnt_ovf_c    <= cnt_ovf_c    + 32'd1;
        end
    end
    assign cnt_ovf = cnt_ovf_n + cnt_ovf_c;       // read when quiescent
endmodule
