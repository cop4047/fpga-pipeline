"""cocotb tests for mdp_async_fifo: two unrelated clocks, a Python queue as the reference.

What is checked, every cycle of either clock:
  * data: whenever the FIFO says !rempty, the word on rd_data is the oldest unread word
    (so first-word-fall-through is right, nothing is skipped, repeated or corrupted)
  * flags: !rempty only when data really is queued; a write is accepted exactly when
    `wfull` was low at the edge, and the queue never holds more than the depth
    (a lying `wfull` would overwrite unread data)
  * overflow reporting: a write dropped because of `wfull` produces exactly one `wovf` pulse
  * Gray property: each pointer that crosses a clock changes at most ONE bit per cycle
    (the property that makes the 2-flop synchroniser safe)
  * occupancy outputs are one-sided bounds: wcount never under-reports, rcount never over-reports
The DUT is built with -DMDP_CDC_SIM: bits that change inside the sampling aperture of the first
synchroniser flop resolve at random, so a binary pointer or a wrong flag logic fails here while
a correct Gray FIFO does not.
"""
import json
import os
import random
from collections import deque

import cocotb
from cocotb.triggers import RisingEdge, Timer
from cocotb.utils import get_sim_time

W = 16
AW = 3
DEPTH = 1 << AW
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_async_fifo.json")


def now_ps():
    return int(round(get_sim_time("ps")))


