// mdp_itch_frame -- Ethernet/IPv4/UDP + MoldUDP64 screening and ITCH message-block
// extraction from a 64-bit AXI-Stream (byte 0 in tdata[7:0], no back-pressure).
//
// Frame:  Eth 14 | IPv4 20 | UDP 8 | MoldUDP64 20 | blocks (length[2] + message)
// The first block starts at frame byte 62 (beat 7, lane 6); later blocks start
// at arbitrary lanes.  Rules (identical to model/itch50.py):
//   * header checks: ethertype 0x0800, IPv4 IHL 5, UDP, dst port, session; frames
//     shorter than 62 bytes are bad
//   * count 0 = heartbeat, 0xFFFF = end of session: sequence sync only
//   * sequence: first packet sets the expectation; seq > expected = gap (resync);
//     seq < expected = the first (expected - seq) messages are duplicates
//   * a block shorter than 12 bytes ends the packet (counted as `ev_trunc`);
//     so does a frame that ends before `count` complete blocks have arrived
//   * a complete, non-duplicate block is presented on m_* for one cycle:
//     m_data holds message bytes in network order (byte 0 = m_data[319:312]);
//     only the first min(m_len, 40) bytes are meaningful.
//
// Block tracking: `nb` is the distance (in bytes, from lane 0 of the beat being
// processed) to the end of the newest learned block, which is also the start of
// the next one.  The 2-byte length is read straight from the beat with an 8:1 byte
// mux; at most one block starts per beat because blocks are >= 14 bytes with
// their prefix.  Two mdp_itch_unit instances alternate between blocks.
module mdp_itch_frame (
    input  logic         clk,
    input  logic         rst,
    input  logic [15:0]  cfg_dport,
    input  logic [79:0]  cfg_session,
    // AXI-Stream in
    input  logic [63:0]  s_tdata,
    input  logic [7:0]   s_tkeep,       // contiguous from lane 0 (0xFF except on tlast)
    input  logic         s_tvalid,
    input  logic         s_tlast,
    // extracted messages
    output logic         m_valid,
    output logic [15:0]  m_len,
    output logic [319:0] m_data,
    // one-cycle event pulses
    output logic         ev_good,
    output logic         ev_bad,
    output logic         ev_hb,
    output logic         ev_eos,
    output logic         ev_gap,
    output logic         ev_trunc,
    output logic         ev_dup
);
    localparam int MIN_BLOCK = 12;

    function automatic logic [7:0] lane(input logic [63:0] d, input logic [2:0] i);
        lane = d[8*i +: 8];
    endfunction

    // ------------------------------------------------------------ beat state
    logic [3:0]  bidx;                 // beat index within frame, saturates at 8
    logic [63:0] prv;
    logic        flush_q;              // cycle after tlast: lets units finish their last word

    // header accumulation
    logic        ok_q;
    logic [31:0] seq_hi_q;
    logic [63:0] seq_q;
    logic        good_q, hb_q, eos_q;

    // block tracker
    logic signed [17:0] nb_q;
    logic [15:0] left_q, kcnt_q;
    logic        pend_q, infl_q, endu_q, trunc_done_q;
    logic [7:0]  hi_q;

    wire first_b = (bidx == 4'd7);
    wire in_blk  = s_tvalid && (bidx >= 4'd7);

    // beat 7 header completion (combinational, used the same cycle)
    wire [63:0] seq_n   = {seq_hi_q, lane(s_tdata, 3'd0), lane(s_tdata, 3'd1),
                           lane(s_tdata, 3'd2), lane(s_tdata, 3'd3)};
    wire [15:0] count_n = {lane(s_tdata, 3'd4), lane(s_tdata, 3'd5)};
    wire        good7   = ok_q && (!s_tlast || s_tkeep[5]);
    wire        data7   = (count_n != 16'd0) && (count_n != 16'hFFFF);
    wire        good_eff = first_b ? good7 : good_q;

    wire signed [17:0] nb_eff = nb_q;
    wire [15:0] left_eff = first_b ? ((good7 && data7) ? count_n : 16'd0) : left_q;

    // valid lanes in this beat
    logic [3:0] nvl;
    always_comb begin
        nvl = 4'd0;
        for (int i = 0; i < 8; i++) nvl = nvl + 4'(s_tkeep[i]);
    end

    // ---- locate a block start in this beat
    logic               learn, pend_set, dead;
    logic [2:0]         j;
    logic [15:0]        len_n;
    logic signed [17:0] jstart;
    always_comb begin
        learn = 1'b0; pend_set = 1'b0; dead = 1'b0;
        len_n = 16'd0; jstart = 18'sd0; j = nb_eff[2:0];
        if (in_blk && left_eff != 16'd0) begin
            if (pend_q) begin
                if (s_tkeep[0]) begin
                    learn = 1'b1; len_n = {hi_q, lane(s_tdata, 3'd0)}; jstart = -18'sd1;
                end
            end else if (nb_eff >= 18'sd0 && nb_eff <= 18'sd6) begin
                if (s_tkeep[j] && s_tkeep[j + 3'd1]) begin
                    learn = 1'b1; len_n = {lane(s_tdata, j), lane(s_tdata, j + 3'd1)};
                    jstart = nb_eff;
                end
            end else if (nb_eff == 18'sd7) begin
                if (s_tkeep[7]) pend_set = 1'b1;
            end
        end
        if (learn && len_n < 16'(MIN_BLOCK)) dead = 1'b1;
    end
    wire learn_ok = learn && !dead;

    logic signed [17:0] nb_n;
    always_comb begin
        if (dead)          nb_n = -18'sd8;
        else if (learn)    nb_n = jstart + $signed({2'b00, len_n}) - 18'sd6;
        else if (nb_eff <= -18'sd16) nb_n = nb_eff;
        else               nb_n = nb_eff - 18'sd8;
    end

    // ---- a block's last byte is in this beat (and it is a valid lane)
    wire end_ev = in_blk && infl_q && (nb_eff >= 18'sd1) && (nb_eff <= 18'sd8) &&
                  (nb_eff <= $signed({14'd0, nvl}));
    wire infl_n = learn_ok || (infl_q && !end_ev);
    wire [15:0] left_n = dead ? 16'd0 : (learn_ok ? left_eff - 16'd1 : left_eff);

    // block -> unit parameters
    wire [2:0] bl = (jstart == -18'sd1) ? 3'd1 : (jstart[2:0] + 3'd2);
    wire       sk = (jstart == 18'sd6);

    // ------------------------------------------------------------ units
    wire         tick = s_tvalid | flush_q;
    logic        d0_msg, d0_dup, d1_msg, d1_dup;
    logic [15:0] l0, l1;
    logic [319:0] msg0, msg1;
    logic [16:0] skip_q;

    mdp_itch_unit u0 (
        .clk(clk), .rst(rst), .tick(tick), .prv(prv), .cur(s_tdata),
        .load(learn_ok && !kcnt_q[0]), .ld_r(bl), .ld_skip1(sk), .ld_len(len_n), .ld_blk(kcnt_q),
        .end_ev(end_ev && !endu_q), .clear(flush_q), .skip(skip_q),
        .done_msg(d0_msg), .done_dup(d0_dup), .o_len(l0), .msg(msg0)
    );
    mdp_itch_unit u1 (
        .clk(clk), .rst(rst), .tick(tick), .prv(prv), .cur(s_tdata),
        .load(learn_ok && kcnt_q[0]), .ld_r(bl), .ld_skip1(sk), .ld_len(len_n), .ld_blk(kcnt_q),
        .end_ev(end_ev && endu_q), .clear(flush_q), .skip(skip_q),
        .done_msg(d1_msg), .done_dup(d1_dup), .o_len(l1), .msg(msg1)
    );

    assign m_valid = d0_msg | d1_msg;
    assign m_len   = d1_msg ? l1 : l0;
    assign m_data  = d1_msg ? msg1 : msg0;

    // ------------------------------------------------------------ sequence tracking
    logic [63:0] exp_q;
    logic        has_exp_q;
    logic        sync1_q, sync2_q;
    logic        gt_q, lt_q;
    logic [63:0] diff_q;

    always_ff @(posedge clk) begin
        ev_hb  <= 1'b0;
        ev_eos <= 1'b0;
        ev_gap <= 1'b0;
        ev_dup <= d0_dup | d1_dup;
        sync2_q <= sync1_q;
        if (rst) begin
            exp_q <= 64'd0; has_exp_q <= 1'b0; skip_q <= 17'd0;
            sync2_q <= 1'b0; gt_q <= 1'b0; lt_q <= 1'b0; diff_q <= 64'd0;
        end else begin
            if (sync1_q) begin                       // stage 1: compare
                gt_q   <= (seq_q > exp_q);
                lt_q   <= (seq_q < exp_q);
                diff_q <= exp_q - seq_q;
            end
            if (sync2_q) begin                       // stage 2: apply
                ev_hb  <= hb_q;
                ev_eos <= eos_q;
                skip_q <= 17'd0;
                if (!has_exp_q) begin
                    exp_q <= seq_q; has_exp_q <= 1'b1;
                end else if (gt_q) begin
                    ev_gap <= 1'b1; exp_q <= seq_q;
                end else if (lt_q) begin
                    skip_q <= (diff_q > 64'h1FFFF) ? 17'h1FFFF : diff_q[16:0];
                end
            end else if (m_valid) begin
                exp_q <= exp_q + 64'd1;
            end
        end
    end

    // ------------------------------------------------------------ frame / header / tracker state
    always_ff @(posedge clk) begin
        ev_good  <= 1'b0;
        ev_bad   <= 1'b0;
        ev_trunc <= 1'b0;
        flush_q  <= 1'b0;
        sync1_q  <= 1'b0;
        if (rst) begin
            bidx <= 4'd0; prv <= 64'd0; ok_q <= 1'b1; seq_hi_q <= 32'd0; seq_q <= 64'd0;
            good_q <= 1'b0; hb_q <= 1'b0; eos_q <= 1'b0;
            nb_q <= 18'sd6; left_q <= 16'd0; kcnt_q <= 16'd0; pend_q <= 1'b0; infl_q <= 1'b0;
            endu_q <= 1'b0; trunc_done_q <= 1'b0; hi_q <= 8'd0;
        end else if (s_tvalid) begin
            prv <= s_tdata;
            if (bidx != 4'd8) bidx <= bidx + 4'd1;
            case (bidx)
                4'd1: ok_q <= ok_q && (s_tdata[47:32] == 16'h0008) && (s_tdata[55:48] == 8'h45);
                4'd2: ok_q <= ok_q && (s_tdata[63:56] == 8'h11);
                4'd4: ok_q <= ok_q && ({s_tdata[39:32], s_tdata[47:40]} == cfg_dport);
                4'd5: ok_q <= ok_q && ({lane(s_tdata, 3'd2), lane(s_tdata, 3'd3), lane(s_tdata, 3'd4),
                                        lane(s_tdata, 3'd5), lane(s_tdata, 3'd6), lane(s_tdata, 3'd7)}
                                       == cfg_session[79:32]);
                4'd6: begin
                    ok_q     <= ok_q && ({lane(s_tdata, 3'd0), lane(s_tdata, 3'd1), lane(s_tdata, 3'd2),
                                          lane(s_tdata, 3'd3)} == cfg_session[31:0]);
                    seq_hi_q <= {lane(s_tdata, 3'd4), lane(s_tdata, 3'd5), lane(s_tdata, 3'd6),
                                 lane(s_tdata, 3'd7)};
                end
                4'd7: begin
                    seq_q   <= seq_n;
                    good_q  <= good7;
                    hb_q    <= (count_n == 16'd0);
                    eos_q   <= (count_n == 16'hFFFF);
                    sync1_q <= good7;
                end
                default: ;
            endcase
            if (in_blk) begin
                left_q  <= left_n;
                nb_q    <= nb_n;
                pend_q  <= pend_set;
                if (pend_set) hi_q <= lane(s_tdata, 3'd7);
                infl_q  <= infl_n;
                if (learn_ok) kcnt_q <= kcnt_q + 16'd1;
                if (end_ev)   endu_q <= !endu_q;
                if (dead) begin
                    ev_trunc     <= 1'b1;
                    trunc_done_q <= 1'b1;
                end
            end
            if (s_tlast) begin
                flush_q <= 1'b1;
                ev_good <= (bidx >= 4'd7) && good_eff;
                ev_bad  <= !((bidx >= 4'd7) && good_eff);
                if (in_blk && !trunc_done_q && !dead &&
                    (left_n != 16'd0 || pend_set || infl_n))
                    ev_trunc <= 1'b1;
                bidx <= 4'd0; ok_q <= 1'b1; nb_q <= 18'sd6; left_q <= 16'd0; kcnt_q <= 16'd0;
                pend_q <= 1'b0; infl_q <= 1'b0; endu_q <= 1'b0; trunc_done_q <= 1'b0; good_q <= 1'b0;
            end
        end
    end
endmodule
