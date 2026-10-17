#!/usr/bin/env python3
"""Write test vectors for the C++ CPU baseline from the golden model.

  frames.bin    repeated [u32 little-endian length][frame bytes]
  expected.txt  CFG lines, then the model's event stream in order:
                  TOB sym bid_vld bid_px ask_vld ask_px
                  DROP reason
                  ORD <hex of order frame>
                  COUNTS frm_good frm_bad msgs drops orders

The C++ program must reproduce expected.txt exactly (cpu_baseline --check).
"""
import argparse
import os
import random
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "model"))
import mdp_model as M  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=os.path.join(HERE, "vectors"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    rng = random.Random(a.seed)
    cfg = M.Cfg(base_px=5000, qty=100)
    cfg.buy_px = cfg.base_px + 25
    frames = M.gen_traffic(rng, cfg, a.frames)

    # Replay through a fresh model so event order is exactly what a sequential
    # consumer sees (drops and tobs interleave by message, but the RTL scoreboard
    # compares each stream separately, so we emit them as separate sections).
    m = M.Model(cfg)
    for f in frames:
        m.process_frame(f)

    with open(os.path.join(a.out, "frames.bin"), "wb") as fh:
        for f in frames:
            fh.write(struct.pack("<I", len(f)))
            fh.write(f)

    with open(os.path.join(a.out, "expected.txt"), "w") as fh:
        for k in ("base_px", "buy_px", "qty", "en", "md_dport", "dst_mac", "src_mac",
                  "src_ip", "dst_ip", "sport", "o_dport"):
            fh.write(f"CFG {k} {getattr(cfg, k)}\n")
        for t in m.tobs:
            fh.write(f"TOB {t.sym} {t.bid_vld} {t.bid_px} {t.ask_vld} {t.ask_px}\n")
        for r in m.drops:
            fh.write(f"DROP {r}\n")
        for o in m.orders_tx:
            fh.write(f"ORD {o.hex()}\n")
        fh.write(f"COUNTS {m.frm_good} {m.frm_bad} {m.msgs_seen} {len(m.drops)} {len(m.orders_tx)}\n")

    nmsg = m.msgs_seen
    print(f"frames={len(frames)} good={m.frm_good} bad={m.frm_bad} msgs={nmsg} "
          f"tob={len(m.tobs)} drops={len(m.drops)} orders={len(m.orders_tx)} -> {a.out}")


if __name__ == "__main__":
    main()
