"""cocotb testbench for mdp_itch_frame.

The Python model (model/itch50.py) consumes the same frames as the RTL.  For every
frame stream the testbench checks, in order:
  * each message handed over on m_* (length, and the first min(len, 40) bytes)
  * the packet counters: good/bad frames, heartbeats, end-of-session, gaps,
    duplicate messages, truncations, and the number of messages
  * the hardware timestamp that accompanies each message: it must equal the clock value in the
    cycle in which the beat holding the message's last byte was presented (the testbench drives
    `ts_now` with a free-running cycle count, so the expected value is known exactly)
Frames are driven with random idle cycles between beats and between frames, and
back to back, with partial tkeep on the last beat.
"""
import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import itch50 as I  # noqa: E402

PERIOD_PS = 6400
TS_W = 40
TS_MASK = (1 << TS_W) - 1


def now_ps():
    return int(round(cocotb.utils.get_sim_time("ps")))
MAXB = 40


class Env:
    def __init__(self, dut):
        self.dut = dut
        self.cfg = I.Cfg()
        self.msgs = []                       # (len, bytes) as seen on m_*
        self.ev = dict.fromkeys(("good", "bad", "hb", "eos", "gap", "trunc", "dup"), 0)
        self._clk = False
        self._mon = None
        self._ts_task = None
        self.t0 = 0
        self.tss = []                        # m_ts as seen on m_*
        self.beat_ts = []                    # per frame: ts_now value in the cycle each beat was presented

    async def _ts_counter(self):
        """ts_now = number of cycles since t0, a pure function of simulation time."""
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            d.ts_now.value = ((now_ps() - self.t0) // PERIOD_PS) & TS_MASK

    async def start(self, cfg=None):
        d = self.dut
        if cfg is not None:
            self.cfg = cfg
        self.msgs.clear()
        self.tss.clear()
        self.beat_ts.clear()
        for k in self.ev:
            self.ev[k] = 0
        if self._mon is not None:
            self._mon.cancel()
        if not self._clk:
            cocotb.start_soon(Clock(d.clk, PERIOD_PS, unit="ps").start())
            self._clk = True
        d.rst.value = 1
        d.s_tdata.value = 0
        d.s_tkeep.value = 0
        d.s_tvalid.value = 0
        d.s_tlast.value = 0
        d.cfg_dport.value = self.cfg.md_dport
        d.cfg_session.value = int.from_bytes(self.cfg.session, "big")
        for _ in range(4):
            await RisingEdge(d.clk)
        d.rst.value = 0
        await RisingEdge(d.clk)
        if self._ts_task is not None:
            self._ts_task.cancel()
        self.t0 = now_ps()
        d.ts_now.value = 0
        self._ts_task = cocotb.start_soon(self._ts_counter())
        self._mon = cocotb.start_soon(self._monitor())

    async def _monitor(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            if int(d.m_valid.value):
                ln = int(d.m_len.value)
                data = int(d.m_data.value).to_bytes(40, "big")
                self.msgs.append((ln, data[:min(ln, MAXB)]))
                self.tss.append(int(d.m_ts.value))
            for k in self.ev:
                if int(getattr(d, "ev_" + k).value):
                    self.ev[k] += 1

    async def send_frame(self, f, rng, gap_choices=(0, 0, 0, 1, 3), idle_in_frame=0.0):
        d = self.dut
        beats = [f[i:i + 8] for i in range(0, len(f), 8)]
        bts = []
        for i, b in enumerate(beats):
            while idle_in_frame and rng.random() < idle_in_frame:
                d.s_tvalid.value = 0
                await RisingEdge(d.clk)
            n = len(b)
            bts.append(((now_ps() - self.t0) // PERIOD_PS) & TS_MASK)
            d.s_tdata.value = int.from_bytes(b + bytes(8 - n), "little")
            d.s_tkeep.value = (1 << n) - 1
            d.s_tvalid.value = 1
            d.s_tlast.value = int(i == len(beats) - 1)
            await RisingEdge(d.clk)
        self.beat_ts.append(bts)
        d.s_tvalid.value = 0
        d.s_tlast.value = 0
        for _ in range(rng.choice(gap_choices)):
            await RisingEdge(d.clk)

    async def drain(self, n=60):
        for _ in range(n):
            await RisingEdge(self.dut.clk)

    def check(self, model, frame_msg_counts=None):
        m = model
        exp = [(len(x), x[:MAXB]) for x in m.emitted]
        for i, (a, b) in enumerate(zip(self.msgs, exp)):
            if a != b:
                where = ""
                if frame_msg_counts:
                    acc = 0
                    for fi, c in enumerate(frame_msg_counts):
                        acc += c
                        if i < acc:
                            where = f" (frame #{fi})"
                            break
                raise AssertionError(f"message #{i}{where}:\n rtl  len={a[0]} {a[1].hex()}\n"
                                     f" model len={b[0]} {b[1].hex()}")
        assert len(self.msgs) == len(exp), f"message count: rtl={len(self.msgs)} model={len(exp)}"
        st = m.st
        want = dict(good=st["frm_good"], bad=st["frm_bad"], hb=st["heartbeats"], eos=st["eos"],
                    gap=st["gaps"], trunc=st["trunc"], dup=st["dups"])
        assert self.ev == want, f"counters rtl={self.ev} model={want}"
        assert len(self.msgs) == st["msgs"]
        # hardware timestamps: the cycle in which the beat with the message's last byte was presented
        assert len(self.beat_ts) == m.nframes, "testbench sent a different number of frames than the model saw"
        assert len(self.tss) == len(m.emit_pos)
        for i, ((fno, off), got) in enumerate(zip(m.emit_pos, self.tss)):
            exp = self.beat_ts[fno][off // 8]
            assert got == exp, (f"message #{i} (frame {fno}, last byte at offset {off}, beat {off // 8}): "
                                f"timestamp {got}, expected {exp}")


def pad(f):
    return f


# ===================================================================== tests
@cocotb.test()
async def test_single_messages(dut):
    """One message of each known type, at the natural lane (block 0 at beat 7, lane 6)."""
    env = Env(dut)
    await env.start()
    cfg, b = env.cfg, env.cfg.base_px
    rng = random.Random(0)
    model = I.Model(cfg)
    seq = 1
    for m in (I.pack_add(5, I.SIDE_S, 100, b"AAPL    ", b + 1000),
              I.pack_add(6, I.SIDE_B, 7, b"MSFT    ", b + 300, mpid=True),
              I.pack_exec(5, 10), I.pack_exec(5, 3, with_price=True, px=b),
              I.pack_cancel(6, 2), I.pack_delete(5),
              I.pack_replace(6, 9, 50, b + 500), I.pack_other(ord("P"))):
        f = I.build_frame([m], cfg, seq)
        seq += 1
        model.process_frame(f)
        await env.send_frame(f, rng, gap_choices=(8,))
    await env.drain()
    env.check(model)
    assert len(env.msgs) == 8


@cocotb.test()
async def test_multi_message_frames(dut):
    """Several messages per frame so every start-lane / straddle case occurs."""
    env = Env(dut)
    await env.start()
    cfg, b = env.cfg, env.cfg.base_px
    rng = random.Random(1)
    model = I.Model(cfg)
    ref = 100
    seq = 1
    counts = []
    for n in range(200):
        msgs = []
        for _ in range(rng.randint(1, 8)):
            kind = rng.randrange(4)
            ref += 1
            if kind == 0:
                msgs.append(I.pack_add(ref, I.SIDE_S, 5, b"AAPL    ", b + 500))
            elif kind == 1:
                msgs.append(I.pack_delete(ref - 1))
            elif kind == 2:
                msgs.append(I.pack_other(rng.choice(list(I.IGNORED_LEN))))
            else:
                msgs.append(I.pack_replace(ref - 2, ref, 9, b + 800))
        f = I.build_frame(msgs, cfg, seq)
        seq += len(msgs)
        before = len(model.emitted)
        model.process_frame(f)
        counts.append(len(model.emitted) - before)
        await env.send_frame(f, rng, idle_in_frame=0.1)
    await env.drain()
    env.check(model, counts)


@cocotb.test()
async def test_random(dut):
    """Constrained-random traffic from the model's generator, several seeds."""
    env = Env(dut)
    seeds = [int(s) for s in os.environ.get("ITCH_SEEDS", "1,2,3").split(",")]
    nframes = int(os.environ.get("ITCH_FRAMES", "1500"))
    for seed in seeds:
        rng = random.Random(seed)
        cfg = I.Cfg()
        await env.start(cfg)
        frames = I.gen_traffic(random.Random(seed), cfg, nframes)
        model = I.Model(cfg)
        counts = []
        for f in frames:
            before = len(model.emitted)
            model.process_frame(f)
            counts.append(len(model.emitted) - before)
        for f in frames:
            await env.send_frame(f, rng, idle_in_frame=float(os.environ.get('ITCH_IDLE', '0.03')),
                                 gap_choices=((0,) if os.environ.get('ITCH_IDLE') == '0' else (0, 0, 0, 1, 3)))
        await env.drain()
        env.check(model, counts)
        dut._log.info(f"seed {seed}: {len(frames)} frames, {len(env.msgs)} messages, {env.ev}")


@cocotb.test()
async def test_latency(dut):
    """Cycles from the clock edge that captures the beat holding a message's LAST byte to
    the cycle that message is presented on m_*.  Measured for every start-lane of a 38-byte
    block (an Add): a preceding ignored message of length 12..19 shifts the alignment."""
    env = Env(dut)
    await env.start()
    cfg, b = env.cfg, env.cfg.base_px
    rng = random.Random(0)
    d = dut
    results = {}
    seq = 1
    for padlen in range(12, 20):
        pre = I.pack_other(ord("S"), padlen)
        add = I.pack_add(5, I.SIDE_S, 100, b"AAPL    ", b + 1000)
        f = I.build_frame([pre, add], cfg, seq)
        seq += 2
        end_byte = 62 + (2 + padlen) + (2 + len(add))              # exclusive end of the Add block
        last_beat = (end_byte - 1) // 8
        start_lane = (62 + 2 + padlen) % 8                          # lane of the Add's length prefix
        beats = [f[i:i + 8] for i in range(0, len(f), 8)]
        env.msgs.clear()
        t_cap = None
        t_seen = None
        for i, bt in enumerate(beats):
            n = len(bt)
            d.s_tdata.value = int.from_bytes(bt + bytes(8 - n), "little")
            d.s_tkeep.value = (1 << n) - 1
            d.s_tvalid.value = 1
            d.s_tlast.value = int(i == len(beats) - 1)
            await RisingEdge(d.clk)
            if i == last_beat:
                t_cap = cocotb.utils.get_sim_time("ps")
            if len(env.msgs) >= 2 and t_seen is None:
                t_seen = cocotb.utils.get_sim_time("ps")
        d.s_tvalid.value = 0
        d.s_tlast.value = 0
        for _ in range(12):
            await RisingEdge(d.clk)
            if len(env.msgs) >= 2 and t_seen is None:
                t_seen = cocotb.utils.get_sim_time("ps")
        assert len(env.msgs) == 2 and t_seen is not None, f"padlen {padlen}: {len(env.msgs)} messages"
        # the monitor appends one edge after the output register is set; match the convention of
        # tb/test_mdpipe.py:test_latency  (launch edge relative to capture edge)
        lat = round((t_seen - t_cap) / PERIOD_PS) - 1
        results[start_lane] = lat
    dut._log.info(f"cycles after capture of the last byte's beat, by start lane of the 38-byte block: {results}")
    assert max(results.values()) <= 2, results