def _save(key, value):
    d = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            d = json.load(f)
    d[key] = value
    with open(RESULTS, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


async def clock(sig, period, phase):
    hi = period // 2
    sig.value = 0
    if phase:
        await Timer(phase, unit="ps")
    while True:
        sig.value = 1
        await Timer(hi, unit="ps")
        sig.value = 0
        await Timer(period - hi, unit="ps")


def popcount(x):
    return bin(x).count("1")


def gray(b):
    return b ^ (b >> 1)


class Bench:
    def __init__(self, dut, wper, rper, wph, rph, seed=1):
        self.dut = dut
        self.wper, self.rper, self.wph, self.rph = wper, rper, wph, rph
        self.rng = random.Random(seed)
        self.q = deque()
        self.sent = 0
        self.got = 0
        self.dropped = 0
        self.ovf_seen = 0
        self.full_seen = 0
        self.head_blocked = 0          # cycles with data queued but rempty still high (crossing latency)
        self.p_w = 0.5
        self.p_r = 0.5
        self.tasks = []

    async def start(self, reset_cycles=6):
        d = self.dut
        d.wr_en.value = 0
        d.wr_data.value = 0
        d.rd_en.value = 0
        d.wrst.value = 1
        d.rrst.value = 1
        self.tasks = [cocotb.start_soon(clock(d.wclk, self.wper, self.wph)),
                      cocotb.start_soon(clock(d.rclk, self.rper, self.rph))]
        await Timer(max(self.wper, self.rper) * reset_cycles, unit="ps")
        # release at unrelated moments, as two independent reset synchronisers would
        await Timer(self.rng.randrange(0, self.wper), unit="ps")
        d.wrst.value = 0
        await Timer(self.rng.randrange(0, 2 * self.rper), unit="ps")
        d.rrst.value = 0
        self.q.clear()
        self.sent = self.got = self.dropped = self.ovf_seen = 0
        self.tasks += [cocotb.start_soon(self._write_side()), cocotb.start_soon(self._read_side())]
        for _ in range(4):
            await RisingEdge(d.wclk)

    def stop(self):
        for t in self.tasks:
            t.cancel()
        self.tasks = []

    async def _write_side(self):
        d = self.dut
        prev_gray = None
        pend_drop = 0
        drive_en, drive_data = 0, 0
        wbin_mirror = 0
        while True:
            await RisingEdge(d.wclk)
            # ---- what the DUT sampled at this edge (pre-edge values)
            full = int(d.wfull.value)
            wc = int(d.wcount.value)
            g = int(d.wgray.value)
            assert int(d.wovf.value) == pend_drop, \
                f"wovf={int(d.wovf.value)} but {pend_drop} write(s) were dropped at the previous edge"
            self.ovf_seen += int(d.wovf.value)
            assert wc >= len(self.q), f"wcount {wc} under-reports occupancy {len(self.q)}"
            if prev_gray is not None:
                assert popcount(g ^ prev_gray) <= 1, f"write pointer Gray code moved {popcount(g ^ prev_gray)} bits"
            prev_gray = g
            pend_drop = 0
            self.full_seen += full
            if drive_en:
                if full:
                    pend_drop = 1
                    self.dropped += 1
                else:
                    self.q.append(drive_data)
                    self.sent += 1
                    assert len(self.q) <= DEPTH, f"FIFO holds {len(self.q)} words, depth is {DEPTH}: wfull lied"
            # ---- drive the next cycle
            drive_en = int(self.rng.random() < self.p_w)
            drive_data = self.rng.getrandbits(W)
            d.wr_en.value = drive_en
            d.wr_data.value = drive_data

    async def _read_side(self):
        d = self.dut
        prev_gray = None
        drive_en = 0
        while True:
            await RisingEdge(d.rclk)
            empty = int(d.rempty.value)
            rc = int(d.rcount.value)
            g = int(d.rgray_r.value)
            if prev_gray is not None:
                assert popcount(g ^ prev_gray) <= 1, f"read pointer Gray code moved {popcount(g ^ prev_gray)} bits"
            prev_gray = g
            assert rc <= len(self.q), f"rcount {rc} over-reports occupancy {len(self.q)}"
            if not empty:
                assert self.q, "rempty low but nothing was ever written (or everything was already read)"
                data = int(d.rd_data.value)
                assert data == self.q[0], f"head word {data:#x}, expected {self.q[0]:#x}"
                if drive_en:
                    self.q.popleft()
                    self.got += 1
            elif self.q:
                self.head_blocked += 1
            drive_en = int(self.rng.random() < self.p_r)
            d.rd_en.value = drive_en

    async def run_words(self, n, phases=None):
        """Run until n words have been read; vary the write/read duty cycles in phases."""
        phases = phases or [(0.5, 0.5)]
        i = 0
        while self.got < n:
            self.p_w, self.p_r = phases[(i // 200) % len(phases)]
            i += 1
            await Timer(self.rper * 5, unit="ps")
            assert i < 20000, "stuck: no progress (deadlock)"


CLOCKS = [  # (write period, read period, write phase, read phase)  in ps
    (6400, 4000, 0, 1370),       # network 156.25 MHz -> core 250 MHz
    (4000, 6400, 500, 0),        # core -> network
    (6400, 6400, 0, 3200),       # same frequency, half a cycle apart
    (6400, 6401, 0, 0),          # 156.25 MHz vs 156.2 MHz: phase drifts through every alignment
    (5000, 5003, 1000, 2500),
    (3000, 7000, 0, 0),
    (7000, 3000, 1234, 99),
]
PHASES = [(0.9, 0.2), (0.2, 0.9), (0.5, 0.5), (1.0, 1.0), (0.05, 0.05), (0.7, 0.7)]


@cocotb.test()
async def test_basic(dut):
    """Fill it, drain it, in order; full and empty reached; overflow counted."""
    b = Bench(dut, 6400, 4000, 0, 1370)
    await b.start()
    assert int(dut.rempty.value) == 1 and int(dut.wfull.value) == 0
    b.p_w, b.p_r = 1.0, 0.0
    await Timer(6400 * 40, unit="ps")
    assert b.dropped > 0 and b.ovf_seen > 0, "writing into a stalled reader must overflow and report it"
    assert int(dut.wfull.value) == 1 and len(b.q) == DEPTH
    b.p_w, b.p_r = 0.0, 1.0
    await Timer(6400 * 40, unit="ps")
    assert len(b.q) == 0 and int(dut.rempty.value) == 1
    assert b.got == b.sent == DEPTH
    b.stop()


@cocotb.test()
async def test_random_traffic(dut):
    """Random enables, duty cycles that fill and drain it, many clock relationships."""
    total = 0
    for ci, (wp, rp, wph, rph) in enumerate(CLOCKS):
        b = Bench(dut, wp, rp, wph, rph, seed=100 + ci)
        await b.start()
        await b.run_words(2500, PHASES)
        b.stop()
        total += b.got
        dut._log.info(f"w {wp} ps / r {rp} ps: {b.got} words, {b.dropped} dropped (all reported), "
                      f"full seen {b.full_seen}x")
        assert b.got >= 2500 and b.dropped > 0 and b.full_seen > 0
        assert b.head_blocked > 0, "never saw the crossing delay: empty flag too optimistic?"
        await Timer(1, unit="ns")
    assert total > 15000


@cocotb.test()
async def test_cdc_model_exercised(dut):
    """The metastability model must really have resolved bits at random, in both directions."""
    b = Bench(dut, 6400, 4000, 0, 1370, seed=5)
    await b.start()
    await b.run_words(1500, PHASES)
    b.stop()
    hw, hr = int(dut.u_w2r.n_hit.value), int(dut.u_r2w.n_hit.value)
    dut._log.info(f"aperture hits: write->read {hw}, read->write {hr}")
    assert hw > 0 and hr > 0


@cocotb.test()
async def test_reset_midway(dut):
    """Reset with data inside, both sides together: empty again and fully working."""
    b = Bench(dut, 6400, 4000, 0, 1370, seed=9)
    await b.start()
    b.p_w, b.p_r = 0.9, 0.1
    await Timer(6400 * 30, unit="ps")
    assert len(b.q) > 2
    b.stop()
    b.p_w, b.p_r = 0.0, 0.0
    await b.start()                                # asserts reset again, clears the reference queue
    assert int(dut.rempty.value) == 1
    await b.run_words(800, PHASES)
    b.stop()


@cocotb.test()
async def test_crossing_latency(dut):
    """Delay from the write that enters an empty FIFO to the read-side flag falling, over all
    phases.  Recorded, and bounded: the cost of the crossing is part of the design's latency."""
    res = {}
    for wp, rp in ((6400, 4000), (4000, 6400), (6400, 6400)):
        lat = []
        for phase in range(0, rp, max(rp // 16, 1)):
            b = Bench(dut, wp, rp, 0, phase, seed=phase)
            b.tasks = []
            d = dut
            d.wr_en.value = 0; d.rd_en.value = 0; d.wrst.value = 1; d.rrst.value = 1
            ts = [cocotb.start_soon(clock(d.wclk, wp, 0)), cocotb.start_soon(clock(d.rclk, rp, phase))]
            await Timer(wp * 8, unit="ps")
            d.wrst.value = 0; d.rrst.value = 0
            for _ in range(6):
                await RisingEdge(d.rclk)
            for _ in range(3):
                await RisingEdge(d.wclk)
            d.wr_en.value = 1; d.wr_data.value = 0xABCD
            await RisingEdge(d.wclk)                # this edge writes the word
            t_w = now_ps()
            d.wr_en.value = 0
            n = 0
            while True:
                await RisingEdge(d.rclk)
                if int(d.rempty.value) == 0:
                    break
                n += 1
                assert n < 10
            # rempty is read BEFORE the edge, so the flag actually fell one read edge earlier
            lat.append((now_ps() - t_w) / rp - 1)   # read-clock cycles from the write edge to the flag falling
            assert int(d.rd_data.value) == 0xABCD
            for t in ts:
                t.cancel()
            await Timer(1, unit="ns")
        res[f"w{wp}_r{rp}"] = {"min_read_cycles": round(min(lat), 2), "max_read_cycles": round(max(lat), 2)}
        dut._log.info(f"w {wp} ps -> r {rp} ps: empty flag falls {min(lat):.2f}..{max(lat):.2f} read cycles after the write edge")
        # 2-flop synchroniser + registered flag: more than 2 and at most 3 read cycles; the aperture
        # model (1 ns) can push a bit that was caught changing into one more cycle.
        assert min(lat) > 1.99 and max(lat) <= 3 + 1000 / rp + 0.01, (min(lat), max(lat))
    _save("crossing_latency", res)
