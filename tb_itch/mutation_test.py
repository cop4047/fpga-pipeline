#!/usr/bin/env python3
"""Mutation testing for the ITCH RTL: inject one bug at a time and check that the cocotb
regression notices.  A SURVIVED mutant is a testbench blind spot (or an equivalent
mutation -- inspect it).

    python3 tb_itch/mutation_test.py                  framer suite  (tb_itch/test_framer.py)
    python3 tb_itch/mutation_test.py --suite book     book suite    (tb_itch/test_pipeline.py, whole pipeline)
    python3 tb_itch/mutation_test.py --suite rtc      hardware clock           (tb_itch/test_rtc.py)
    python3 tb_itch/mutation_test.py --suite fifo     async FIFO / CDC         (tb_itch/test_async_fifo.py)
    python3 tb_itch/mutation_test.py --suite ts-framer  timestamp capture in the framer
    python3 tb_itch/mutation_test.py --suite ts-pipe    timestamp path + latency statistics, one clock
    python3 tb_itch/mutation_test.py --suite cdc-pipe   clock-crossing wiring of the whole pipeline (ASYNC=1)
    python3 tb_itch/mutation_test.py -k label-substring
A mutant entry's pattern may be a list of (old, new) pairs applied together.
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RTL = os.path.join(ROOT, "rtl")
TB = os.path.join(ROOT, "tb_itch")

# (rtl file, text to replace, replacement, label)
FRAMER_MUTANTS = [
    ("mdp_itch_frame.sv", "nb_eff <= 18'sd6) begin", "nb_eff <= 18'sd5) begin", "block start at lane 6 missed"),
    ("mdp_itch_frame.sv", "(jstart[2:0] + 3'd2)", "(jstart[2:0] + 3'd1)", "body start lane off by one"),
    ("mdp_itch_frame.sv", "wire       sk = (jstart == 18'sd6);", "wire       sk = (jstart == 18'sd5);", "skip-tick case wrong"),
    ("mdp_itch_frame.sv", "len_n < 16'(MIN_BLOCK)", "len_n < 16'(MIN_BLOCK + 1)", "minimum block length 13"),
    ("mdp_itch_frame.sv", "(nb_eff <= $signed({14'd0, nvl}));", "(nb_eff <= 18'sd8);", "end ignores valid lanes"),
    ("mdp_itch_frame.sv", "(left_n != 16'd0 || pend_set || infl_n)", "(left_n != 16'd0 || pend_set)", "unfinished last block not counted"),
    ("mdp_itch_frame.sv", "end else if (lt_q) begin", "end else if (1'b0) begin", "duplicates not skipped"),
    ("mdp_itch_frame.sv", "ev_gap <= 1'b1; exp_q <= seq_q;", "ev_gap <= 1'b1;", "gap does not resync"),
    ("mdp_itch_frame.sv", "ok_q && ({s_tdata[39:32], s_tdata[47:40]} == cfg_dport)", "ok_q", "UDP port check removed"),
    ("mdp_itch_frame.sv", "== cfg_session[31:0]);", "== cfg_session[31:0] || 1'b1);", "session low check removed"),
    ("mdp_itch_frame.sv", "(!s_tlast || s_tkeep[5])", "(!s_tlast || s_tkeep[6])", "62-byte minimum off by one"),
    ("mdp_itch_frame.sv", "if (end_ev)   endu_q <= !endu_q;", "if (1'b0)   endu_q <= !endu_q;", "end unit never alternates"),
    ("mdp_itch_unit.sv", "if (widx == 3'd2) msg[191:128] <= word;", "if (widx == 3'd2) msg[191:128] <= 64'd0;", "word 2 not stored"),
    ("mdp_itch_unit.sv", "wire         fin  = last && (end_seen || end_ev);", "wire         fin  = last;", "completion ignores end event"),
    ("mdp_itch_unit.sv", "if ({1'b0, blk} < skip)", "if ({1'b0, blk} <= skip)", "duplicate boundary"),
    # Equivalent mutant (documented): every block load overwrites the unit and an end event only
    # fires for a block in flight, so a stale active unit can never complete.  `clear` is kept as
    # defence in depth.  Run with -k "not cleared" to see it survive.
    ("mdp_itch_unit.sv", "if (clear) active <= 1'b0;", "if (1'b0) active <= 1'b0;", "unit not cleared at frame end (equivalent)"),
    ("mdp_itch_frame.sv", "wire         tick = s_tvalid | flush_q;", "wire         tick = s_tvalid;", "no flush tick"),
]


BOOK = "mdp_itch_book.sv"
BOOK_MUTANTS = [
    # --- message decode
    (BOOK, "is_a = (r_type == 8'h41)", "is_a = (r_type == 8'h40)", "type A not recognised"),
    (BOOK, "(is_f && raw_len == 16'd40)", "(is_f && raw_len == 16'd41)", "F length 41"),
    (BOOK, "(is_e && raw_len == 16'd31)", "(is_e && raw_len == 16'd30)", "E length 30"),
    (BOOK, "(is_c && raw_len == 16'd36)", "(is_c && raw_len == 16'd35)", "C length 35"),
    (BOOK, "(is_x && raw_len == 16'd23)", "(is_x && raw_len == 16'd24)", "X length 24"),
    (BOOK, "(is_d && raw_len == 16'd19)", "(is_d && raw_len == 16'd18)", "D length 18"),
    (BOOK, "(is_u && raw_len == 16'd35)", "(is_u && raw_len == 16'd36)", "U length 36"),
    (BOOK, "end else if (!len_ok) begin", "end else if (1'b0) begin", "wrong length not dropped"),
    (BOOK, "side_ok   <= (r_side == 8'h42) || (r_side == 8'h53);", "side_ok   <= (r_side == 8'h42);", "sell side rejected"),
    (BOOK, "side_ask  <= (r_side == 8'h53);", "side_ask  <= (r_side == 8'h42);", "side inverted"),
    (BOOK, "hit_c[i] = (r_stk == cfg_watch[64*i +: 64]);", "hit_c[i] = (r_stk[63:8] == cfg_watch[64*i+8 +: 56]);", "stock last byte not compared"),
    (BOOK, "hit_c[i] = (r_stk == cfg_watch[64*i +: 64]);", "hit_c[i] = (r_stk[55:0] == cfg_watch[64*i +: 56]);", "stock first byte not compared"),
    (BOOK, "for (int i = N_SYM-1; i >= 0; i--) if (hit_c[i]) idx_c = SYM_W'(i);", "for (int i = 0; i < N_SYM; i++) if (hit_c[i]) idx_c = SYM_W'(i);", "watchlist: highest index wins"),
    (BOOK, "wire [31:0] shs_c = (is_a || is_f) ? r_shs_a : (is_u ? r_shs_u : r_shs_r);", "wire [31:0] shs_c = (is_a || is_f) ? r_shs_a : (is_u ? r_shs_r : r_shs_r);", "replace shares taken from wrong bytes"),
    (BOOK, "wire [31:0] shs_c = (is_a || is_f) ? r_shs_a : (is_u ? r_shs_u : r_shs_r);", "wire [31:0] shs_c = (is_a || is_f) ? r_shs_a : (is_u ? r_shs_u : r_shs_a);", "execute shares taken from wrong bytes"),
    (BOOK, "n_shs_z   <= (shs_c == 32'd0);", "n_shs_z   <= (shs_c[15:0] == 16'd0);", "zero-share check on low half only"),
    (BOOK, "n_d       <= is_u ? dd_u[31:0] : dd_a[31:0];", "n_d       <= dd_a[31:0];", "replace price taken from add offset"),
    (BOOK, "n_ge      <= is_u ? !dd_u[32]  : !dd_a[32];", "n_ge      <= 1'b1;", "px >= base check removed"),
    # --- price window
    (BOOK, "(n_d[31:DW] == '0)", "1'b1", "window upper bits ignored"),
    (BOOK, "(n_d[DW-1:0] == DW'(k * TICK))", "(n_d[DW-1:1] == DW'(k * TICK) >> 1)", "off-tick by one accepted"),
    (BOOK, "px_ok_r <= px_ok_c;", "px_ok_r <= 1'b1;", "price check bypassed"),
    # --- order table lookup
    (BOOK, "e_tag_eq  <= (oq_tag == r_tag1);", "e_tag_eq  <= (oq_tag[TAG_W-1:1] == r_tag1[TAG_W-1:1]);", "tag low bit ignored"),
    (BOOK, "e_tag_eq  <= (oq_tag == r_tag1);", "e_tag_eq  <= (oq_tag[31:0] == r_tag1[31:0]);", "tag high bits ignored"),
    (BOOK, "e_occ     <= oq_valid;", "e_occ     <= 1'b0;", "occupancy never seen"),
    (BOOK, "dec_reason = e_tag_eq ? R_DUP_REF : R_COLLISION; end\n            end else if (t_red)", "dec_reason = e_tag_eq ? R_COLLISION : R_DUP_REF; end\n            end else if (t_red)", "DUP/COLLISION swapped (add)"),
    (BOOK, "wire        tag_new  = (oq_tag == r_tag2);", "wire        tag_new  = (oq_tag == r_tag1);", "replace: new-slot tag compared with old ref"),
    (BOOK, "wire        occ_new  = oq_valid && !same_slot;", "wire        occ_new  = oq_valid;", "replace into own slot treated as occupied"),
    (BOOK, "same_slot <= (r_idx1 == r_idx2);", "same_slot <= (r_idx1[ORD_AW-1:1] == r_idx2[ORD_AW-1:1]);", "same-slot compare ignores low bit"),
    (BOOK, "ord_raddr = r_idx2;   // U: fetch", "ord_raddr = r_idx1;   // U: fetch", "replace never reads the new slot"),
    # --- drop precedence
    (BOOK, "else if (!w_hit)    begin", "else if (!w_hit && px_ok_r) begin", "NOT_WATCHED loses to BAD_PRICE"),
    (BOOK, "else if (n_shs_z)   begin dec_drop = 1'b1; dec_reason = R_BAD_QTY;     end\n                else if (e_occ)", "else if (n_shs_z && !e_occ) begin dec_drop = 1'b1; dec_reason = R_BAD_QTY;     end\n                else if (e_occ)", "DUP_REF beats BAD_QTY"),
    (BOOK, "if (!hit_old)       begin dec_drop = 1'b1; dec_reason = R_UNK_REF;     end\n            end else begin", "if (1'b0)       begin dec_drop = 1'b1; dec_reason = R_UNK_REF;     end\n            end else begin", "delete of unknown ref accepted"),
    # --- reduce / delete arithmetic
    (BOOK, "wire        exe_all   = t_del || rem_diff[32];", "wire        exe_all   = t_del;", "over-execution not clamped"),
    (BOOK, "wire        new_nz    = !exe_all && (rem_diff[31:0] != 32'd0);", "wire        new_nz    = !exe_all;", "fully executed order stays valid"),
    (BOOK, "w_nrem  <= n_shs;\n                        w_nz    <= 1'b1;\n                    end else if", "w_nrem  <= 32'd0;\n                        w_nz    <= 1'b1;\n                    end else if", "added order stores zero remaining"),
    # --- level RAM / occupancy
    (BOOK, "wire [31:0] new_q = w_sub ? (lvl_rd - w_delta) : (lvl_rd + w_delta);", "wire [31:0] new_q = w_sub ? (lvl_rd + w_delta) : (lvl_rd - w_delta);", "add/subtract swapped"),
    # (Used to be equivalent: negated quantities keep zero / non-zero.  Since the empty test compares lvl_rd with
    # +/-delta directly, stored values must be the true ones, so this is now caught.)
    (BOOK, "wire        lvl_nz       = (lvl_rd != lvl_zero_cmp);", "wire        lvl_nz       = (lvl_rd[15:0] != lvl_zero_cmp[15:0]);", "occupancy bit from low half"),
    (BOOK, "wire [31:0] lvl_zero_cmp = w_sub ? w_delta : (32'd0 - w_delta);", "wire [31:0] lvl_zero_cmp = w_sub ? w_delta : w_delta;", "empty test for add uses +d instead of -d"),
    (BOOK, "wire [31:0] lvl_zero_cmp = w_sub ? w_delta : (32'd0 - w_delta);", "wire [31:0] lvl_zero_cmp = w_sub ? (32'd0 - w_delta) : w_delta;", "empty test sign swapped"),
    (BOOK, "wire              lvl_wr_en   = init_busy | lvl_we;", "wire              lvl_wr_en   = lvl_we;", "level RAM not cleared after reset"),
    (BOOK, "wire              ord_wr_en   = init_busy | ord_we;", "wire              ord_wr_en   = ord_we;", "order table not cleared after reset"),
    # --- replace sequencing
    (BOOK, "w_lvl   <= q_lvl;\n                        w_delta <= q_shs;", "w_lvl   <= w_lvl;\n                        w_delta <= q_shs;", "replacement added at the old level"),
    (BOOK, "w_delta <= q_shs;\n                        w_sub   <= 1'b0;", "w_delta <= oq_rem;\n                        w_sub   <= 1'b0;", "replacement uses wrong share count"),
    (BOOK, "w_delta <= oq_rem;\n                        w_sub   <= 1'b1;\n                        w_nrem  <= 32'd0;", "w_delta <= n_shs;\n                        w_sub   <= 1'b1;\n                        w_nrem  <= 32'd0;", "replace removes new shares from old level"),
    # --- best bid/ask, trigger
    (BOOK, "for (int i = N_LVL-1; i >= 0; i--) begin\n            if (mask[{w_sym, 1'b1}][i]) begin", "for (int i = 0; i < N_LVL; i++) begin\n            if (mask[{w_sym, 1'b1}][i]) begin", "best ask = highest level"),
    (BOOK, "tb_bid_lvl_q <= bid_lvl_c;", "tb_bid_lvl_q <= ask_lvl_c;", "bid tap shows ask level"),
    (BOOK, "wire thr_hit = !thr_never && (ask_lvl_c <= thr_lvl);", "wire thr_hit = !thr_never && (ask_lvl_c < thr_lvl);", "trigger: < instead of <="),
    (BOOK, "wire               thr_ok   = (32'(thr_cand) * 32'(TICK) <= thr_diff_q);", "wire               thr_ok   = (32'(thr_cand) * 32'(TICK) < thr_diff_q);", "threshold search off by one"),
    (BOOK, "thr_never <= !thr_ge_q;", "thr_never <= 1'b0;", "threshold below window not handled"),
    (BOOK, "if (cond_c && !fired[w_sym]) begin", "if (cond_c) begin", "fire on every update (no edge arming)"),
    (BOOK, "ord_lvl      <= ask_lvl_c;", "ord_lvl      <= bid_lvl_c;", "order priced at best bid"),
    (BOOK, "state <= accept ? S_D1 : S_IDLE;", "state <= S_IDLE;", "overlapped accept in BT lost"),
]


RTC = "mdp_ptp_rtc.sv"
RTC_MUTANTS = [
    (RTC, "time_q <= set_en ? set_time : time_q + TIME_W'(inc_q);", "time_q <= time_q + TIME_W'(inc_q);", "step (set) ignored"),
    (RTC, "time_q <= set_en ? set_time : time_q + TIME_W'(inc_q);", "time_q <= set_en ? set_time + TIME_W'(inc_q) : time_q + TIME_W'(inc_q);", "step lands one increment late"),
    (RTC, "inc_q  <= cfg_inc + INC_W'(cfg_trim);", "inc_q  <= cfg_inc + INC_W'($unsigned(cfg_trim));", "trim not sign-extended"),
    (RTC, "inc_q  <= cfg_inc + INC_W'(cfg_trim);", "inc_q  <= cfg_inc;", "trim ignored"),
    (RTC, "time_q <= set_en ? set_time : time_q + TIME_W'(inc_q);", "time_q <= set_en ? set_time : time_q + TIME_W'(cfg_inc);", "increment not registered / trim bypassed"),
    (RTC, "            time_q <= '0;\n        end else begin", "        end else begin", "time not cleared by reset"),
    (RTC, "parameter int INC_W  = 40,", "parameter int INC_W  = 36,", "increment truncated to 36 bits"),
    (RTC, "time_q <= set_en ? set_time : time_q + TIME_W'(inc_q);", "time_q <= set_en ? set_time : {time_q[TIME_W-1:1] + TIME_W'(inc_q >> 1), time_q[0]};", "carry into the LSB lost"),
]

ASYNC_FIFO = "mdp_async_fifo.sv"
FIFO_MUTANTS = [
    (ASYNC_FIFO, [(".d(wgray), .q(wgray_r)", ".d(wbin), .q(wgray_r)"), ("rempty  <= (rgray_n == wgray_r);", "rempty  <= (rbin_n == wgray_r);"),
                  ("assign rcount  = g2b(wgray_r) - rbin;", "assign rcount  = wgray_r - rbin;")],
        "write pointer crosses in BINARY"),
    (ASYNC_FIFO, [(".d(rgray_r), .q(rgray_w)", ".d(rbin), .q(rgray_w)"), ("wfull <= (wgray_n == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]});", "wfull <= (wbin_n == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]});"),
                  ("assign wcount = wbin - g2b(rgray_w);", "assign wcount = wbin - rgray_w;")],
        "read pointer crosses in BINARY"),
    (ASYNC_FIFO, "wfull <= (wgray_n == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]});", "wfull <= (wgray_n == rgray_w);", "full compare without MSB inversion"),
    (ASYNC_FIFO, "wfull <= (wgray_n == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]});", "wfull <= (wgray == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]});", "full from current pointer, not next"),
    (ASYNC_FIFO, "rempty  <= (rgray_n == wgray_r);", "rempty  <= (rgray_r == wgray_r);", "empty from current pointer, not next"),
    (ASYNC_FIFO, "wire         do_wr   = wr_en && !wfull;", "wire         do_wr   = wr_en;", "write while full not blocked"),
    (ASYNC_FIFO, "wire         do_rd   = rd_en && !rempty;", "wire         do_rd   = rd_en;", "read while empty not blocked"),
    (ASYNC_FIFO, "assign rd_data = mem[rbin[AW-1:0]];", "assign rd_data = mem[rbin_n[AW-1:0]];", "read data from the next address"),
    (ASYNC_FIFO, "b2g = (b >> 1) ^ b;", "b2g = (b >> 2) ^ b;", "Gray conversion wrong (several bits change)"),
    (ASYNC_FIFO, "if (wr_en && wfull) wovf <= 1'b1;", "if (1'b0) wovf <= 1'b1;", "overflow not reported"),
    (ASYNC_FIFO, "assign wcount = wbin - g2b(rgray_w);", "assign wcount = wbin - rgray_w;", "write-side count uses Gray as binary"),
    (ASYNC_FIFO, "assign rcount  = g2b(wgray_r) - rbin;", "assign rcount  = wgray_r - rbin;", "read-side count uses Gray as binary"),
    (ASYNC_FIFO, "if (do_wr) mem[wbin[AW-1:0]] <= wr_data;", "if (do_wr) mem[wbin[AW-1:0]] <= {wr_data[W-1:1], 1'b0};", "memory write drops the LSB"),
]

FRAME = "mdp_itch_frame.sv"
UNIT = "mdp_itch_unit.sv"
TS_FRAMER_MUTANTS = [
    (UNIT, "if (end_ev) o_ts <= ts_in;", "if (load) o_ts <= ts_in;", "stamped when the block starts"),
    (UNIT, "if (end_ev) o_ts <= ts_in;", "if (done_msg) o_ts <= ts_in;", "stamped after the message is presented"),
    # Equivalent: the block tracker raises end_ev once per block, so a second end event (the only case where
    # "restamp" differs) cannot occur.
    (UNIT, "if (end_ev) o_ts <= ts_in;", "if (end_ev && !end_seen) o_ts <= ts_in;", "second end event does not restamp (equivalent)"),
    (FRAME, "assign m_ts    = d1_msg ? ts1 : ts0;", "assign m_ts    = ts0;", "always unit 0's stamp"),
    (FRAME, ".end_ev(end_ev && !endu_q), .ts_in(ts_now)", ".end_ev(end_ev && !endu_q), .ts_in(ts_now - TS_W'(1))", "unit 0 stamp one cycle early"),
    (FRAME, ".end_ev(end_ev && endu_q), .ts_in(ts_now)", ".end_ev(end_ev && endu_q), .ts_in('0)", "unit 1 stamp lost"),
]

TOP = "mdp_itch_top.sv"
STATS = "mdp_lat_stats.sv"
TS_PIPE_MUTANTS = [
    (BOOK, "raw_ts  <= in_ts;", "raw_ts  <= '0;", "book drops the stamp"),
    (BOOK, "ord_ts       <= raw_ts;", "ord_ts       <= in_ts;", "order carries the NEXT message's stamp"),
    (TOP, ".lat(ts_now - tx_req_word[OWD-1:80])", ".lat(tx_req_word[OWD-1:80] - ts_now)", "latency sign reversed"),
    (TOP, "wire tx_start = tx_req_valid && tx_req_ready;", "wire tx_start = tx_req_valid;", "latency counted every waiting cycle"),
    (TOP, "wire [TS_W-1:0] ts_now = ptp_time[63 -: TS_W];", "wire [TS_W-1:0] ts_now = ptp_time[55 -: TS_W];", "timestamp from the wrong clock bits"),
    (TOP, "wire [OWD-1:0]  ord_word = {ord_ts, ord_sym, ord_px, ord_qty};", "wire [OWD-1:0]  ord_word = {ts_now, ord_sym, ord_px, ord_qty};", "order stamped at issue time, not ingress time"),
    (TOP, "assign f_word = {f_ts, f_len, f_data[319:32]};", "assign f_word = {f_ts[TS_W-1:1], 1'b0, f_len, f_data[319:32]};", "message FIFO drops the stamp LSB"),
    (STATS, "if (lat < s_min) s_min <= lat;", "if (lat > s_min) s_min <= lat;", "minimum inverted"),
    (STATS, "if (lat > s_max) s_max <= lat;", "if (lat < s_max) s_max <= lat;", "maximum inverted"),
    # Equivalent for reachable latencies: lat is 40 bits in 0.125 ns units, and a latency above 2^32 units (0.5 s)
    # never occurs, so the upper 8 bits of lat are always zero.
    (STATS, "s_sum   <= s_sum + 64'(lat);", "s_sum   <= s_sum + 64'(lat[31:0]);", "sum drops the high bits (equivalent)"),
    (STATS, "s_cnt   <= s_cnt + 32'd1;", "s_cnt   <= s_cnt + 32'd2;", "count wrong"),
    (STATS, "            s_last  <= lat;\n", "", "last value not kept"),
    (TOP, ".set_en(ptp_set_en), .set_time(ptp_set_time), .time_q(ptp_time)", ".set_en(1'b0), .set_time(ptp_set_time), .time_q(ptp_time)", "clock step not wired"),
]

CDC_PIPE_MUTANTS = [
    (TOP, ".rclk(clk_core), .rrst(rst_c), .rd_en(mq_rd)", ".rclk(clk), .rrst(rst_c), .rd_en(mq_rd)", "message FIFO read side on the wrong clock"),
    (TOP, ".wclk(clk_core), .wrst(rst_c), .wr_en(ord_valid)", ".wclk(clk), .wrst(rst_c), .wr_en(ord_valid)", "order FIFO write side on the wrong clock"),
    (TOP, "wire  clk_c = (ASYNC_CORE != 0) ? clk_core : clk;", "wire  clk_c = clk;", "book left on the network clock"),
    (TOP, ".wclk(clk), .wrst(rst_n), .wr_en(f_valid), .wr_data(f_word)", ".wclk(clk_core), .wrst(rst_n), .wr_en(f_valid), .wr_data(f_word)", "message FIFO write side on the core clock"),
    (TOP, ".rclk(clk), .rrst(rst_n), .rd_en(oq_rd)", ".rclk(clk_core), .rrst(rst_n), .rd_en(oq_rd)", "order FIFO read side on the core clock"),
]

# suite -> (mutants, extra make arguments, strict: the pattern must occur exactly once)
SUITES = {
    "framer": (FRAMER_MUTANTS, [], False),
    "book": (BOOK_MUTANTS, ["TOP=pipeline"], False),
    "rtc": (RTC_MUTANTS, ["TOP=rtc"], True),
    "fifo": (FIFO_MUTANTS, ["TOP=fifo"], True),
    "ts-framer": (TS_FRAMER_MUTANTS, ["TOP=frame"], True),
    "ts-pipe": (TS_PIPE_MUTANTS, ["TOP=pipeline", "ASYNC=0"], True),
    "cdc-pipe": (CDC_PIPE_MUTANTS, ["TOP=pipeline", "ASYNC=1"], True),
}


TIMEOUT_S = int(os.environ.get("MUTATION_TIMEOUT", "400"))


def run(suite):
    for d in os.listdir(TB):
        if d.startswith("sim_build"):
            shutil.rmtree(os.path.join(TB, d), ignore_errors=True)
    p = os.path.join(TB, "results.xml")
    if os.path.exists(p):
        os.remove(p)
    env = dict(os.environ, ITCH_SEEDS="1,2", ITCH_FRAMES="800")
    cmd = ["make"] + SUITES[suite][1]
    proc = subprocess.Popen(cmd, cwd=TB, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                            start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)    # only this run's make + simulator
        proc.communicate()
        return (0, 0, 1), "timeout"            # a hang is a detected bug: the regression never passes
    m = re.search(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+)", out)
    if not m:
        return None, out[-500:]
    return tuple(int(x) for x in m.groups()), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="")
    ap.add_argument("--suite", choices=tuple(SUITES), default="framer")
    a = ap.parse_args()
    MUTANTS, _, strict = SUITES[a.suite]
    backup = {f: open(os.path.join(RTL, f)).read() for f in os.listdir(RTL) if f.endswith(".sv")}
    killed, survived = [], []
    try:
        for mut in MUTANTS:
            fname, old, new, label = mut if len(mut) == 4 else (mut[0], mut[1], None, mut[2])
            if a.k not in label:
                continue
            src = backup[fname]
            pairs = old if isinstance(old, list) else [(old, new)]
            bad = [o for o, _ in pairs if (src.count(o) != 1 if strict else o not in src)]
            if bad:
                print(f"[SKIP ] {label}: pattern {'not found' if strict is False else 'not found exactly once'} in {fname}: {bad[0][:60]!r}")
                continue
            mutated = src
            for o, n in pairs:
                mutated = mutated.replace(o, n, 1)
            with open(os.path.join(RTL, fname), "w") as f:
                f.write(mutated)
            res, err = run(a.suite)
            with open(os.path.join(RTL, fname), "w") as f:
                f.write(src)
            if res is None:
                print(f"[ERROR] {label}: simulation did not complete\n{err}")
                continue
            total, passed, failed = res
            if failed:
                killed.append(label)
                print(f"[KILLED]   {label}  ({failed}/{total} tests failed)")
            else:
                survived.append(label)
                print(f"[SURVIVED] {label}")
    finally:
        for f, s in backup.items():
            with open(os.path.join(RTL, f), "w") as fh:
                fh.write(s)
    real = [m for m in survived if "(equivalent)" not in m]
    equiv = [m for m in survived if "(equivalent)" in m]
    print(f"\n{len(killed)} killed, {len(real)} survived, {len(equiv)} equivalent (cannot be killed), "
          f"of {len(killed) + len(survived)} mutants")
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())
