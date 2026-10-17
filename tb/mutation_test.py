#!/usr/bin/env python3
"""Mutation testing: inject one deliberate bug at a time into the RTL and check
that the cocotb regression notices.  A mutant that SURVIVES means the testbench
has a blind spot (or the mutation is behaviourally equivalent -- inspect it).

    python3 tb/mutation_test.py            # run all mutants (~10 s each)
    python3 tb/mutation_test.py -k bypass  # only mutants whose label matches

The RTL is restored after every mutant (also on Ctrl-C).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RTL = os.path.join(ROOT, "rtl")
TB = os.path.join(ROOT, "tb")

# (rtl file, text to replace, replacement, label)
MUTANTS = [
    ("mdp_book.sv", "(ask_lvl_c <= thr_lvl)", "(ask_lvl_c < thr_lvl)",
     "trigger compare off-by-one"),
    ("mdp_book.sv", "(thr_diff >= 32'(N_LVL))", "(thr_diff > 32'(N_LVL))",
     "threshold exactly one window above base"),
    ("mdp_book.sv", "mask[{w_sym, w_side}][w_lvl] <= (sub_qty != 32'd0);",
     "mask[{w_sym, w_side}][w_lvl] <= 1'b1;", "Del/Exe never clears occupancy bit"),
    ("mdp_book.sv", "if (oq_valid) begin\n                            drop_valid  <= 1'b1;\n                            drop_reason <= R_DUP_REF;",
     "if (1'b0) begin\n                            drop_valid  <= 1'b1;\n                            drop_reason <= R_DUP_REF;",
     "duplicate order ref not detected"),
    ("mdp_book.sv", "m_del || rem_diff[32]", "m_del",
     "execute not clamped to remaining shares"),
    ("mdp_book.sv", "lvl_wdata = add_qty;", "lvl_wdata = lvl_rd;",
     "Add does not update level quantity"),
    ("mdp_book.sv", "else if (!cond_c) begin", "else if (1'b0) begin",
     "trigger never re-arms"),
    ("mdp_book.sv", "else if (!i_type_ok)", "else if (!i_type_ok)",   # placeholder removed below
     "(skipped placeholder)"),
    ("mdp_top.sv", "assign mq_wr         = m_valid && !(mq_empty && book_in_ready);",
     "assign mq_wr         = m_valid;", "message bypass also enqueues"),
    ("mdp_top.sv", "assign oq_wr        = ord_valid && !(oq_empty && tx_req_ready);",
     "assign oq_wr        = ord_valid && !tx_req_ready;",
     "order lost when FIFO non-empty and TX ready"),
    ("mdp_msg_asm.sv", "bswap32(w_data[31:0])", "w_data[31:0]", "price byte order"),
    ("mdp_msg_asm.sv", "remaining <= rem_eff - 16'd1;", "remaining <= rem_eff;",
     "message count not decremented"),
    ("mdp_hdr_parse.sv", "3'd4: ok <= ok && ({s_tdata[39:32], s_tdata[47:40]} == cfg_dport);",
     "3'd4: ok <= ok;", "UDP destination-port check removed"),
    ("mdp_hdr_parse.sv", "3'd2: ok <= ok && (s_tdata[63:56] == 8'h11);", "3'd2: ok <= ok;",
     "IP protocol check removed"),
    ("mdp_order_tx.sv", "m_tkeep  <= (beat == 3'd7) ? 8'h0F : 8'hFF;", "m_tkeep  <= 8'hFF;",
     "last-beat tkeep wrong"),
]
MUTANTS = [m for m in MUTANTS if not m[3].startswith("(skipped")]


def run_regression():
    shutil.rmtree(os.path.join(TB, "sim_build"), ignore_errors=True)
    for f in ("results.xml",):
        p = os.path.join(TB, f)
        if os.path.exists(p):
            os.remove(p)
    r = subprocess.run(["make"], cwd=TB, capture_output=True, text=True)
    m = re.search(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+)", r.stdout + r.stderr)
    if not m:
        return None, r.stdout[-400:] + r.stderr[-400:]
    return tuple(int(x) for x in m.groups()), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only run mutants whose label contains this")
    args = ap.parse_args()
    backup = {f: open(os.path.join(RTL, f)).read() for f in os.listdir(RTL) if f.endswith(".sv")}
    survived, killed = [], []
    try:
        for fname, old, new, label in MUTANTS:
            if args.k not in label:
                continue
            src = backup[fname]
            if old not in src:
                print(f"[SKIP ] {label}: pattern not found in {fname}")
                continue
            with open(os.path.join(RTL, fname), "w") as f:
                f.write(src.replace(old, new, 1))
            res, err = run_regression()
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
    print(f"\n{len(killed)} killed, {len(survived)} survived, of {len(killed) + len(survived)} mutants")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
