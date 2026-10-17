"""cocotb testbench for mdp_top.

Strategy: a Python golden model (model/mdp_model.py) consumes exactly the same
frames as the RTL.  Three RTL observation points are scoreboarded against it:
  * top-of-book tap  -> after every accepted book update
  * drop tap         -> every rejected message, with its reason code
  * order AXI-Stream -> the exact bytes of every transmitted order frame
plus the statistics counters.  Directed tests pin down the rules; constrained-
random tests (several seeds, random inter-frame gaps, bad headers, bad
messages) hammer the interaction between them.  Functional coverage is
collected in Python and asserted at the end of the random run.
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
import mdp_model as M  # noqa: E402

PERIOD_PS = 6400                       # 156.25 MHz
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
LATENCY_REGRESSION = 5                 # cycles; a deliberate RTL latency change must update this


def _save(key, value):
    d = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            d = json.load(f)
    d[key] = value
    with open(RESULTS, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


class Env:
    """Clock, reset, frame driver, monitors, scoreboard."""

    def __init__(self, dut, cfg=None):
        self.dut = dut
        self.cfg = cfg or M.Cfg()
        self.model = M.Model(self.cfg)
        self.tobs, self.drops, self.orders = [], [], []
        self.order_first_t = []            # sim time (ps) of first beat of each order
        self.probe_first = {}              # stage name -> first time observed (ps)
        self.probes = {}
        self._mon = None
        self._cur = bytearray()

    # ---------------------------------------------------------------- setup
    def _apply_cfg(self):
        d, c = self.dut, self.cfg
        d.cfg_md_dport.value = c.md_dport
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
        """Start the clock, apply config + reset, wait for the RAM init sweep."""
        if cfg is not None:
            self.cfg = cfg
        self.model = M.Model(self.cfg)
        self.tobs.clear(); self.drops.clear(); self.orders.clear()
        self.order_first_t.clear(); self.probe_first.clear()
        self.probes = probes or {}
        d = self.dut
        if self._mon is not None:
            self._mon.cancel()
            self._mon = None
        if not getattr(self, "_clk_started", False):
            cocotb.start_soon(Clock(d.clk, PERIOD_PS, unit="ps").start())
            self._clk_started = True
        d.rst.value = 1
        d.s_tdata.value = 0
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
                return
        raise AssertionError("o_ready never asserted after reset")

    # -------------------------------------------------------------- monitors
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
                if name not in self.probe_first and int(sig.value):
                    self.probe_first[name] = t

    # ---------------------------------------------------------------- driver
    async def send_frame(self, frame, gap=0, flow_ctl=True, feed_model=True):
        """Drive one frame (one beat per clock) and feed the model.
        Returns the sim time (ps) of the clock edge that captured the last beat."""
        d = self.dut
        if feed_model:
            self.model.process_frame(frame)
        if flow_ctl:
            # emulate a traffic-shaped feed: do not start a frame while the
            # message FIFO is already well filled (a real feed is bounded by
            # the exchange; the overload behaviour is tested separately).
            while int(d.dbg_msgq_count.value) > 12:
                await RisingEdge(d.clk)
        beats = M.frame_to_beats(frame)
        t_last = None
        for i, b in enumerate(beats):
            d.s_tdata.value = b
            d.s_tvalid.value = 1
            d.s_tlast.value = int(i == len(beats) - 1)
            await RisingEdge(d.clk)
            t_last = get_sim_time("ps")
        d.s_tvalid.value = 0
        d.s_tlast.value = 0
        for _ in range(gap):
            await RisingEdge(d.clk)
        return t_last

    async def drain(self, extra=60):
        """Wait until the pipeline is empty (book idle, FIFOs empty, TX idle)."""
        d = self.dut
        quiet = 0
        for _ in range(200000):
            await RisingEdge(d.clk)
            busy = (int(d.dbg_msgq_count.value) or int(d.dbg_ordq_count.value)
                    or int(d.u_book.state.value) != 0 or int(d.m_tvalid.value)
                    or int(d.u_parse.bidx.value) != 0 or int(d.u_asm.have_w0.value)
                    or int(d.u_asm.m_valid.value) or int(d.u_parse.w_valid.value))
            quiet = 0 if busy else quiet + 1
            if quiet >= extra:
                return
        raise AssertionError("pipeline failed to drain")

    # ------------------------------------------------------------ scoreboard
    def check(self):
        m, d = self.model, self.dut
        exp_tob = [(t.sym, t.bid_vld, t.bid_px, t.ask_vld, t.ask_px) for t in m.tobs]
        assert len(self.tobs) == len(exp_tob), \
            f"top-of-book event count: rtl={len(self.tobs)} model={len(exp_tob)}"
        for i, (a, b) in enumerate(zip(self.tobs, exp_tob)):
            assert a == b, f"top-of-book #{i}: rtl={a} model={b}"
        assert len(self.drops) == len(m.drops), \
            f"drop count: rtl={len(self.drops)} model={len(m.drops)}"
        for i, (a, b) in enumerate(zip(self.drops, m.drops)):
            assert a == b, f"drop #{i}: rtl={M.REASON_NAMES.get(a, a)} model={M.REASON_NAMES.get(b, b)}"
        assert len(self.orders) == len(m.orders_tx), \
            f"order count: rtl={len(self.orders)} model={len(m.orders_tx)}"
        for i, (a, b) in enumerate(zip(self.orders, m.orders_tx)):
            assert a == b, f"order #{i} bytes differ:\n rtl  ={a.hex()}\n model={b.hex()}"
        assert int(d.cnt_frm_good.value) == m.frm_good, "cnt_frm_good"
        assert int(d.cnt_frm_bad.value) == m.frm_bad, "cnt_frm_bad"
        assert int(d.cnt_msg.value) == m.msgs_seen, f"cnt_msg rtl={int(d.cnt_msg.value)} model={m.msgs_seen}"
        assert int(d.cnt_drop.value) == len(m.drops), "cnt_drop"
        assert int(d.cnt_ord.value) == len(m.orders_tx), "cnt_ord"
        assert int(d.cnt_ovf.value) == 0, "unexpected FIFO overflow"


def A(side, sym, ref, px, shs):
    return M.Msg(M.T_ADD, side, sym, ref, px, shs)


def D(ref):
    return M.Msg(M.T_DEL, 0, 0, ref, 0, 0)


def E(ref, shs):
    return M.Msg(M.T_EXE, 0, 0, ref, 0, shs)


# ===================================================================== tests
@cocotb.test()
async def test_reset_state(dut):
    env = Env(dut)
    await env.start()
    for _ in range(20):
        await RisingEdge(dut.clk)
    assert int(dut.m_tvalid.value) == 0
    for c in (dut.cnt_frm_good, dut.cnt_frm_bad, dut.cnt_msg, dut.cnt_drop, dut.cnt_ord, dut.cnt_ovf):
        assert int(c.value) == 0
    assert not env.tobs and not env.drops and not env.orders


@cocotb.test()
async def test_directed_trigger(dut):
    """Fire on the first crossing, stay quiet while armed-off, re-arm, fire again."""
    env = Env(dut)
    await env.start()
    cfg, base = env.cfg, env.cfg.base_px            # buy_px = base + 25
    seq = [
        [A(M.SIDE_B, 1, 1, base + 10, 500)],        # bid only: no ask, no order
        [A(M.SIDE_S, 1, 2, base + 40, 300)],        # ask above threshold: no order
        [A(M.SIDE_S, 1, 3, base + 20, 200)],        # ask <= threshold: FIRE (cid 1)
        [A(M.SIDE_S, 1, 4, base + 18, 100)],        # still true, already fired: quiet
        [D(4), D(3)],                               # best ask back to base+40: re-arm
        [A(M.SIDE_S, 1, 5, base + 25, 50)],         # == threshold: FIRE (cid 2)
        [A(M.SIDE_S, 2, 6, base + 5, 10)],          # other symbol fires independently (cid 3)
        [E(5, 20), E(5, 1000)],                     # partial then over-execute (clamped) -> re-arm
        [A(M.SIDE_S, 1, 7, base + 1, 7)],           # FIRE again (cid 4)
    ]
    for msgs in seq:
        await env.send_frame(M.build_mkt_frame(msgs, cfg), gap=3)
    await env.drain()
    env.check()
    assert len(env.orders) == 4, f"expected 4 orders, got {len(env.orders)}"
    # explicit spot checks independent of the model: first order's fields
    o = env.orders[0]
    assert o[42:44] == b"OB" and int.from_bytes(o[46:50], "big") == 1
    assert int.from_bytes(o[50:54], "big") == base + 20      # price = best ask
    assert int.from_bytes(o[54:58], "big") == cfg.qty
    assert [int.from_bytes(x[46:50], "big") for x in env.orders] == [1, 2, 3, 4]


@cocotb.test()
async def test_directed_errors(dut):
    """Every drop reason, every header-reject path, truncated / padded frames."""
    env = Env(dut)
    await env.start()
    cfg, base = env.cfg, env.cfg.base_px
    good = A(M.SIDE_B, 0, 10, base + 5, 100)
    frames = [
        M.build_mkt_frame([good], cfg),
        # one drop reason per frame
        M.build_mkt_frame([M.Msg(0x5A, M.SIDE_B, 0, 11, base, 1)], cfg),       # BAD_TYPE
        M.build_mkt_frame([M.Msg(M.T_ADD, 0x58, 0, 12, base, 1)], cfg),        # BAD_TYPE (side)
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 0, 1 << M.ORD_AW, base, 1)], cfg),  # BAD_REF
        M.build_mkt_frame([M.Msg(M.T_DEL, 0, 0, 0xFFFFFFFF, 0, 0)], cfg),      # BAD_REF (D)
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 4, 13, base, 1)], cfg),    # BAD_SYM
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 0, 14, base - 1, 1)], cfg), # BAD_PRICE (low)
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 0, 15, base + M.N_LVL, 1)], cfg),  # BAD_PRICE (high)
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 0, 16, base, 0)], cfg),    # BAD_QTY
        M.build_mkt_frame([M.Msg(M.T_EXE, 0, 0, 10, 0, 0)], cfg),              # BAD_QTY (E)
        M.build_mkt_frame([A(M.SIDE_B, 0, 10, base + 6, 5)], cfg),             # DUP_REF
        M.build_mkt_frame([D(999)], cfg),                                      # UNK_REF
        M.build_mkt_frame([E(998, 5)], cfg),                                   # UNK_REF
        # precedence: bad type beats bad ref; bad ref beats bad sym; sym beats price
        M.build_mkt_frame([M.Msg(0x00, M.SIDE_B, 9, 1 << 12, 0, 0)], cfg),
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 9, 1 << 12, 0, 0)], cfg),
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 9, 20, 0, 0)], cfg),
        M.build_mkt_frame([M.Msg(M.T_ADD, M.SIDE_B, 0, 20, 0, 0)], cfg),
        # header rejects
        M.build_mkt_frame([good], cfg, ethertype=0x86DD),
        M.build_mkt_frame([good], cfg, ihl=0x46),
        M.build_mkt_frame([good], cfg, proto=6),
        M.build_mkt_frame([good], cfg, dport=cfg.md_dport + 1),
        # count > payload actually present: only the complete messages are used
        M.build_mkt_frame([A(M.SIDE_B, 0, 30, base + 1, 1)], cfg, count=3),
        # count == 0 and count < payload: padding/extra messages are ignored
        M.build_mkt_frame([A(M.SIDE_B, 0, 31, base + 2, 1)], cfg, count=0),
        M.build_mkt_frame([A(M.SIDE_B, 0, 32, base + 3, 1), A(M.SIDE_B, 0, 33, base + 4, 1)], cfg, count=1),
        # too-short frame (ends before the payload) and a runt
        M.build_mkt_frame([good], cfg)[:40],
        M.build_mkt_frame([good], cfg)[:16],
        # still alive afterwards
        M.build_mkt_frame([A(M.SIDE_S, 0, 40, base + 3, 9)], cfg),
    ]
    for f in frames:
        await env.send_frame(f, gap=2)
    await env.drain()
    env.check()
    assert set(env.drops) == set(range(1, 8)), f"not all drop reasons exercised: {sorted(set(env.drops))}"


@cocotb.test()
async def test_threshold_edges(dut):
    """Trigger threshold relative to the book window: below it (never fires), at
    level 0, at the top level, exactly one window above, far above (always)."""
    env = Env(dut)
    for off in (-5, -1, 0, 1, 63, 64, 65, 1000):
        cfg = M.Cfg(base_px=2000)
        cfg.buy_px = cfg.base_px + off
        await env.start(cfg)
        base = cfg.base_px
        ref = 1
        for lvl in (0, 1, 31, 63, 0):                     # asks at assorted levels (last = dup level)
            for sym in (0, 3):
                m = A(M.SIDE_S, sym, ref, base + lvl, 10)
                ref += 1
                await env.send_frame(M.build_mkt_frame([m], cfg), gap=4)
            await env.send_frame(M.build_mkt_frame([D(ref - 1), D(ref - 2)], cfg), gap=4)   # re-arm
        await env.drain()
        env.check()
        n = len(env.orders)
        dut._log.info(f"buy_px = base{off:+d}: {n} orders")
        if off < 0:
            assert n == 0, "threshold below the window must never fire"
        if off >= 63:
            assert n == 10, f"threshold covering the whole window must fire on every add ({n})"


@cocotb.test()
async def test_order_storm(dut):
    """Order bursts that arrive faster than the 8-beat transmitter drains them:
    exercises the order FIFO and the bypass/enqueue hand-off.  Every order must
    survive.  (A *sustained* rate above one order per 8 clocks is beyond the
    transmitter's capacity by construction and is counted in cnt_ovf instead --
    see docs/SPEC.md, limits.)"""
    env = Env(dut)
    await env.start()
    cfg, base = env.cfg, env.cfg.base_px
    rng = random.Random(5)
    ref = 1
    for _burst in range(25):
        for _ in range(7):                               # 7 frames x 2 orders = 14-order burst
            msgs = []
            for _ in range(2):                           # add-low-ask + delete, per symbol
                sym = rng.randrange(4)
                msgs += [A(M.SIDE_S, sym, ref, base + rng.randint(0, 20), 5), D(ref)]
                ref = ref % 1000 + 1
            await env.send_frame(M.build_mkt_frame(msgs, cfg), gap=rng.choice((0, 0, 1, 2)))
        await env.drain(extra=20)
    env.check()
    assert len(env.orders) >= 300, f"storm produced only {len(env.orders)} orders"


@cocotb.test()
async def test_back_to_back_min_frames(dut):
    """Single-message frames with zero inter-frame gap (worst sustained rate for
    this workload) -- the book must keep up with no overflow."""
    env = Env(dut)
    await env.start()
    cfg, base = env.cfg, env.cfg.base_px
    rng = random.Random(7)
    ref = 1
    for _ in range(300):
        s = rng.choice((M.SIDE_B, M.SIDE_S))
        m = A(s, rng.randrange(4), ref, base + rng.randint(0, 40), rng.randint(1, 100))
        ref += 1
        await env.send_frame(M.build_mkt_frame([m], cfg), gap=0, flow_ctl=False)
    await env.drain()
    env.check()


@cocotb.test()
async def test_random(dut):
    """Constrained-random frames, several seeds, random gaps, with coverage."""
    env = Env(dut)
    cov = {"reasons": set(), "orders": 0, "bad_frames": 0, "multi_msg_frames": 0,
           "rearm": 0, "types": set(), "syms_fired": set(), "frames": 0, "msgs": 0}
    seeds = [int(s) for s in os.environ.get("MDP_SEEDS", "1,2,3,4").split(",")]
    nframes = int(os.environ.get("MDP_FRAMES", "1200"))
    for seed in seeds:
        rng = random.Random(seed)
        cfg = M.Cfg(base_px=rng.choice((1000, 5000, 70000)), qty=rng.randint(1, 1000))
        cfg.buy_px = cfg.base_px + rng.randint(15, 35)
        await env.start(cfg)
        frames = M.gen_traffic(rng, cfg, nframes)
        for f in frames:
            await env.send_frame(f, gap=rng.choice((0, 0, 0, 1, 2, 5, 12, 40)))
        await env.drain()
        env.check()
        m = env.model
        cov["reasons"] |= set(m.drops)
        cov["orders"] += len(m.orders_tx)
        cov["bad_frames"] += m.frm_bad
        cov["frames"] += m.frm_good + m.frm_bad
        cov["msgs"] += m.msgs_seen
        cov["syms_fired"] |= {int.from_bytes(o[44:46], "big") for o in m.orders_tx}
        dut._log.info(f"seed {seed}: frames={m.frm_good + m.frm_bad} msgs={m.msgs_seen} "
                      f"tob={len(m.tobs)} drops={len(m.drops)} orders={len(m.orders_tx)}")
    assert cov["reasons"] == set(range(1, 8)), f"drop reasons not all covered: {cov['reasons']}"
    assert cov["orders"] >= 50, "too few orders fired -- trigger not exercised"
    assert cov["syms_fired"] == set(range(4)), "not every symbol fired an order"
    assert cov["bad_frames"] >= 10
    cov["reasons"] = sorted(cov["reasons"])
    cov["syms_fired"] = sorted(cov["syms_fired"])
    cov.pop("types")
    cov.pop("rearm")
    cov.pop("multi_msg_frames")
    _save("random_coverage", cov)


@cocotb.test()
async def test_latency(dut):
    """Tick-to-trade in clock cycles on an idle pipeline.

    Definition: clock edge that captures the LAST beat of the market-data frame
    -> clock edge at which the FIRST beat of the order frame is launched on the
    output register.  Per-stage first-seen times are recorded as well.
    """
    env = Env(dut)
    probes = {
        "parse_w_valid": dut.u_parse.w_valid,
        "asm_m_valid": dut.u_asm.m_valid,
        "book_accept": dut.u_book.accept,
        "tob_tap": dut.tap_tob_valid,
        "order_req": dut.u_book.ord_valid,
        "tx_first_beat": dut.m_tvalid,
    }
    await env.start(probes=probes)
    cfg, base = env.cfg, env.cfg.base_px
    for _ in range(30):
        await RisingEdge(dut.clk)
    m = A(M.SIDE_S, 0, 1, base + 10, 25)              # ask <= buy_px => fires
    t_last = await env.send_frame(M.build_mkt_frame([m], cfg), gap=0)
    await env.drain()
    env.check()
    assert len(env.orders) == 1
    cyc = lambda t: round((t - t_last) / PERIOD_PS) - 1     # launch edge relative to capture edge
    stages = {k: cyc(v) for k, v in env.probe_first.items()}
    lat = cyc(env.order_first_t[0])
    dut._log.info(f"tick-to-trade (simulated) = {lat} cycles = {lat * 6.4:.1f} ns @156.25 MHz")
    dut._log.info(f"stage first-seen (cycles after last input beat captured): {stages}")
    _save("latency", {"tick_to_trade_cycles": lat, "tick_to_trade_ns_at_156p25MHz": round(lat * 6.4, 1),
                      "stage_cycles": stages, "frame_beats": len(M.frame_to_beats(M.build_mkt_frame([m], cfg))),
                      "order_frame_beats": 8})
    if LATENCY_REGRESSION is not None:
        assert lat == LATENCY_REGRESSION, f"latency changed: {lat} != {LATENCY_REGRESSION}"


@cocotb.test()
async def test_mtu_frame(dut):
    """A maximum-size (MTU) frame: 91 messages arrive at one message per 2 clocks,
    faster than the book retires them, so the message FIFO must absorb the burst."""
    env = Env(dut)
    await env.start()
    cfg, base = env.cfg, env.cfg.base_px
    rng = random.Random(11)
    msgs = [A(rng.choice((M.SIDE_B, M.SIDE_S)), rng.randrange(4), r, base + rng.randint(0, 45),
              rng.randint(1, 900)) for r in range(1, 92)]
    f = M.build_mkt_frame(msgs, cfg)
    assert len(f) == 44 + 91 * 16 and len(f) <= 1500
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
    assert len(env.tobs) == 91
    depth = 2 ** (len(dut.dbg_msgq_count) - 1)
    dut._log.info(f"MTU frame (91 msgs): peak message-FIFO occupancy {peak[0]} of {depth}")
    assert peak[0] < depth
    _save("mtu_frame", {"messages": 91, "peak_msgq_occupancy": peak[0], "msgq_depth": depth})


@cocotb.test()
async def test_overload_is_counted(dut):
    """No back-pressure exists, so under sustained overload the design must DROP and
    COUNT, never corrupt or hang: the overflow counter rises and the design recovers."""
    env = Env(dut)
    await env.start()
    cfg, base = env.cfg, env.cfg.base_px
    ref = 1
    for _ in range(12):                                   # MTU-size frames, zero gap
        msgs = []
        for _ in range(91):
            msgs.append(A(M.SIDE_B, 0, ref, base + 3, 1))
            ref = ref % 1000 + 1
        await env.send_frame(M.build_mkt_frame(msgs, cfg), gap=0, flow_ctl=False, feed_model=False)
    await env.drain()
    assert int(dut.cnt_ovf.value) > 0, "overload was not detected"
    # recovery: reset, run a clean directed sequence, compare with the model again
    await env.start()
    await env.send_frame(M.build_mkt_frame([A(M.SIDE_S, 0, 1, base + 5, 10)], env.cfg), gap=3)
    await env.drain()
    env.check()
    assert len(env.orders) == 1
