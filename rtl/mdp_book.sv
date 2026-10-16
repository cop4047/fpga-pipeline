// mdp_book -- order book + trigger (v2: one message in flight, 3-4 cycles/msg).
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
// Message flow (valid/ready input, one message in flight):
//   IDLE  accept: validate fields combinationally (drop reasons 5,6,1,2,7 --
//         precedence in SPEC.md); issue the order-table read and, for Add, the
//         level-RAM read, so their data is ready next cycle.
//   TBL   order-table result: dup / unknown check, table write.
//         Add: update level quantity + occupancy mask right here  -> BT
//         Del/Exe: pick the level from the table entry, read it   -> LVL
//   LVL   (Del/Exe only) update level quantity + occupancy mask
//   BT    best bid/ask priority encode + trigger decision, emit order request
//         and top-of-book tap
// Add: 3 cycles from accept to order request, Del/Exe: 4.
//
// The trigger compares in the level domain (ask_lvl <= buy_px - base_px), so no
// 32-bit adder sits between the priority encoder and the fire decision.
module mdp_book #(
    parameter int SYM_W  = 2,
    parameter int LVL_W  = 6,
    parameter int ORD_AW = 10
) (
    input  logic         clk,
    input  logic         rst,
    // message input
    input  logic         in_valid,
    input  logic [127:0] in_data,
    output logic         in_ready,
    output logic         ready,        // init sweep finished (RAMs cleared)
    // configuration (static while running)
    input  logic [31:0]  cfg_base_px,
    input  logic [31:0]  cfg_buy_px,
    input  logic [31:0]  cfg_qty,
    input  logic         cfg_en,
    // order request out (one-cycle pulse); price = cfg_base_px + ord_lvl
    output logic         ord_valid,
    output logic [15:0]  ord_sym,
    output logic [LVL_W-1:0] ord_lvl,
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

    localparam logic [1:0] S_IDLE = 2'd0, S_TBL = 2'd1, S_LVL = 2'd2, S_BT = 2'd3;

    localparam logic [2:0] R_BAD_SYM = 3'd1, R_BAD_PRICE = 3'd2, R_DUP_REF = 3'd3,
                           R_UNK_REF = 3'd4, R_BAD_TYPE  = 3'd5, R_BAD_REF = 3'd6,
                           R_BAD_QTY = 3'd7;

    if (ORD_AW < LAW) begin : g_chk
        $error("mdp_book: ORD_AW must be >= SYM_W+1+LVL_W so one sweep clears both RAMs");
    end

    logic [1:0]        state;
    logic [ORD_AW-1:0] init_cnt;
    logic              init_busy;
    assign ready    = !init_busy;
    assign in_ready = (state == S_IDLE) && !init_busy;
    wire   accept   = in_valid && in_ready;

    // ------------------------------------------------ decode of incoming message
    wire [7:0]  i_type = in_data[127:120];
    wire [7:0]  i_side = in_data[119:112];
    wire [15:0] i_sym  = in_data[111:96];
    wire [31:0] i_ref  = in_data[95:64];
    wire [31:0] i_px   = in_data[63:32];
    wire [31:0] i_shs  = in_data[31:0];

    wire i_add      = (i_type == 8'h41);
    wire i_del      = (i_type == 8'h44);
    wire i_exe      = (i_type == 8'h45);
    wire i_side_ok  = (i_side == 8'h42) || (i_side == 8'h53);
    wire i_side_ask = (i_side == 8'h53);
    wire i_type_ok  = i_add ? i_side_ok : (i_del || i_exe);
    wire i_ref_ok   = (i_ref[31:ORD_AW] == '0);
    wire i_sym_ok   = (i_sym[15:SYM_W] == '0);
    wire [31:0] i_lvl_full = i_px - cfg_base_px;
    wire i_px_ok    = (i_px >= cfg_base_px) && (i_lvl_full < 32'(N_LVL));
    wire i_qty_ok   = (i_shs != 32'd0);

    logic       dec_drop;
    logic [2:0] dec_reason;
    always_comb begin
        dec_drop   = 1'b1;
        dec_reason = R_BAD_TYPE;
        if (!i_type_ok)                             dec_reason = R_BAD_TYPE;
        else if (!i_ref_ok)                         dec_reason = R_BAD_REF;
        else if (i_add && !i_sym_ok)                dec_reason = R_BAD_SYM;
        else if (i_add && !i_px_ok)                 dec_reason = R_BAD_PRICE;
        else if ((i_add || i_exe) && !i_qty_ok)     dec_reason = R_BAD_QTY;
        else begin
            dec_drop   = 1'b0;
            dec_reason = 3'd0;
        end
    end

    // message fields kept for the TBL stage
    logic [ORD_AW-1:0] m_idx;
    logic              m_add, m_del, m_side_ask;
    logic [SYM_W-1:0]  m_sym;
    logic [LVL_W-1:0]  m_lvl;
    logic [31:0]       m_shs;

    // ------------------------------------------------------------ order table
    logic [OW-1:0] ord_mem [0:(1<<ORD_AW)-1];
    logic [OW-1:0] ord_q;
    logic          ord_we;
    logic [OW-1:0] ord_wdata;

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

    // working registers for the Del/Exe level update
    logic [SYM_W-1:0] w_sym;
    logic             w_side;
    logic [LVL_W-1:0] w_lvl;
    logic [31:0]      w_delta;

    // Add: address known at accept time.  Del/Exe: address comes from the table.
    always_comb begin
        if (state == S_IDLE) lvl_raddr = {i_sym[SYM_W-1:0], i_side_ask, i_lvl_full[LVL_W-1:0]};
        else                 lvl_raddr = {oq_sym, oq_side, oq_lvl};
    end

    initial begin
        for (int i = 0; i < (1<<ORD_AW); i++) ord_mem[i] = '0;
        for (int i = 0; i < (1<<LAW);    i++) lvl_mem[i] = '0;
    end

    always_ff @(posedge clk) begin
        ord_q  <= ord_mem[i_ref[ORD_AW-1:0]];      // read issued in IDLE (accept) cycle
        lvl_rd <= lvl_mem[lvl_raddr];
        if (init_busy) begin
            ord_mem[init_cnt] <= '0;
            if (init_cnt < ORD_AW'(1 << LAW) || (ORD_AW == LAW))
                lvl_mem[init_cnt[LAW-1:0]] <= 32'd0;
        end else begin
            if (ord_we) ord_mem[m_idx]     <= ord_wdata;
            if (lvl_we) lvl_mem[lvl_waddr] <= lvl_wdata;
        end
    end

    // -------------------------------------------- TBL / LVL combinational logic
    wire [31:0] exe_delta = (m_del || (m_shs >= oq_rem)) ? oq_rem : m_shs;
    wire [31:0] new_rem   = oq_rem - exe_delta;
    wire [31:0] add_qty   = lvl_rd + m_shs;
    wire [31:0] sub_qty   = lvl_rd - w_delta;

    always_comb begin
        ord_we    = 1'b0;
        ord_wdata = '0;
        lvl_we    = 1'b0;
        lvl_waddr = {w_sym, w_side, w_lvl};
        lvl_wdata = sub_qty;
        if (state == S_TBL) begin
            if (m_add) begin
                if (!oq_valid) begin
                    ord_we    = 1'b1;
                    ord_wdata = {1'b1, m_sym, m_side_ask, m_lvl, m_shs};
                    lvl_we    = 1'b1;
                    lvl_waddr = {m_sym, m_side_ask, m_lvl};
                    lvl_wdata = add_qty;
                end
            end else if (oq_valid) begin
                ord_we    = 1'b1;
                ord_wdata = {(new_rem != 32'd0), oq_sym, oq_side, oq_lvl, new_rem};
            end
        end else if (state == S_LVL) begin
            lvl_we = 1'b1;
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

    // ---------------------------------------------------- trigger (level domain)
    wire [31:0] thr_diff = cfg_buy_px - cfg_base_px;
    logic             thr_never, thr_all;
    logic [LVL_W-1:0] thr_lvl;
    always_ff @(posedge clk) begin                      // cfg is static: 1-cycle settle
        thr_never <= (cfg_buy_px < cfg_base_px);
        thr_all   <= (cfg_buy_px >= cfg_base_px) && (thr_diff >= 32'(N_LVL));
        thr_lvl   <= thr_diff[LVL_W-1:0];
    end
    wire thr_hit = !thr_never && (thr_all || (ask_lvl_c <= thr_lvl));
    wire cond_c  = cfg_en && ask_vld_c && thr_hit;

    logic [N_SYM-1:0] fired;

    // top-of-book tap: registered levels, price formed on the way out
    logic             tb_bid_vld_q, tb_ask_vld_q;
    logic [LVL_W-1:0] tb_bid_lvl_q, tb_ask_lvl_q;
    assign tob_bid_vld = tb_bid_vld_q;
    assign tob_ask_vld = tb_ask_vld_q;
    assign tob_bid_px  = tb_bid_vld_q ? (cfg_base_px + 32'(tb_bid_lvl_q)) : 32'd0;
    assign tob_ask_px  = tb_ask_vld_q ? (cfg_base_px + 32'(tb_ask_lvl_q)) : 32'd0;

    // ------------------------------------------------------------------- FSM
    always_ff @(posedge clk) begin
        ord_valid  <= 1'b0;
        drop_valid <= 1'b0;
        tob_valid  <= 1'b0;
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
                S_IDLE: if (accept) begin
                    if (dec_drop) begin
                        drop_valid  <= 1'b1;
                        drop_reason <= dec_reason;
                    end else begin
                        m_idx      <= i_ref[ORD_AW-1:0];
                        m_add      <= i_add;
                        m_del      <= i_del;
                        m_side_ask <= i_side_ask;
                        m_sym      <= i_sym[SYM_W-1:0];
                        m_lvl      <= i_lvl_full[LVL_W-1:0];
                        m_shs      <= i_shs;
                        state      <= S_TBL;
                    end
                end

                S_TBL: begin
                    if (m_add) begin
                        if (oq_valid) begin
                            drop_valid  <= 1'b1;
                            drop_reason <= R_DUP_REF;
                            state       <= S_IDLE;
                        end else begin
                            mask[{m_sym, m_side_ask}][m_lvl] <= (add_qty != 32'd0);
                            w_sym <= m_sym;
                            state <= S_BT;
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
                            state   <= S_LVL;
                        end
                    end
                end

                S_LVL: begin
                    mask[{w_sym, w_side}][w_lvl] <= (sub_qty != 32'd0);
                    state <= S_BT;
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
                    if (cond_c && !fired[w_sym]) begin
                        ord_valid    <= 1'b1;
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
