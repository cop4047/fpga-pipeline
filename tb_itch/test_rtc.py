"""cocotb tests for mdp_ptp_rtc, the hardware real-time clock.

* test_nominal           reset, step to a value, free-run: time == set + n * increment, to the last bit
* test_random_controls   every cycle: random changes of increment, trim and steps, compared bit for bit
                         with a Python model of the documented behaviour
* test_wrap              the 64-bit count wraps cleanly
* test_trim_resolution   one trim LSB is 2^-32 ns per clock: n clocks later the clock is n LSBs ahead
* test_servo             a software PI loop (the testbench) disciplines a clock whose oscillator is 100 ppm
                         slow, using only the two controls the hardware offers (rate trim and step)
"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

M40 = (1 << 40) - 1
M64 = (1 << 64) - 1
INC_156 = round(6.4 * 2 ** 32)           # 156.25 MHz


class Model:
    """Cycle-exact model of mdp_ptp_rtc (documented behaviour, not a copy of the RTL text)."""

    def __init__(self):
        self.time = 0
        self.inc_q = 0

    def step(self, rst, inc, trim, set_en, set_time):
        if rst:
            self.time, self.inc_q = 0, 0
            return
        nxt_inc = (inc + trim) & M40
        nxt_time = set_time if set_en else (self.time + self.inc_q) & M64
        self.time, self.inc_q = nxt_time, nxt_inc


def signed32(v):
    return v & 0xFFFFFFFF


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 6400, unit="ps").start())
    dut.rst.value = 1
    dut.cfg_inc.value = INC_156
    dut.cfg_trim.value = 0
    dut.set_en.value = 0
    dut.set_time.value = 0
    await ClockCycles(dut.clk, 4)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_nominal(dut):
    """Step the clock to a value, then free-run: the value appears once, and from then on every
    clock adds exactly the increment."""
    await setup(dut)
    v0 = 123456789 << 32
    dut.set_time.value = v0
    dut.set_en.value = 1
    await RisingEdge(dut.clk)                   # the edge that samples set_en and loads v0
    dut.set_en.value = 0
    vals = []
    for _ in range(4000):                       # each read returns the value from before that edge
        await RisingEdge(dut.clk)
        vals.append(int(dut.time_q.value))
    assert vals[0] == v0, f"first value after the step is {vals[0]:#x}, not the value that was loaded"
    diffs = {(b - a) & M64 for a, b in zip(vals, vals[1:])}
    assert diffs == {INC_156}, f"increments seen: {sorted(diffs)[:5]}"
    # frequency: 156.25 MHz nominal gives 6.4 ns per clock to within the 2^-32 ns increment rounding
    err_ns_per_clk = (INC_156 / 2 ** 32) - 6.4
    assert abs(err_ns_per_clk) < 2 ** -32
    dut._log.info(f"nominal increment error: {err_ns_per_clk * 1000:.3e} ps per clock "
                  f"= {err_ns_per_clk / 6.4 * 1e9:.3f} ppb")


@cocotb.test()
async def test_random_controls(dut):
    """Bit-exact against the model with random inc / trim / set changes on random cycles."""
    await setup(dut)
    rng = random.Random(77)
    m = Model()
    # bring the model into step with the DUT: it was reset 5 cycles ago and has run since; the
    # simplest exact way is to force both with a reset
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    m.step(True, 0, 0, 0, 0)
    dut.rst.value = 0
    inc, trim, set_en, set_time = INC_156, 0, 0, 0
    for cyc in range(30000):
        if rng.random() < 0.01:
            inc = rng.choice((INC_156, rng.getrandbits(40), rng.randrange(1 << 36), 0, M40))
        if rng.random() < 0.02:
            trim = rng.choice((0, 1, -1, rng.randrange(-(1 << 31), 1 << 31), -(1 << 31), (1 << 31) - 1,
                               rng.randrange(-1000, 1000)))
        set_en = int(rng.random() < 0.003)
        set_time = rng.choice((rng.getrandbits(64), M64 - rng.randrange(0, 1 << 34), 0))
        dut.cfg_inc.value = inc
        dut.cfg_trim.value = signed32(trim)
        dut.set_en.value = set_en
        dut.set_time.value = set_time
        await RisingEdge(dut.clk)
        got = int(dut.time_q.value)              # value before this edge
        assert got == m.time, f"cycle {cyc}: rtl {got:#x} model {m.time:#x}"
        m.step(False, inc, trim, set_en, set_time)


@cocotb.test()
async def test_wrap(dut):
    await setup(dut)
    dut.set_time.value = M64 - 5 * INC_156
    dut.set_en.value = 1
    await RisingEdge(dut.clk)
    dut.set_en.value = 0
    seen_wrap = False
    prev = None
    for _ in range(40):
        await RisingEdge(dut.clk)
        v = int(dut.time_q.value)
        if prev is not None and v < prev:
            seen_wrap = True
            assert v == (prev + INC_156) & M64
        prev = v
    assert seen_wrap


@cocotb.test()
async def test_trim_resolution(dut):
    """trim = +k adds k * 2^-32 ns per clock: after N clocks the clock is k*N LSBs ahead."""
    await setup(dut)
    N = 5000
    results = {}
    for k in (0, 1, 7, -3, 100000):
        dut.cfg_trim.value = signed32(k)
        dut.set_time.value = 0
        dut.set_en.value = 1
        await RisingEdge(dut.clk)
        dut.set_en.value = 0
        await ClockCycles(dut.clk, 3)             # let the registered increment settle
        await RisingEdge(dut.clk)
        t0 = int(dut.time_q.value)
        await ClockCycles(dut.clk, N)
        t1 = int(dut.time_q.value)
        results[k] = ((t1 - t0) & M64) - N * INC_156
        assert results[k] == k * N, f"trim {k}: advanced {results[k]} LSBs extra, expected {k * N}"
    dut._log.info(f"1 trim LSB = {2 ** -32 / 6.4 * 1e9:.3f} ppb of the 156.25 MHz clock rate; "
                  f"range +/-{2 ** 31 * 2 ** -32 / 6.4 * 100:.1f} %")


@cocotb.test()
async def test_servo(dut):
    """The clock's oscillator is 100 ppm slow (a model of a crystal): the DUT counts 6.4 ns per
    clock while true time advances 6.4 ns * (1 + 100e-6).  A PI controller (in Python, as a PTP
    slave's servo would be in software) reads the clock every 100 clocks and steers it with the
    rate trim.  The error must converge to well under a nanosecond."""
    await setup(dut)
    TRUE_NS_PER_CLK = 6.4 * (1 + 100e-6)
    interval = 100
    dut.cfg_trim.value = 0
    dut.set_time.value = 0
    dut.set_en.value = 1
    await RisingEdge(dut.clk)
    dut.set_en.value = 0
    await ClockCycles(dut.clk, 3)
    clocks = 0
    kp, ki = 0.5, 0.05
    integ = 0.0
    trim = 0.0
    errs = []
    start = None
    for step in range(900):
        await ClockCycles(dut.clk, interval)
        clocks += interval
        t = int(dut.time_q.value) / 2 ** 32           # ns, as read (before the edge)
        if start is None:
            start = t - clocks * 6.4                  # fix the phase origin: true time == DUT time at start
        true = start + clocks * TRUE_NS_PER_CLK
        err = t - true                                # ns (negative: the clock is behind)
        errs.append(err)
        integ += err
        trim = -(kp * err + ki * integ) / interval    # ns per clock
        dut.cfg_trim.value = signed32(round(trim * 2 ** 32))
    worst_late = max(abs(e) for e in errs[-200:])
    dut._log.info(f"uncorrected the clock would be {900 * interval * 6.4 * 100e-6:.0f} ns off by the end; "
                  f"with the servo: |error| <= {worst_late:.3f} ns over the last 200 updates; "
                  f"max error during pull-in {max(abs(e) for e in errs):.2f} ns")
    assert worst_late < 0.5
