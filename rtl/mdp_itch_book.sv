// mdp_itch_book -- ITCH 5.0 order book + trigger (one message in flight).
//
// Takes one framed ITCH message (first 36 bytes, network order, byte 0 at the
// top; see mdp_itch_frame) and applies the rules in docs/ITCH50.md exactly as
// model/itch50.py does:
//   A/F add, E/C/X reduce, D delete, U replace, every other type is "ignored".
//
// Data structures
//   ord_mem : direct-mapped order table indexed by ref[ORD_AW-1:0].  Each entry
//             holds {valid, tag = ref[63:ORD_AW], sym, side, level, remaining}
//             so a lookup is exact on the full 64-bit reference.
//   lvl_mem : aggregate shares per {symbol, side, level}.
//   mask    : occupancy bit per level per {symbol, side} (flops), so best bid /
//             ask is a priority encode.
// Prices live in a window of N_LVL ticks (TICK price units each) from cfg_base_px.
//
// Pipeline (states), cycles counted from the cycle the message is accepted:
//   IDLE/BT  accept: capture the message, issue the order-table read (cycle 0)
//   D1       normalise: type, length, side, watchlist match, price offset
//            (px - base), shares, order-table hit/tag compare        (cycle 1)
//   D2       price offset -> level (one-hot compare), delta/remaining
//            maths, save entry fields.  Unknown types and wrong lengths
//            leave here.                                             (cycle 2)
//   RD       final accept/drop decision (all checks, priority as in the
//            model); issue the level-RAM read                        (cycle 3)
//   WR       level RAM + occupancy + order table update              (cycle 4)
//            (a replace runs RD/WR twice: remove old, add new)
//   BT       best bid/ask + trigger -> order request, top-of-book    (cycle 5)
// An Add therefore leaves the book 6 cycles after accept (order_valid visible in
// cycle 6).  BT can overlap the accept of the next message (nothing in BT touches
// the captured message), so back-to-back Adds are accepted every 5 cycles.
module mdp_itch_book #(
    parameter int SYM_W  = 2,
    parameter int LVL_W  = 6,
    parameter int ORD_AW = 10,
    parameter int TICK   = 100,
    parameter int TS_W   = 40
) (
    input  logic         clk,
    input  logic         rst,
    // message input (valid/ready); only the first 36 bytes are needed
    input  logic         in_valid,
    input  logic [15:0]  in_len,
    input  logic [287:0] in_data,
    input  logic [TS_W-1:0] in_ts,      // ingress timestamp of this message (carried to the order)
    output logic         in_ready,
    output logic         ready,        // init sweep finished (RAMs cleared)
    // configuration (static while running)
    input  logic [31:0]  cfg_base_px,
    input  logic [31:0]  cfg_buy_px,
    input  logic [31:0]  cfg_qty,
    input  logic         cfg_en,
    input  logic [(1<<SYM_W)*64-1:0] cfg_watch,   // 8-byte stock names, symbol i at [64*i +: 64]
    // order request out (one-cycle pulse); price = cfg_base_px + ord_lvl * TICK
    output logic         ord_valid,
    output logic [15:0]  ord_sym,
    output logic [LVL_W-1:0] ord_lvl,
    output logic [31:0]  ord_qty,
    output logic [TS_W-1:0] ord_ts,     // ingress timestamp of the message that triggered the order
    // message dropped / ignored (one-cycle pulses)
    output logic         drop_valid,
    output logic [3:0]   drop_reason,
    output logic         ign_valid,
    // top-of-book after every accepted update (one-cycle pulse)
    output logic         tob_valid,
    output logic [15:0]  tob_sym,
    output logic         tob_bid_vld,
    output logic [31:0]  tob_bid_px,
    output logic         tob_ask_vld,
    output logic [31:0]  tob_ask_px
);
    localparam int N_SYM  = 1 << SYM_W;
    localparam int N_LVL  = 1 << LVL_W;
    localparam int TAG_W  = 64 - ORD_AW;
    localparam int OW     = 1 + TAG_W + SYM_W + 1 + LVL_W + 32;   // order-table entry
    localparam int LAW    = SYM_W + 1 + LVL_W;                    // level-RAM address
    localparam int DW     = $clog2(N_LVL * TICK);                 // bits of (px - base) that matter

    localparam logic [2:0] S_IDLE = 3'd0, S_D1 = 3'd1, S_D2 = 3'd2, S_RD = 3'd3,
                           S_WR   = 3'd4, S_BT = 3'd5;

    localparam logic [3:0] R_BAD_LEN = 4'd1, R_BAD_SIDE = 4'd2, R_NOT_WATCHED = 4'd3,
                           R_BAD_PRICE = 4'd4, R_BAD_QTY = 4'd5, R_COLLISION = 4'd6,
                           R_DUP_REF = 4'd7, R_UNK_REF = 4'd8;

    if (ORD_AW < LAW) begin : g_chk
        $error("mdp_itch_book: ORD_AW must be >= SYM_W+1+LVL_W so one sweep clears both RAMs");
    end

    logic [2:0]        state;
    logic [ORD_AW-1:0] init_cnt;
    logic              init_busy;
    assign ready    = !init_busy;
    wire   live     = (state == S_IDLE) || (state == S_BT);    // may accept a message
    assign in_ready = live && !init_busy;
    wire   accept   = in_valid && in_ready;

    // ------------------------------------------------------- captured message
    //   byte k of the message is raw[287-8k -: 8]
    /* verilator lint_off UNUSEDSIGNAL */
    logic [287:0] raw;             // bytes 1..10 (locate, tracking, timestamp) are never used
    /* verilator lint_on UNUSEDSIGNAL */
    logic [15:0]  raw_len;
    logic [TS_W-1:0] raw_ts;
    wire  [7:0]   r_type  = raw[287:280];
    wire  [63:0]  r_ref   = raw[199:136];                      // bytes 11..18 (all types)
    wire  [63:0]  r_ref2  = raw[135:72];                       // U: new reference, bytes 19..26
    wire  [ORD_AW-1:0] r_idx1 = r_ref[ORD_AW-1:0];
    wire  [ORD_AW-1:0] r_idx2 = r_ref2[ORD_AW-1:0];
    wire  [TAG_W-1:0]  r_tag1 = r_ref[63:ORD_AW];
    wire  [TAG_W-1:0]  r_tag2 = r_ref2[63:ORD_AW];

    // live (not yet captured) index for the order-table read issued at accept
    wire  [ORD_AW-1:0] in_idx = in_data[136 +: ORD_AW];        // low bits of ref (bytes 11..18)

    // ---------------------------------------------------------- order table
    // Timing: Vivado 2026.1 put this in distributed RAM (LUTRAM), which gave a 144-load read address and a
    // 1024:1 LUT read mux on the critical path (-0.545 ns @ 250 MHz).  Block RAM has a registered read inside
    // the RAM and no mux tree.  Same behaviour: the read is registered (ord_q) and a same-cycle write is
    // read-first (old data), which is what the testbench already checks.
    (* ram_style = "block" *) logic [OW-1:0] ord_mem [0:(1<<ORD_AW)-1];
    logic [OW-1:0] ord_q;
    logic          ord_we;
    logic [ORD_AW-1:0] ord_waddr;
    logic [OW-1:0] ord_wdata;
    logic [ORD_AW-1:0] ord_raddr;

    wire               oq_valid = ord_q[OW-1];
    wire [TAG_W-1:0]   oq_tag   = ord_q[33+LVL_W+SYM_W +: TAG_W];
    wire [SYM_W-1:0]   oq_sym   = ord_q[33+LVL_W +: SYM_W];
    wire               oq_side  = ord_q[32+LVL_W];
    wire [LVL_W-1:0]   oq_lvl   = ord_q[32 +: LVL_W];
    wire [31:0]        oq_rem   = ord_q[31:0];

    // ------------------------------------------------------------- level RAM
    logic [31:0]    lvl_mem [0:(1<<LAW)-1];
    logic [31:0]    lvl_rd;
    logic [LAW-1:0] lvl_raddr, lvl_waddr;
    logic           lvl_we;
    logic [31:0]    lvl_wdata;

    // ---------------------------------------------------------------- D1 regs
    logic              t_add, t_red, t_del, t_rep, t_ign, len_ok;
    logic              side_ok, side_ask, w_hit, n_ge, n_shs_z, e_occ, e_tag_eq, same_slot;
    logic [SYM_W-1:0]  w_idx;
    logic [31:0]       n_shs, n_d;

    wire is_a = (r_type == 8'h41), is_f = (r_type == 8'h46), is_e = (r_type == 8'h45),
         is_c = (r_type == 8'h43), is_x = (r_type == 8'h58), is_d = (r_type == 8'h44),
         is_u = (r_type == 8'h55);
    wire len_c = (is_a && raw_len == 16'd36) || (is_f && raw_len == 16'd40) ||
                 (is_e && raw_len == 16'd31) || (is_c && raw_len == 16'd36) ||
                 (is_x && raw_len == 16'd23) || (is_d && raw_len == 16'd19) ||
                 (is_u && raw_len == 16'd35);

    wire [7:0]  r_side = raw[135:128];
    wire [63:0] r_stk  = raw[95:32];
    wire [31:0] r_shs_a = raw[127:96];     // A/F  bytes 20..23
    wire [31:0] r_shs_r = raw[135:104];    // E/C/X bytes 19..22
    wire [31:0] r_shs_u = raw[71:40];      // U    bytes 27..30
    wire [31:0] r_px_a  = raw[31:0];       // A/F  bytes 32..35
    wire [31:0] r_px_u  = raw[39:8];       // U    bytes 31..34

    wire [32:0] dd_a = {1'b0, r_px_a} - {1'b0, cfg_base_px};
    wire [32:0] dd_u = {1'b0, r_px_u} - {1'b0, cfg_base_px};

    logic [N_SYM-1:0] hit_c;
    always_comb begin
        for (int i = 0; i < N_SYM; i++) hit_c[i] = (r_stk == cfg_watch[64*i +: 64]);
    end
    logic [SYM_W-1:0] idx_c;
    always_comb begin
        idx_c = '0;
        for (int i = N_SYM-1; i >= 0; i--) if (hit_c[i]) idx_c = SYM_W'(i);   // lowest index wins
    end

    wire [31:0] shs_c = (is_a || is_f) ? r_shs_a : (is_u ? r_shs_u : r_shs_r);

    always_ff @(posedge clk) begin
        if (state == S_D1) begin
            t_add     <= is_a || is_f;
            t_red     <= is_e || is_c || is_x;
            t_del     <= is_d;
            t_rep     <= is_u;
            t_ign     <= !(is_a || is_f || is_e || is_c || is_x || is_d || is_u);
            len_ok    <= len_c;
            side_ok   <= (r_side == 8'h42) || (r_side == 8'h53);
            side_ask  <= (r_side == 8'h53);
            w_hit     <= |hit_c;
            w_idx     <= idx_c;
            n_shs     <= shs_c;
            n_shs_z   <= (shs_c == 32'd0);
            n_d       <= is_u ? dd_u[31:0] : dd_a[31:0];
            n_ge      <= is_u ? !dd_u[32]  : !dd_a[32];
            e_occ     <= oq_valid;
            e_tag_eq  <= (oq_tag == r_tag1);
            same_slot <= (r_idx1 == r_idx2);
        end
    end

    // ------------------------------------------------------------------- D2
    // price offset -> level: exact multiples of TICK inside the window only
    logic [N_LVL-1:0] px_hit;
    logic             px_ok_c;
    logic [LVL_W-1:0] lvl_c;
    always_comb begin
        for (int k = 0; k < N_LVL; k++)
            px_hit[k] = n_ge && (n_d[31:DW] == '0) && (n_d[DW-1:0] == DW'(k * TICK));
        px_ok_c = |px_hit;
        lvl_c   = '0;
        for (int k = 0; k < N_LVL; k++) if (px_hit[k]) lvl_c |= LVL_W'(k);
    end

    wire [32:0] rem_diff  = {1'b0, oq_rem} - {1'b0, n_shs};   // bit 32: shares > remaining
    wire        exe_all   = t_del || rem_diff[32];
    wire [31:0] exe_delta = exe_all ? oq_rem : n_shs;
    wire [31:0] new_rem   = exe_all ? 32'd0 : rem_diff[31:0];
    wire        new_nz    = !exe_all && (rem_diff[31:0] != 32'd0);

    // work registers
    logic [SYM_W-1:0] w_sym;
    logic             w_side, w_sub, w_nz, px_ok_r, ph;
    logic [LVL_W-1:0] w_lvl, q_lvl;
    logic [31:0]      w_delta, w_nrem, q_shs;

    // ------------------------------------------------------- final decision (RD)
    logic       dec_drop;
    logic [3:0] dec_reason;
    wire        hit_old  = e_occ && e_tag_eq;
    wire        occ_new  = oq_valid && !same_slot;           // U: ord_q holds the new slot here
    wire        tag_new  = (oq_tag == r_tag2);
    always_comb begin
        dec_drop   = 1'b0;
        dec_reason = 4'd0;
        if (!ph) begin
            if (t_add) begin
                if      (!side_ok)  begin dec_drop = 1'b1; dec_reason = R_BAD_SIDE;    end
                else if (!w_hit)    begin dec_drop = 1'b1; dec_reason = R_NOT_WATCHED; end
                else if (!px_ok_r)  begin dec_drop = 1'b1; dec_reason = R_BAD_PRICE;   end
                else if (n_shs_z)   begin dec_drop = 1'b1; dec_reason = R_BAD_QTY;     end
                else if (e_occ)     begin dec_drop = 1'b1; dec_reason = e_tag_eq ? R_DUP_REF : R_COLLISION; end
            end else if (t_red) begin
                if      (n_shs_z)   begin dec_drop = 1'b1; dec_reason = R_BAD_QTY;     end
                else if (!hit_old)  begin dec_drop = 1'b1; dec_reason = R_UNK_REF;     end
            end else if (t_del) begin
                if (!hit_old)       begin dec_drop = 1'b1; dec_reason = R_UNK_REF;     end
            end else begin                                    // replace
                if      (!px_ok_r)  begin dec_drop = 1'b1; dec_reason = R_BAD_PRICE;   end
                else if (n_shs_z)   begin dec_drop = 1'b1; dec_reason = R_BAD_QTY;     end
                else if (!hit_old)  begin dec_drop = 1'b1; dec_reason = R_UNK_REF;     end
                else if (occ_new)   begin dec_drop = 1'b1; dec_reason = tag_new ? R_DUP_REF : R_COLLISION; end
            end
        end
    end

    // ------------------------------------------------- memory address / data
    always_comb begin
        if (live)                           ord_raddr = in_idx;
        else if (state == S_D2 && t_rep)    ord_raddr = r_idx2;   // U: fetch the new slot for the RD check
        else if (state == S_RD || state == S_WR) ord_raddr = r_idx1;
        else                                ord_raddr = r_idx1;
    end
    assign lvl_raddr = {w_sym, w_side, w_lvl};

    wire [31:0] new_q = w_sub ? (lvl_rd - w_delta) : (lvl_rd + w_delta);

    always_comb begin
        ord_we    = 1'b0;
        ord_waddr = ph ? r_idx2 : r_idx1;
        ord_wdata = {w_nz, (ph ? r_tag2 : r_tag1), w_sym, w_side, w_lvl, w_nrem};
        lvl_we    = 1'b0;
        lvl_waddr = {w_sym, w_side, w_lvl};
        lvl_wdata = new_q;
        if (state == S_WR) begin
            ord_we = 1'b1;
            lvl_we = 1'b1;
        end
    end

    initial begin
        for (int i = 0; i < (1<<ORD_AW); i++) ord_mem[i] = '0;
        for (int i = 0; i < (1<<LAW);    i++) lvl_mem[i] = '0;
    end

    always_ff @(posedge clk) begin
        ord_q  <= ord_mem[ord_raddr];
        lvl_rd <= lvl_mem[lvl_raddr];
        if (init_busy) begin
            ord_mem[init_cnt]              <= '0;
            lvl_mem[init_cnt[LAW-1:0]]     <= 32'd0;
        end else begin
            if (ord_we) ord_mem[ord_waddr] <= ord_wdata;
            if (lvl_we) lvl_mem[lvl_waddr] <= lvl_wdata;
        end
    end

    // ------------------------------------------------------------- occupancy
    logic [N_LVL-1:0] mask [0:(1<<(SYM_W+1))-1];

    logic             bid_vld_c, ask_vld_c;
    logic [LVL_W-1:0] bid_lvl_c, ask_lvl_c;
    always_comb begin
        bid_vld_c = 1'b0; bid_lvl_c = '0;
        ask_vld_c = 1'b0; ask_lvl_c = '0;
        for (int i = 0; i < N_LVL; i++) begin
            if (mask[{w_sym, 1'b0}][i]) begin           // highest set bit wins
                bid_vld_c = 1'b1;
                bid_lvl_c = LVL_W'(i);
            end
        end
        for (int i = N_LVL-1; i >= 0; i--) begin
            if (mask[{w_sym, 1'b1}][i]) begin           // lowest set bit wins
                ask_vld_c = 1'b1;
                ask_lvl_c = LVL_W'(i);
            end
        end
    end

    // ------------------------------------------------ trigger threshold (level domain)
    // thr_lvl = floor((cfg_buy_px - cfg_base_px) / TICK) clamped to the window,
    // found by a LVL_W-step binary search on registers (cfg is static; the search
    // re-runs continuously, so it settles a few cycles after cfg changes).
    logic [31:0]       thr_diff_q;
    logic              thr_ge_q, thr_never;
    logic [LVL_W-1:0]  thr_acc, thr_lvl;
    logic [3:0]        thr_step;
    wire  [LVL_W-1:0]  thr_cand = thr_acc | (LVL_W'(1) << thr_step);
    wire               thr_ok   = (32'(thr_cand) * 32'(TICK) <= thr_diff_q);
    wire  [LVL_W-1:0]  thr_nacc = thr_ok ? thr_cand : thr_acc;
    always_ff @(posedge clk) begin
        thr_diff_q <= cfg_buy_px - cfg_base_px;
        thr_ge_q   <= (cfg_buy_px >= cfg_base_px);
        if (rst) begin
            thr_step <= 4'(LVL_W - 1);
            thr_acc  <= '0;
        end else if (thr_step == 4'd0) begin
            thr_lvl   <= thr_nacc;
            thr_never <= !thr_ge_q;
            thr_acc   <= '0;
            thr_step  <= 4'(LVL_W - 1);
        end else begin
            thr_acc  <= thr_nacc;
            thr_step <= thr_step - 4'd1;
        end
    end
    wire thr_hit = !thr_never && (ask_lvl_c <= thr_lvl);
    wire cond_c  = cfg_en && ask_vld_c && thr_hit;

    logic [N_SYM-1:0] fired;

    // top-of-book tap: registered levels, price formed on the way out
    logic             tb_bid_vld_q, tb_ask_vld_q;
    logic [LVL_W-1:0] tb_bid_lvl_q, tb_ask_lvl_q;
    assign tob_bid_vld = tb_bid_vld_q;
    assign tob_ask_vld = tb_ask_vld_q;
    assign tob_bid_px  = tb_bid_vld_q ? (cfg_base_px + 32'(tb_bid_lvl_q) * 32'(TICK)) : 32'd0;
    assign tob_ask_px  = tb_ask_vld_q ? (cfg_base_px + 32'(tb_ask_lvl_q) * 32'(TICK)) : 32'd0;

    // ------------------------------------------------------------------- FSM
    always_ff @(posedge clk) begin
        ord_valid  <= 1'b0;
        drop_valid <= 1'b0;
        ign_valid  <= 1'b0;
        tob_valid  <= 1'b0;
        if (rst) begin
            state     <= S_IDLE;
            init_busy <= 1'b1;
            init_cnt  <= '0;
            fired     <= '0;
            ph        <= 1'b0;
            for (int i = 0; i < (1<<(SYM_W+1)); i++) mask[i] <= '0;
        end else begin
            if (init_busy) begin
                init_cnt <= init_cnt + 1'b1;
                if (&init_cnt) init_busy <= 1'b0;
            end
            if (accept) begin
                raw     <= in_data;
                raw_len <= in_len;
                raw_ts  <= in_ts;
            end

            case (state)
                S_IDLE: if (accept) state <= S_D1;

                S_D1: state <= S_D2;

                S_D2: begin
                    px_ok_r <= px_ok_c;
                    if (t_add) begin
                        w_sym   <= w_idx;
                        w_side  <= side_ask;
                        w_lvl   <= lvl_c;
                        w_delta <= n_shs;
                        w_sub   <= 1'b0;
                        w_nrem  <= n_shs;
                        w_nz    <= 1'b1;
                    end else if (t_red || t_del) begin
                        w_sym   <= oq_sym;
                        w_side  <= oq_side;
                        w_lvl   <= oq_lvl;
                        w_delta <= exe_delta;
                        w_sub   <= 1'b1;
                        w_nrem  <= new_rem;
                        w_nz    <= new_nz;
                    end else begin                              // replace: remove the old order first
                        w_sym   <= oq_sym;
                        w_side  <= oq_side;
                        w_lvl   <= oq_lvl;
                        w_delta <= oq_rem;
                        w_sub   <= 1'b1;
                        w_nrem  <= 32'd0;
                        w_nz    <= 1'b0;
                        q_lvl   <= lvl_c;
                        q_shs   <= n_shs;
                    end
                    if (t_ign) begin
                        ign_valid <= 1'b1;
                        state     <= S_IDLE;
                    end else if (!len_ok) begin
                        drop_valid  <= 1'b1;
                        drop_reason <= R_BAD_LEN;
                        state       <= S_IDLE;
                    end else begin
                        state <= S_RD;
                    end
                end

                S_RD: begin
                    if (dec_drop) begin
                        drop_valid  <= 1'b1;
                        drop_reason <= dec_reason;
                        state       <= S_IDLE;
                    end else begin
                        state <= S_WR;
                    end
                end

                S_WR: begin
                    mask[{w_sym, w_side}][w_lvl] <= (new_q != 32'd0);
                    if (t_rep && !ph) begin                     // now add the replacement
                        ph      <= 1'b1;
                        w_lvl   <= q_lvl;
                        w_delta <= q_shs;
                        w_sub   <= 1'b0;
                        w_nrem  <= q_shs;
                        w_nz    <= 1'b1;
                        state   <= S_RD;
                    end else begin
                        ph    <= 1'b0;
                        state <= S_BT;
                    end
                end

                S_BT: begin
                    tob_valid    <= 1'b1;
                    tob_sym      <= 16'(w_sym);
                    tb_bid_vld_q <= bid_vld_c;
                    tb_bid_lvl_q <= bid_lvl_c;
                    tb_ask_vld_q <= ask_vld_c;
                    tb_ask_lvl_q <= ask_lvl_c;
                    ord_sym      <= 16'(w_sym);
                    ord_lvl      <= ask_lvl_c;
                    ord_qty      <= cfg_qty;
                    ord_ts       <= raw_ts;
                    if (cond_c && !fired[w_sym]) begin
                        ord_valid    <= 1'b1;
                        fired[w_sym] <= 1'b1;
                    end else if (!cond_c) begin
                        fired[w_sym] <= 1'b0;
                    end
                    state <= accept ? S_D1 : S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end
endmodule
