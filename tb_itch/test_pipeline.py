"""cocotb testbench for mdp_itch_top (framer + ITCH 5.0 book + order transmitter).

The Python model (model/itch50.py) consumes exactly the frames the RTL sees.  Four
RTL observation points are scoreboarded against it:
  * top-of-book tap  -> after every message the book applies
  * drop tap         -> every rejected message with its 4-bit reason
  * order AXI-Stream -> the exact bytes of every transmitted order frame
  * statistics counters (frames, heartbeats, gaps, duplicates, messages, ignored, ...)
Directed tests pin down each rule; constrained-random tests (several seeds, idle
cycles inside and between frames, damaged headers, bad messages) hammer the
interaction between them.
"""
import json
import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb.utils import get_sim_time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import itch50 as I  # noqa: E402

PERIOD_PS = 6400                       # 156.25 MHz
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_pipeline.json")


def _save(key, value):
    d = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            d = json.load(f)
    d[key] = value
    with open(RESULTS, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


class Env:
    def __init__(self, dut):
        self.dut = dut
        self.cfg = I.Cfg()
        self.model = I.Model(self.cfg)
        self.tobs, self.drops, self.orders = [], [], []
        self.order_first_t = []
        self.probe_first, self.probes = {}, {}
        self._mon = None
        self._clk = False
        self._cur = bytearray()

    def _apply_cfg(self):
        d, c = self.dut, self.cfg
        d.cfg_md_dport.value = c.md_dport
        d.cfg_session.value = int.from_bytes(c.session, "big")
        d.cfg_watch.value = sum(int.from_bytes(w, "big") << (64 * i) for i, w in enumerate(c.watch))
        d.cfg_base_px.value = c.base_px
        d.cfg_buy_px.value = c.buy_px
        d.cfg_qty.value = c.qty
        d.cfg_en.value = c.en
        d.cfg_dst_mac.value = c.dst_mac
        d.cfg_src_mac.value = c.src_mac
        d.cfg_src_ip.value = c.src_ip
        d.cfg_dst_ip.value = c.dst_ip
        d.cfg_sport.value = c.sport
        d.cfg_odport.value = c.o_dport
        d.cfg_ip_csum.value = c.ip_csum()

    async def start(self, cfg=None, probes=None):
        if cfg is not None:
            self.cfg = cfg
        self.model = I.Model(self.cfg)
        self.tobs.clear(); self.drops.clear(); self.orders.clear()
        self.order_first_t.clear(); self.probe_first.clear()
        self.probes = probes or {}
        d = self.dut
        if self._mon is not None:
            self._mon.cancel()
            self._mon = None
        if not self._clk:
            cocotb.start_soon(Clock(d.clk, PERIOD_PS, unit="ps").start())
            self._clk = True
        d.rst.value = 1
        d.s_tdata.value = 0
        d.s_tkeep.value = 0
        d.s_tvalid.value = 0
        d.s_tlast.value = 0
        self._apply_cfg()
        for _ in range(4):
            await RisingEdge(d.clk)
        d.rst.value = 0
        await RisingEdge(d.clk)
        self._cur = bytearray()
        self._mon = cocotb.start_soon(self._monitor())
        for _ in range(4000):
            await RisingEdge(d.clk)
            if int(d.o_ready.value):
                for _ in range(20):                  # let the threshold search settle
                    await RisingEdge(d.clk)
                return
        raise AssertionError("o_ready never asserted after reset")

    async def _monitor(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            t = get_sim_time("ps")
            if int(d.tap_tob_valid.value):
                self.tobs.append((int(d.tap_tob_sym.value), int(d.tap_tob_bid_vld.value),
                                  int(d.tap_tob_bid_px.value), int(d.tap_tob_ask_vld.value),
                                  int(d.tap_tob_ask_px.value)))
            if int(d.tap_drop_valid.value):
                self.drops.append(int(d.tap_drop_reason.value))
            if int(d.m_tvalid.value):
                data = int(d.m_tdata.value).to_bytes(8, "little")
                keep = int(d.m_tkeep.value)
                if not self._cur:
                    self.order_first_t.append(t)
                self._cur += bytes(data[i] for i in range(8) if (keep >> i) & 1)
                if int(d.m_tlast.value):
                    self.orders.append(bytes(self._cur))
                    self._cur = bytearray()
            for name, sig in self.probes.items():
                if int(sig.value):
                    self.probe_first[name] = t          # last time seen (the Add is a frame's last message)

    async def send_frame(self, f, rng=None, gap=0, idle_in_frame=0.0, flow_ctl=True, feed_model=True):
        """Drive one frame; returns the time (ps) of the edge that captured its last beat."""
        d = self.dut
        if feed_model:
            self.model.process_frame(f)
        if flow_ctl:
            # emulate a traffic-shaped feed: do not start a frame while the message FIFO
            # is already well filled (overload behaviour is tested separately)
            while int(d.dbg_msgq_count.value) > 16:
                await RisingEdge(d.clk)
        beats = [f[i:i + 8] for i in range(0, len(f), 8)]
        t_last = None
        for i, b in enumerate(beats):
            while idle_in_frame and rng.random() < idle_in_frame:
                d.s_tvalid.value = 0
                await RisingEdge(d.clk)
            n = len(b)
            d.s_tdata.value = int.from_bytes(b + bytes(8 - n), "little")
            d.s_tkeep.value = (1 << n) - 1
            d.s_tvalid.value = 1
            d.s_tlast.value = int(i == len(beats) - 1)
            await RisingEdge(d.clk)
            t_last = get_sim_time("ps")
        d.s_tvalid.value = 0
        d.s_tlast.value = 0
        for _ in range(gap):
            await RisingEdge(d.clk)
        return t_last

    async def drain(self, extra=40):
        d = self.dut
        quiet = 0
        for _ in range(400000):
            await RisingEdge(d.clk)
            busy = (int(d.dbg_msgq_count.value) or int(d.dbg_ordq_count.value)
                    or int(d.u_book.state.value) != 0 or int(d.m_tvalid.value)
                    or int(d.u_frame.bidx.value) != 0 or int(d.u_frame.m_valid.value))
            quiet = 0 if busy else quiet + 1
            if quiet >= extra:
                return
        raise AssertionError("pipeline failed to drain")

    def check(self, allow_ovf=False):
        m, d = self.model, self.dut
        exp_tob = [(t.sym, t.bid_vld, t.bid_px, t.ask_vld, t.ask_px) for t in m.tobs]
        assert len(self.tobs) == len(exp_tob), f"top-of-book count: rtl={len(self.tobs)} model={len(exp_tob)}"
        for i, (a, b) in enumerate(zip(self.tobs, exp_tob)):
            assert a == b, f"top-of-book #{i}: rtl={a} model={b}"
        assert len(self.drops) == len(m.drops), f"drop count: rtl={len(self.drops)} model={len(m.drops)}"
        for i, (a, b) in enumerate(zip(self.drops, m.drops)):
            assert a == b, f"drop #{i}: rtl={I.REASON_NAMES.get(a, a)} model={I.REASON_NAMES.get(b, b)}"
        assert len(self.orders) == len(m.orders_tx), f"order count: rtl={len(self.orders)} model={len(m.orders_tx)}"
        for i, (a, b) in enumerate(zip(self.orders, m.orders_tx)):
            assert a == b, f"order #{i} bytes differ:\n rtl  ={a.hex()}\n model={b.hex()}"
        s = m.st
        want = dict(cnt_frm_good=s["frm_good"], cnt_frm_bad=s["frm_bad"], cnt_hb=s["heartbeats"],
                    cnt_eos=s["eos"], cnt_gap=s["gaps"], cnt_dup=s["dups"], cnt_trunc=s["trunc"],
                    cnt_msg=s["msgs"], cnt_ign=s["ignored"], cnt_drop=len(m.drops),
                    cnt_ord=len(m.orders_tx))
        got = {k: int(getattr(d, k).value) for k in want}
        assert got == want, "counters differ:\n" + "\n".join(
            f" {k}: rtl={got[k]} model={want[k]}" for k in want if got[k] != want[k])
        if not allow_ovf:
            assert int(d.cnt_ovf.value) == 0, "unexpected FIFO overflow"


# --------------------------------------------------------------- shorthands
def add(ref, side, shs, sym, lvl, cfg, mpid=False):
    return I.pack_add(ref, side, shs, cfg.watch[sym], cfg.base_px + lvl * cfg.tick, mpid=mpid)


def px(cfg, lvl):
    return cfg.base_px + lvl * cfg.tick


def frames_of(msgs_per_frame, cfg, seq=1):
    out = []
    for ms in msgs_per_frame:
        out.append(I.build_frame(ms, cfg, seq))
        seq += len(ms)
    return out


# ===================================================================== tests
@cocotb.test()
async def test_reset_state(dut):
    env = Env(dut)
    await env.start()
    for _ in range(20):
        await RisingEdge(dut.clk)
    assert int(dut.m_tvalid.value) == 0
    for n in ("cnt_frm_good", "cnt_frm_bad", "cnt_msg", "cnt_ign", "cnt_drop", "cnt_ord", "cnt_ovf"):
        assert int(getattr(dut, n).value) == 0, n
    assert not env.tobs and not env.drops and not env.orders


@cocotb.test()
async def test_directed_trigger(dut):
    """Fire on the first crossing, stay quiet while armed-off, re-arm, fire again."""
    env = Env(dut)
    await env.start()
    cfg = env.cfg                                       # buy threshold = level 25
    S, B = I.SIDE_S, I.SIDE_B
    steps = [
        [add(1, B, 500, 1, 10, cfg)],                   # bid only: no ask, no order
        [add(2, S, 300, 1, 40, cfg)],                   # ask above threshold: no order
        [add(3, S, 200, 1, 20, cfg)],                   # ask <= threshold: FIRE (cid 1)
        [add(4, S, 100, 1, 18, cfg)],                   # still true, already fired: quiet
        [I.pack_delete(4), I.pack_delete(3)],           # best ask back to level 40: re-arm
        [add(5, S, 50, 1, 25, cfg)],                    # == threshold: FIRE (cid 2)
        [add(6, S, 10, 2, 5, cfg, mpid=True)],          # other symbol fires independently (cid 3)
        [I.pack_exec(5, 20), I.pack_exec(5, 1000)],     # partial then over-execute (clamped) -> re-arm
        [add(7, S, 7, 1, 1, cfg)],                      # FIRE again (cid 4)
    ]
    for ms in steps:
        await env.send_frame(I.build_frame(ms, cfg, 1 + len(env.model.emitted)), gap=3)
    await env.drain()
    env.check()
    assert len(env.orders) == 4, f"expected 4 orders, got {len(env.orders)}"
    o = env.orders[0]
    assert o[42:44] == b"OB" and int.from_bytes(o[46:50], "big") == 1
    assert int.from_bytes(o[50:54], "big") == px(cfg, 20)       # price = best ask
    assert int.from_bytes(o[54:58], "big") == cfg.qty
    assert [int.from_bytes(x[46:50], "big") for x in env.orders] == [1, 2, 3, 4]


@cocotb.test()
async def test_directed_drops(dut):
    """Every drop reason, and the precedence between them; ignored types; mixed frames."""
    env = Env(dut)
    await env.start()
    cfg = env.cfg
    S, B = I.SIDE_S, I.SIDE_B
    base = cfg.base_px
    good = add(10, B, 100, 0, 5, cfg)
    msgs = [
        good,
        I.pack_other(ord("A"), 35),                                              # BAD_LEN (A too short)
        I.pack_other(ord("D"), 25),                                              # BAD_LEN (D too long)
        I.pack_add(11, 0x58, 10, cfg.watch[0], px(cfg, 3)),                      # BAD_SIDE
        I.pack_add(12, B, 10, b"ZZZZ    ", px(cfg, 3)),                          # NOT_WATCHED
        I.pack_add(13, B, 10, cfg.watch[0], base - cfg.tick),                    # BAD_PRICE below
        I.pack_add(14, B, 10, cfg.watch[0], px(cfg, 64)),                        # BAD_PRICE above
        I.pack_add(15, B, 10, cfg.watch[0], px(cfg, 7) + 1),                     # BAD_PRICE off tick
        I.pack_add(16, B, 0, cfg.watch[0], px(cfg, 3)),                          # BAD_QTY
        I.pack_exec(10, 0),                                                      # BAD_QTY (E)
        I.pack_cancel(10, 0),                                                    # BAD_QTY (X)
        add(10, B, 5, 0, 6, cfg),                                                # DUP_REF
        add(10 + I.N_ORD, B, 5, 0, 6, cfg),                                      # COLLISION
        I.pack_delete(999),                                                      # UNK_REF
        I.pack_exec(998, 5),                                                     # UNK_REF
        I.pack_cancel(10 + I.N_ORD, 5),                                          # UNK_REF (slot occupied, tag differs)
        I.pack_replace(997, 996, 5, px(cfg, 3)),                                 # UNK_REF
        I.pack_replace(10, 20, 5, px(cfg, 64)),                                  # BAD_PRICE
        I.pack_replace(10, 20, 0, px(cfg, 3)),                                   # BAD_QTY
        # precedence
        I.pack_add(17, 0x58, 10, b"ZZZZ    ", px(cfg, 99)),                      # BAD_SIDE beats NOT_WATCHED/BAD_PRICE
        I.pack_add(18, B, 0, b"ZZZZ    ", px(cfg, 99)),                          # NOT_WATCHED beats BAD_PRICE/BAD_QTY
        I.pack_add(19, B, 0, cfg.watch[0], px(cfg, 99)),                         # BAD_PRICE beats BAD_QTY
        I.pack_add(10, B, 0, cfg.watch[0], px(cfg, 3)),                          # BAD_QTY beats DUP_REF
        # ignored types of various lengths
        I.pack_other(ord("S")), I.pack_other(ord("R")), I.pack_other(ord("P")), I.pack_other(ord("I")),
        # still alive
        add(40, S, 9, 0, 3, cfg),
    ]
    seq = 1
    for m in msgs:                                   # one message per frame: every drop path in isolation
        await env.send_frame(I.build_frame([m], cfg, seq), gap=2)
        seq += 1
    await env.drain()
    env.check()
    assert set(env.drops) == set(range(1, 9)), f"reasons seen: {sorted(set(env.drops))}"
    # the same messages again, packed into multi-message frames
    env2 = env
    await env2.start()
    seq = 1
    for i in range(0, len(msgs), 5):
        chunk = msgs[i:i + 5]
        await env2.send_frame(I.build_frame(chunk, cfg, seq), gap=1)
        seq += len(chunk)
    await env2.drain()
    env2.check()


@cocotb.test()
async def test_directed_replace(dut):
    """Replace: price move, same level, same slot, different slot, onto occupied slot, partial fills first."""
    env = Env(dut)
    await env.start()
    cfg = env.cfg
    S, B = I.SIDE_S, I.SIDE_B
    msgs = [
        add(1, S, 100, 0, 30, cfg), add(2, B, 50, 0, 10, cfg),
        I.pack_replace(1, 3, 80, px(cfg, 20)),                    # ask moves 30 -> 20, new ref, different slot
        I.pack_replace(3, 4, 80, px(cfg, 20)),                    # same level (remove then add on one address)
        I.pack_replace(4, 4 + I.N_ORD, 60, px(cfg, 21)),          # same slot, new ref (allowed)
        I.pack_replace(4 + I.N_ORD, 4 + I.N_ORD, 55, px(cfg, 22)),  # same ref
        I.pack_replace(2, 3, 1, px(cfg, 11)),                     # onto occupied slot 3 -> DUP/COLLISION? (3 is free now)
        I.pack_exec(3, 1),                                        # fully executes the replacement
        I.pack_replace(4, 9, 5, px(cfg, 5)),                      # old ref no longer present -> UNK_REF
        add(5, S, 10, 2, 50, cfg),
        I.pack_replace(5, 4 + I.N_ORD, 10, px(cfg, 2)),           # new ref == live order -> DUP_REF
        I.pack_replace(5, 6 + I.N_ORD * 2, 10, px(cfg, 2)),       # fine
        add(7, S, 10, 3, 9, cfg),
        I.pack_replace(7, 7 + I.N_ORD, 10, px(cfg, 9)),           # same slot AND same level
        I.pack_cancel(7 + I.N_ORD, 4), I.pack_cancel(7 + I.N_ORD, 6),   # drains it
    ]
    seq = 1
    for m in msgs:
        await env.send_frame(I.build_frame([m], cfg, seq), gap=3)
        seq += 1
    await env.drain()
    env.check()
    assert any(r in env.drops for r in (I.R_DUP_REF, I.R_COLLISION)) and I.R_UNK_REF in env.drops


@cocotb.test()
async def test_directed_edges(dut):
    """Width and boundary cases that random traffic with small values would never reach."""
    env = Env(dut)
    await env.start()
    cfg = env.cfg
    S, B = I.SIDE_S, I.SIDE_B
    base, tick = cfg.base_px, cfg.tick
    A0 = cfg.watch[0]
    big = 10**12
    msgs = [
        # stock names that differ from a watchlist entry in exactly one byte (first / last)
        I.pack_add(1, B, 10, b"AAPL   X", base),
        I.pack_add(2, B, 10, b"BAPL    ", base),
        I.pack_add(3, B, 10, b"AAPL    ", base + tick),                  # exact match: accepted
        # window edges
        I.pack_add(4, S, 10, A0, base + 63 * tick),                      # top level: ok
        I.pack_add(5, S, 10, A0, base + 64 * tick),                      # one past: BAD_PRICE
        I.pack_add(6, S, 10, A0, base + 8192 + 3 * tick),                # aliases to level 3 in the low 13 bits
        I.pack_add(7, S, 10, A0, base + (1 << 20) + 5 * tick),           # aliases in the low 20 bits
        I.pack_add(8, S, 10, A0, base + 63 * tick + 1),                  # off tick at the top
        I.pack_add(9, S, 10, A0, base + 1),                              # off tick at the bottom
        # share counts whose low halves are zero / that overflow a level
        I.pack_add(10, B, 65536, A0, base + 2 * tick),
        I.pack_add(11, B, 0x80000000, A0, base + 4 * tick),
        I.pack_add(12, B, 0x80000000, A0, base + 4 * tick),              # level total wraps to 0
        I.pack_add(13, B, 0xFFFFFFFF, A0, base + 5 * tick),
        I.pack_exec(10, 65536),
        I.pack_exec(13, 0xFFFFFFFF),
        I.pack_exec(11, 0x80000000), I.pack_exec(12, 1),
        I.pack_cancel(12, 0x10000), I.pack_cancel(12, 0x7FFF0000),
        # 64-bit references: collisions that differ only in high bits
        I.pack_add(big, B, 5, A0, base + 6 * tick),
        I.pack_add(big ^ (1 << 60), B, 5, A0, base + 6 * tick),          # COLLISION
        I.pack_add(big ^ (1 << 33), B, 5, A0, base + 6 * tick),          # COLLISION
        I.pack_add(big ^ (1 << 10), B, 5, A0, base + 6 * tick),          # COLLISION (lowest tag bit)
        I.pack_delete(big ^ (1 << 60)),                                  # UNK_REF (slot holds another tag)
        I.pack_delete(big ^ (1 << 40)),                                  # UNK_REF
        I.pack_replace(big, big ^ (1 << 50), 5, base + 7 * tick),        # same slot, new tag: allowed
        I.pack_add(big + 1, S, 5, A0, base + 9 * tick),
        I.pack_replace(big ^ (1 << 50), (big + 1) ^ (1 << 45), 5, base + 7 * tick),   # onto occupied neighbour slot
        I.pack_replace(big ^ (1 << 50), (big + 1) ^ (1 << 45) ^ 1, 5, base + 7 * tick),  # free slot
        I.pack_delete(big ^ (1 << 50)),                                  # old ref is gone -> UNK_REF
        I.pack_replace(big, big + 5, 5, base + 7 * tick),                # old ref does not match tag -> UNK_REF
    ]
    seq = 1
    for m in msgs:
        await env.send_frame(I.build_frame([m], cfg, seq), gap=2)
        seq += 1
    await env.drain()
    env.check()
    assert {I.R_NOT_WATCHED, I.R_BAD_PRICE, I.R_COLLISION, I.R_UNK_REF} <= set(env.drops)


@cocotb.test()
async def test_base_near_top_of_range(dut):
    """A price below the window must be rejected even when (px - base) wraps into the window."""
    env = Env(dut)
    cfg = I.Cfg()
    cfg.base_px = (1 << 32) - 3000
    cfg.buy_px = cfg.base_px
    cfg.en = 0                                    # prices past 2^32 are not meaningful for orders
    await env.start(cfg)
    S, B = I.SIDE_S, I.SIDE_B
    msgs = [I.pack_add(1, B, 10, cfg.watch[0], 100),                                  # wraps to offset 3100
            I.pack_add(2, B, 10, cfg.watch[0], cfg.base_px - cfg.tick),               # just below
            I.pack_add(3, B, 10, cfg.watch[0], cfg.base_px + 5 * cfg.tick),           # fine
            I.pack_add(4, S, 10, cfg.watch[0], cfg.base_px + 29 * cfg.tick)]          # top level that fits
    seq = 1
    for m in msgs:
        await env.send_frame(I.build_frame([m], cfg, seq), gap=2)
        seq += 1
    await env.drain()
    env.check()
    assert env.drops.count(I.R_BAD_PRICE) == 2


@cocotb.test()
async def test_threshold_edges(dut):
    """Trigger threshold relative to the window: below it (never fires), at level 0,
    at the top level, one tick above the window, far above (always); two tick sizes
    are not tested here (TICK is a synthesis parameter)."""
    env = Env(dut)
    LEVELS = (0, 1, 31, 62, 63, 0)
    for off in (-5, -1, 0, 1, 25, 62, 63, 64, 65, 1000):
        cfg = I.Cfg()
        cfg.buy_px = cfg.base_px + off * cfg.tick + (cfg.tick - 1 if off % 2 else 0)   # half-tick offsets too
        await env.start(cfg)
        ref, seq = 1, 1
        for lvl in LEVELS:
            for sym in (0, 3):
                m = add(ref, I.SIDE_S, 10, sym, lvl, cfg)
                ref += 1
                await env.send_frame(I.build_frame([m], cfg, seq), gap=4)
                seq += 1
            await env.send_frame(I.build_frame([I.pack_delete(ref - 1), I.pack_delete(ref - 2)], cfg, seq), gap=4)
            seq += 2
        await env.drain()
        env.check()
        n = len(env.orders)
        dut._log.info(f"buy_px = base{off:+d} ticks: {n} orders")
        if off < 0:
            assert n == 0, "threshold below the window must never fire"
        if off >= 63:
            assert n == 2 * len(LEVELS), f"threshold covering the whole window must fire on every add ({n})"


@cocotb.test()
async def test_random(dut):
    """Constrained-random MoldUDP64/ITCH traffic from the model's generator, several seeds."""
    env = Env(dut)
    seeds = [int(s) for s in os.environ.get("ITCH_SEEDS", "1,2,3").split(",")]
    nframes = int(os.environ.get("ITCH_FRAMES", "1500"))
    idle = float(os.environ.get("ITCH_IDLE", "0.03"))
    for seed in seeds:
        rng = random.Random(seed)
        cfg = I.Cfg()
        await env.start(cfg)
        frames = I.gen_traffic(random.Random(seed), cfg, nframes)
        for f in frames:
            await env.send_frame(f, rng, idle_in_frame=idle, gap=rng.choice((0, 0, 1, 3, 10)))
        await env.drain()
        env.check()
        s = env.model.st
        dut._log.info(f"seed {seed}: {len(frames)} frames, {s['msgs']} messages, {len(env.tobs)} book updates, "
                      f"{len(env.drops)} drops, {len(env.orders)} orders, {s}")
        assert set(env.drops) == set(range(1, 9)) or nframes < 400, f"drop reasons seen: {sorted(set(env.drops))}"


@cocotb.test()
async def test_latency(dut):
    """Tick-to-trade in clock cycles on an idle pipeline (simulated, not a hardware measurement).

    Definition: clock edge that captures the LAST beat of the market-data frame -> clock edge
    at which the FIRST beat of the order frame is launched on the output register.  Measured
    for an Add whose message ends in each byte lane, because the framer's cost depends on it.
    """
    env = Env(dut)
    probes = {
        "frame_m_valid": dut.u_frame.m_valid,
        "book_accept": dut.u_book.accept,
        "tob_tap": dut.tap_tob_valid,
        "order_req": dut.u_book.ord_valid,
        "tx_first_beat": dut.m_tvalid,
    }
    await env.start(probes=probes)
    cfg = env.cfg
    results = {}
    for padlen in range(12, 20):                      # shifts the Add's start lane across all 8 values
        await env.start(cfg, probes=probes)
        for _ in range(10):
            await RisingEdge(dut.clk)
        pre = I.pack_other(ord("S"), padlen)
        m = add(1, I.SIDE_S, 25, 0, 10, cfg)           # ask <= threshold => fires
        f = I.build_frame([pre, m], cfg, 1)
        start_lane = (62 + 2 + padlen) % 8
        t_last = await env.send_frame(f, gap=0)
        await env.drain()
        env.check()
        assert len(env.orders) == 1
        cyc = lambda t: round((t - t_last) / PERIOD_PS) - 1
        stages = {k: cyc(v) for k, v in env.probe_first.items()}
        results[start_lane] = (cyc(env.order_first_t[0]), stages)
    lat = {k: v[0] for k, v in results.items()}
    dut._log.info(f"tick-to-trade (simulated) by start lane of the Add block: {lat}")
    dut._log.info(f"stage last-seen for lane {min(lat, key=lat.get)} (best): {results[min(lat, key=lat.get)][1]}")
    dut._log.info(f"stage last-seen for lane {max(lat, key=lat.get)} (worst): {results[max(lat, key=lat.get)][1]}")
    best, worst = min(lat.values()), max(lat.values())
    _save("latency", {"cycles_by_start_lane": {str(k): v for k, v in sorted(lat.items())},
                      "best_cycles": best, "worst_cycles": worst,
                      "best_ns_at_156p25MHz": round(best * 6.4, 1), "worst_ns_at_156p25MHz": round(worst * 6.4, 1),
                      "stages_best": results[min(lat, key=lat.get)][1],
                      "stages_worst": results[max(lat, key=lat.get)][1]})


@cocotb.test()
async def test_burst_and_overflow(dut):
    """A burst the book cannot retire as fast as it arrives: the message FIFO absorbs it
    (up to 64 deep).  Past that, messages are dropped and COUNTED, never silently lost."""
    env = Env(dut)
    await env.start()
    cfg = env.cfg
    rng = random.Random(5)
    # 35 Adds in one frame (38 bytes each) arrive in ~5 clocks/msg; the book retires one per ~5
    msgs = [add(r, rng.choice((I.SIDE_B, I.SIDE_S)), rng.randint(1, 900), rng.randrange(4),
                rng.randint(0, 40), cfg) for r in range(1, 36)]
    f = I.build_frame(msgs, cfg, 1)
    assert len(f) <= 1500
    peak = [0]

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            peak[0] = max(peak[0], int(dut.dbg_msgq_count.value))
    w = cocotb.start_soon(watch())
    await env.send_frame(f, gap=0)
    await env.drain()
    w.cancel()
    env.check()
    dut._log.info(f"35-message frame: peak message-FIFO depth {peak[0]}")
    assert len(env.tobs) == 35
    # Overload on purpose.  Shortest block = 14 bytes (1.75 beats) but the book needs 3 cycles even
    # for an ignored message, so a full-size frame of them outruns it and the 64-deep FIFO fills.
    await env.start(cfg)
    n_sent = 0
    for k in range(20):
        ms = [I.pack_other(ord("S")) for _ in range(100)]
        f = I.build_frame(ms, cfg, 1 + n_sent)
        assert len(f) <= 1500
        await env.send_frame(f, gap=0, flow_ctl=False)
        n_sent += len(ms)
    await env.drain()
    ovf, ign, msgs = int(dut.cnt_ovf.value), int(dut.cnt_ign.value), int(dut.cnt_msg.value)
    dut._log.info(f"sustained overload: {n_sent} messages sent, framed {msgs}, ignored by the book {ign}, "
                  f"overflow events counted {ovf}")
    assert msgs == n_sent and ovf > 0, "expected overflow under sustained overload"
    assert ign + ovf == msgs, "every message is either processed or counted as overflow"


@cocotb.test()
async def test_duplicate_watch_entries(dut):
    """Two watchlist slots with the same name: the lowest index wins (model: list.index)."""
    env = Env(dut)
    cfg = I.Cfg()
    cfg.watch = [b"AAPL    ", b"MSFT    ", b"AAPL    ", b"NVDA    "]
    await env.start(cfg)
    base, tick = cfg.base_px, cfg.tick
    msgs = [I.pack_add(1, I.SIDE_S, 10, b"AAPL    ", base + 40 * tick),
            I.pack_add(2, I.SIDE_B, 10, b"AAPL    ", base + 3 * tick),
            I.pack_add(3, I.SIDE_S, 10, b"MSFT    ", base + 20 * tick)]
    for i, m in enumerate(msgs):
        await env.send_frame(I.build_frame([m], cfg, 1 + i), gap=3)
    await env.drain()
    env.check()
    assert {t[0] for t in env.tobs} == {0, 1}, "AAPL must map to symbol 0, never 2"
