// mdp_book -- order book + trigger (v1: one message in flight, 6-state FSM).
//
// Data structures
//   ord_mem : direct-indexed order table (ref[ORD_AW-1:0]) holding
//             {valid, sym, side, level, remaining shares}.  Needed so that
//             Delete/Execute messages, which carry only an order ref, can find
//             the price level they affect.
//   lvl_mem : aggregate shares per {symbol, side, level}.
//   mask    : one occupancy bit per level per {symbol, side}, in flops, so the
//             best bid/ask is a priority encode instead of a RAM scan.
// Prices are tracked inside a window of N_LVL ticks starting at cfg_base_px;
// anything outside is dropped (BAD_PRICE).  There is no matching engine: the
// book may be crossed, exactly as a feed-handler book can be.
//
// After reset an init sweep clears both RAMs (2^ORD_AW cycles, `ready` low);
// block RAMs are not reset by a reset pin, so this is what a real design does.
//
// Per-message pipeline (cycles after the message is popped from the FIFO):
//   IDLE  pop FIFO, latch message
//   DEC   validate fields (drop reasons 5,6,1,2,7 -- precedence in SPEC.md)
//   TBL   order-table read result: dup/unknown check, table write, pick level
//   LVL   level-RAM read result: update quantity, update occupancy mask
//   BEST  priority-encode best bid / best ask
//   TRIG  evaluate trigger, emit order request + top-of-book tap
module mdp_book #(
    parameter int SYM_W  = 2,
    parameter int LVL_W  = 6,
    parameter int ORD_AW = 10
) (
    input  logic         clk,
    input  logic         rst,
    // message FIFO (first-word-fall-through)
    input  logic         f_empty,
    input  logic [127:0] f_data,
    output logic         f_rd,
    output logic         ready,        // init sweep finished (RAMs cleared)
    // configuration (static while running)
    input  logic [31:0]  cfg_base_px,
    input  logic [31:0]  cfg_buy_px,
    input  logic [31:0]  cfg_qty,
    input  logic         cfg_en,
    // order request out (one-cycle pulse)
    output logic         ord_valid,
    output logic [15:0]  ord_sym,
    output logic [31:0]  ord_px,
    output logic [31:0]  ord_qty,
    // message dropped (one-cycle pulse)
    output logic         drop_valid,
    output logic [2:0]   drop_reason,
    // top-of-book after every accepted update (one-cycle pulse)
    output logic         tob_valid,
    output logic [15:0]  tob_sym,
    output logic         tob_bid_vld,
    output logic [31:0]  tob_bid_px,
    output logic         tob_ask_vld,
    output logic [31:0]  tob_ask_px
);
    localparam int N_SYM = 1 << SYM_W;
    localparam int N_LVL = 1 << LVL_W;
    localparam int OW    = 1 + SYM_W + 1 + LVL_W + 32;   // order-table entry
    localparam int LAW   = SYM_W + 1 + LVL_W;             // level-RAM address

    localparam logic [2:0] S_IDLE = 3'd0, S_DEC = 3'd1, S_TBL = 3'd2,
                           S_LVL  = 3'd3, S_BEST = 3'd4, S_TRIG = 3'd5;

    localparam logic [2:0] R_BAD_SYM = 3'd1, R_BAD_PRICE = 3'd2, R_DUP_REF = 3'd3,
                           R_UNK_REF = 3'd4, R_BAD_TYPE  = 3'd5, R_BAD_REF = 3'd6,
                           R_BAD_QTY = 3'd7;

    logic [2:0] state;

    // ---- post-reset RAM clear -------------------------------------------------
    if (ORD_AW < LAW) begin : g_chk
        $error("mdp_book: ORD_AW must be >= SYM_W+1+LVL_W so one sweep clears both RAMs");
    end
    logic [ORD_AW-1:0] init_cnt;
    logic              init_busy;
    assign ready = !init_busy;

    // ---------------------------------------------------------------- message
    logic [127:0] m_q;
    wire [7:0]  m_type = m_q[127:120];
    wire [7:0]  m_side = m_q[119:112];
    wire [15:0] m_sym  = m_q[111:96];
    wire [31:0] m_ref  = m_q[95:64];
    wire [31:0] m_px   = m_q[63:32];
    wire [31:0] m_shs  = m_q[31:0];

    wire is_add  = (m_type == 8'h41);
    wire is_del  = (m_type == 8'h44);
    wire is_exe  = (m_type == 8'h45);
    wire side_ok = (m_side == 8'h42) || (m_side == 8'h53);
    wire side_ask = (m_side == 8'h53);
    wire type_ok = is_add ? side_ok : (is_del || is_exe);
    wire ref_ok  = (m_ref[31:ORD_AW] == '0);
    wire sym_ok  = (m_sym[15:SYM_W] == '0);
    wire [31:0] lvl_full = m_px - cfg_base_px;
    wire px_ok   = (m_px >= cfg_base_px) && (lvl_full < 32'(N_LVL));
    wire qty_ok  = (m_shs != 32'd0);

    logic       dec_drop;
    logic [2:0] dec_reason;
    always_comb begin
        dec_drop   = 1'b1;
        dec_reason = R_BAD_TYPE;
        if (!type_ok)                           dec_reason = R_BAD_TYPE;
        else if (!ref_ok)                       dec_reason = R_BAD_REF;
        else if (is_add && !sym_ok)             dec_reason = R_BAD_SYM;
        else if (is_add && !px_ok)              dec_reason = R_BAD_PRICE;
        else if ((is_add || is_exe) && !qty_ok) dec_reason = R_BAD_QTY;
        else begin
            dec_drop   = 1'b0;
            dec_reason = 3'd0;
        end
    end

    // ------------------------------------------------------------ order table
    logic [OW-1:0] ord_mem [0:(1<<ORD_AW)-1];
    logic [OW-1:0] ord_q;
    logic          ord_we;
    logic [OW-1:0] ord_wdata;
    wire  [ORD_AW-1:0] ord_idx = m_ref[ORD_AW-1:0];

    wire               oq_valid = ord_q[OW-1];
    wire [SYM_W-1:0]   oq_sym   = ord_q[33+LVL_W +: SYM_W];
    wire               oq_side  = ord_q[32+LVL_W];
    wire [LVL_W-1:0]   oq_lvl   = ord_q[32 +: LVL_W];
    wire [31:0]        oq_rem   = ord_q[31:0];

    // ------------------------------------------------------------- level RAM
    logic [31:0]     lvl_mem [0:(1<<LAW)-1];
    logic [31:0]     lvl_rd;
    logic [LAW-1:0]  lvl_raddr;
    logic            lvl_we;
    logic [LAW-1:0]  lvl_waddr;
    logic [31:0]     lvl_wdata;

    // working registers carried between states
    logic [SYM_W-1:0] w_sym;
    logic             w_side;
    logic [LVL_W-1:0] w_lvl;
    logic [31:0]      w_delta;
    logic             w_add;

    always_comb begin
        if (is_add) lvl_raddr = {m_sym[SYM_W-1:0], side_ask, lvl_full[LVL_W-1:0]};
        else        lvl_raddr = {oq_sym, oq_side, oq_lvl};
    end

    initial begin
        for (int i = 0; i < (1<<ORD_AW); i++) ord_mem[i] = '0;
        for (int i = 0; i < (1<<LAW);    i++) lvl_mem[i] = '0;
    end

    always_ff @(posedge clk) begin
        ord_q  <= ord_mem[ord_idx];
        lvl_rd <= lvl_mem[lvl_raddr];
        if (init_busy) begin
            ord_mem[init_cnt] <= '0;
            if (init_cnt < ORD_AW'(1 << LAW) || (ORD_AW == LAW))
                lvl_mem[init_cnt[LAW-1:0]] <= 32'd0;
        end else begin
            if (ord_we) ord_mem[ord_idx]   <= ord_wdata;
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

    logic             bid_vld_q, ask_vld_q;
    logic [LVL_W-1:0] bid_lvl_q, ask_lvl_q;
    logic [N_SYM-1:0] fired;

    wire [31:0] bid_px_c = bid_vld_q ? (cfg_base_px + 32'(bid_lvl_q)) : 32'd0;
    wire [31:0] ask_px_c = ask_vld_q ? (cfg_base_px + 32'(ask_lvl_q)) : 32'd0;
    wire        cond_c   = cfg_en && ask_vld_q && (ask_px_c <= cfg_buy_px);

    // ------------------------------------------------------------------- FSM
    assign f_rd = (state == S_IDLE) && !f_empty && !init_busy;

    // execution-delta / table-update combinational helpers (state TBL)
    wire [31:0] exe_delta = (is_del || (m_shs >= oq_rem)) ? oq_rem : m_shs;
    wire [31:0] new_rem   = oq_rem - exe_delta;

    always_comb begin
        ord_we    = 1'b0;
        ord_wdata = '0;
        if (state == S_TBL) begin
            if (is_add) begin
                if (!oq_valid) begin
                    ord_we    = 1'b1;
                    ord_wdata = {1'b1, m_sym[SYM_W-1:0], side_ask, lvl_full[LVL_W-1:0], m_shs};
                end
            end else if (oq_valid) begin
                ord_we    = 1'b1;
                ord_wdata = {(new_rem != 32'd0), oq_sym, oq_side, oq_lvl, new_rem};
            end
        end
    end

    wire [31:0] lvl_new = w_add ? (lvl_rd + w_delta) : (lvl_rd - w_delta);

    always_comb begin
        lvl_we    = (state == S_LVL);
        lvl_waddr = {w_sym, w_side, w_lvl};
        lvl_wdata = lvl_new;
    end

    always_ff @(posedge clk) begin
        ord_valid   <= 1'b0;
        drop_valid  <= 1'b0;
        tob_valid   <= 1'b0;
        if (rst) begin
            state     <= S_IDLE;
            init_busy <= 1'b1;
            init_cnt  <= '0;
            fired     <= '0;
            for (int i = 0; i < (1<<(SYM_W+1)); i++) mask[i] <= '0;
        end else begin
            if (init_busy) begin
                init_cnt <= init_cnt + 1'b1;
                if (&init_cnt) init_busy <= 1'b0;
            end
            case (state)
                S_IDLE: if (!f_empty && !init_busy) begin
                    m_q   <= f_data;
                    state <= S_DEC;
                end

                S_DEC: begin
                    if (dec_drop) begin
                        drop_valid  <= 1'b1;
                        drop_reason <= dec_reason;
                        state       <= S_IDLE;
                    end else begin
                        state <= S_TBL;
                    end
                end

                S_TBL: begin
                    if (is_add) begin
                        if (oq_valid) begin
                            drop_valid  <= 1'b1;
                            drop_reason <= R_DUP_REF;
                            state       <= S_IDLE;
                        end else begin
                            w_sym   <= m_sym[SYM_W-1:0];
                            w_side  <= side_ask;
                            w_lvl   <= lvl_full[LVL_W-1:0];
                            w_delta <= m_shs;
                            w_add   <= 1'b1;
                            state   <= S_LVL;
                        end
                    end else begin
                        if (!oq_valid) begin
                            drop_valid  <= 1'b1;
                            drop_reason <= R_UNK_REF;
                            state       <= S_IDLE;
                        end else begin
                            w_sym   <= oq_sym;
                            w_side  <= oq_side;
                            w_lvl   <= oq_lvl;
                            w_delta <= exe_delta;
                            w_add   <= 1'b0;
                            state   <= S_LVL;
                        end
                    end
                end

                S_LVL: begin
                    mask[{w_sym, w_side}][w_lvl] <= (lvl_new != 32'd0);
                    state <= S_BEST;
                end

                S_BEST: begin
                    bid_vld_q <= bid_vld_c;
                    bid_lvl_q <= bid_lvl_c;
                    ask_vld_q <= ask_vld_c;
                    ask_lvl_q <= ask_lvl_c;
                    state     <= S_TRIG;
                end

                S_TRIG: begin
                    tob_valid   <= 1'b1;
                    tob_sym     <= 16'(w_sym);
                    tob_bid_vld <= bid_vld_q;
                    tob_bid_px  <= bid_px_c;
                    tob_ask_vld <= ask_vld_q;
                    tob_ask_px  <= ask_px_c;
                    if (cond_c && !fired[w_sym]) begin
                        ord_valid    <= 1'b1;
                        ord_sym      <= 16'(w_sym);
                        ord_px       <= ask_px_c;
                        ord_qty      <= cfg_qty;
                        fired[w_sym] <= 1'b1;
                    end else if (!cond_c) begin
                        fired[w_sym] <= 1'b0;
                    end
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end
endmodule
