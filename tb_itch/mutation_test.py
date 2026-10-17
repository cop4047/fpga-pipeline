#!/usr/bin/env python3
"""Mutation testing for the ITCH RTL: inject one bug at a time and check that the cocotb
regression notices.  A SURVIVED mutant is a testbench blind spot (or an equivalent
mutation -- inspect it).

    python3 tb_itch/mutation_test.py                  framer suite  (tb_itch/test_framer.py)
    python3 tb_itch/mutation_test.py --suite book     book suite    (tb_itch/test_pipeline.py, whole pipeline)
    python3 tb_itch/mutation_test.py -k label-substring
"""
import argparse
import os
import re
import shutil
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
    (BOOK, "wire [31:0] new_q = w_sub ? (lvl_rd - w_delta) : (lvl_rd + w_delta);", "wire [31:0] new_q = w_sub ? (lvl_rd + w_delta) : (lvl_rd - w_delta);", "add/subtract swapped (equivalent)"),
    # Equivalent: swapping + and - stores the NEGATED level quantity.  Only "is the level empty?"
    # is observable (no port exposes aggregate size), and negation preserves zero / non-zero.
    (BOOK, "mask[{w_sym, w_side}][w_lvl] <= (new_q != 32'd0);", "mask[{w_sym, w_side}][w_lvl] <= (new_q[15:0] != 16'd0);", "occupancy bit from low half"),
    (BOOK, "lvl_mem[init_cnt[LAW-1:0]]     <= 32'd0;", "lvl_mem[init_cnt[LAW-1:0]]     <= lvl_mem[init_cnt[LAW-1:0]];", "level RAM not cleared after reset"),
    (BOOK, "ord_mem[init_cnt]              <= '0;", "ord_mem[init_cnt]              <= ord_mem[init_cnt];", "order table not cleared after reset"),
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


def run(suite):
    for d in ("sim_build", "sim_build_frame", "sim_build_pipeline"):
        shutil.rmtree(os.path.join(TB, d), ignore_errors=True)
    p = os.path.join(TB, "results.xml")
    if os.path.exists(p):
        os.remove(p)
    env = dict(os.environ, ITCH_SEEDS="1,2", ITCH_FRAMES="800")
    cmd = ["make"] + (["TOP=pipeline"] if suite == "book" else [])
    r = subprocess.run(cmd, cwd=TB, capture_output=True, text=True, env=env)
    m = re.search(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+)", r.stdout + r.stderr)
    if not m:
        return None, (r.stdout + r.stderr)[-500:]
    return tuple(int(x) for x in m.groups()), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="")
    ap.add_argument("--suite", choices=("framer", "book"), default="framer")
    a = ap.parse_args()
    MUTANTS = FRAMER_MUTANTS if a.suite == "framer" else BOOK_MUTANTS
    backup = {f: open(os.path.join(RTL, f)).read() for f in os.listdir(RTL) if f.endswith(".sv")}
    killed, survived = [], []
    try:
        for fname, old, new, label in MUTANTS:
            if a.k not in label:
                continue
            src = backup[fname]
            if old not in src:
                print(f"[SKIP ] {label}: pattern not found in {fname}")
                continue
            with open(os.path.join(RTL, fname), "w") as f:
                f.write(src.replace(old, new, 1))
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
