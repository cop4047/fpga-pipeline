#!/usr/bin/env python3
"""Write ITCH 5.0 / MoldUDP64 test vectors for the C++ baseline from model/itch50.py.

  frames.bin    repeated [u32 little-endian length][frame bytes]
  expected.txt  CFG lines, then the event streams and the final counters:
                  TOB sym bid_vld bid_px ask_vld ask_px | DROP reason | ORD <hex>
                  COUNTS frm_good frm_bad msgs ignored gaps dups heartbeats eos trunc drops orders
"""
import argparse
import os
import random
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "model"))
import itch50 as I  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=os.path.join(HERE, "vectors_itch"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    cfg = I.Cfg(qty=100)
    frames = I.gen_traffic(random.Random(a.seed), cfg, a.frames)
    m = I.Model(cfg)
    for f in frames:
        m.process_frame(f)

    with open(os.path.join(a.out, "frames.bin"), "wb") as fh:
        for f in frames:
            fh.write(struct.pack("<I", len(f)) + f)

    with open(os.path.join(a.out, "expected.txt"), "w") as fh:
        for k in ("base_px", "buy_px", "qty", "en", "md_dport", "dst_mac", "src_mac",
                  "src_ip", "dst_ip", "sport", "o_dport", "tick"):
            fh.write(f"CFG {k} {getattr(cfg, k)}\n")
        fh.write(f"CFG session {cfg.session.hex()}\n")
        for i, w in enumerate(cfg.watch):
            fh.write(f"CFG watch{i} {w.hex()}\n")
        for t in m.tobs:
            fh.write(f"TOB {t.sym} {t.bid_vld} {t.bid_px} {t.ask_vld} {t.ask_px}\n")
        for r in m.drops:
            fh.write(f"DROP {r}\n")
        for o in m.orders_tx:
            fh.write(f"ORD {o.hex()}\n")
        s = m.st
        fh.write("COUNTS {} {} {} {} {} {} {} {} {} {} {}\n".format(
            s["frm_good"], s["frm_bad"], s["msgs"], s["ignored"], s["gaps"], s["dups"],
            s["heartbeats"], s["eos"], s["trunc"], len(m.drops), len(m.orders_tx)))
    print(f"frames={len(frames)} {m.st} tob={len(m.tobs)} drops={len(m.drops)} "
          f"orders={len(m.orders_tx)} -> {a.out}")


if __name__ == "__main__":
    main()
