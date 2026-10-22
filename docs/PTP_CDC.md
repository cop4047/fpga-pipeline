# Hardware timestamps and clock-domain crossing

This is the part of the design that lets latency be **measured by the hardware itself** and
that lets the book run on a clock unrelated to the network clock. Everything below is
verified in simulation; none of it has been through Vivado timing or CDC analysis or run on
a board (see *What is not verified*).

## 1. What was added

| Block | File | Clock | Role |
|---|---|---|---|
| Real-time clock | `mdp_ptp_rtc.sv` | network | Free-running 64-bit time (32.32 ns), rate trim and phase step: the two controls a PTP servo uses |
| Timestamp capture | `mdp_itch_unit.sv`, `mdp_itch_frame.sv` | network | Stamps every message when its last byte is on `s_axis` |
| Timestamp carriage | `mdp_itch_book.sv`, `mdp_itch_top.sv` | core | The stamp rides with the message through the book into the order request |
| Gray-coded async FIFO | `mdp_async_fifo.sv`, `mdp_sync2.sv` | both | The two clock crossings (message path and order path) |
| Reset synchronisers | `mdp_rst_sync.sv` | each | Asynchronous assert, synchronous release, one per domain |
| Latency statistics | `mdp_lat_stats.sv` | network | last / min / max / sum / count of (egress time − ingress stamp), plus a per-order stream |

```
        network clock `clk` (156.25 MHz)            core clock `clk_core`          network clock
 s_axis -> itch_frame --stamp--> msg FIFO ==CDC==> itch_book + trigger --> order FIFO ==CDC==> order_tx -> m_axis
              ^                  {ts,len,36B}        carries ts to the      {ts,sym,px,qty}      |
   mdp_ptp_rtc (one clock, one place)                  order request                         lat = rtc - ts
                                                                                          -> mdp_lat_stats
```

`ASYNC_CORE = 0` (default) keeps everything on `clk` with the original bypassed synchronous
FIFOs, so the single-clock numbers are unchanged. `ASYNC_CORE = 1` puts the book on
`clk_core` and swaps both FIFOs for `mdp_async_fifo`.

## 2. Timestamp definition (exact)

* **Ingress stamp** = RTC value in the cycle in which the beat that holds the message's *last
  byte* is presented on `s_axis`. (The edge that ends that cycle captures the byte.)
* **Egress** = RTC value in the cycle in which `mdp_order_tx` accepts the order request. (The
  edge that ends that cycle launches beat 0 of the order frame on `m_axis`.)
* **Latency** = egress − ingress (modulo 2^40, so a wrapped clock still subtracts correctly)
  = *capture edge of the last market-data byte → launch edge of the first order beat*.
  It excludes the MAC/PCS/PHY on both sides.

Both numbers come from the same clock in the same domain, so no timestamp crosses a clock:
the stamp travels as ordinary data through both FIFOs. That is the point of the structure:
a latency measurement across a CDC would otherwise need two synchronised clocks.

Carried timestamp width: 40 bits = 32 bits of ns + 8 fractional bits (1/256 ns).

## 3. The clock (`mdp_ptp_rtc`)

* Time `time_q`: 32.32 fixed-point ns, advanced every clock by `inc_q = cfg_inc + sext(cfg_trim)`.
* `cfg_inc`: 8.32 fixed-point ns per clock. 156.25 MHz → `round(6.4 · 2^32) = 0x6_6666_6666`,
  rounding error 0.015 ppb.
* `cfg_trim`: signed, one LSB = 2^-32 ns per clock = **0.036 ppb** of the clock rate, range ±7.8 %.
* `set_en` / `set_time`: step the clock.
* Wraps every 4.29 s. That is fine for differences; it is not a time of day.

This is the hardware half of IEEE 1588 timestamping. **Not implemented:** the 48-bit seconds
field and the 1e9 wrap, the PTP message stack (Sync / Delay_Req / BMCA), timestamping of PTP
event messages at the MAC, PPS output. The servo in `tests/test_rtc.py::test_servo` is a Python
PI loop acting on the two hardware controls, to show they are sufficient; it is not RTL.

## 4. The crossing (`mdp_async_fifo`)

Cummings-style dual-clock FIFO: binary and Gray pointers per side, the *other* side's Gray
pointer through a 2-flop synchroniser (`mdp_sync2`, `ASYNC_REG`), and `wfull` / `rempty`
registered from the **next** pointer values. Only Gray-coded pointers cross, so one bit
changes per transfer and a late-resolving bit gives the old pointer or the new one, never a
third value. The memory is written on `wclk` and read asynchronously (FWFT, LUTRAM) on `rclk`.

Choices that matter:

* **No back-pressure, same as the single-clock FIFO.** A write while full is dropped and counted
  (`wovf`). The market-data path cannot stall.
* **Flags are conservative.** `wfull` lags the real read pointer, so the FIFO can look fuller
  than it is; `rempty` lags the write pointer, so it can look empty when data has just arrived.
  Neither flag can lie the other way. `wcount` never under-reports, `rcount` never over-reports.
* **A crossing costs 2 to 3 read-clock cycles** from the write edge to the flag falling
  (measured, section 6). Each crossing also removes the "bypass when empty" shortcut of the
  single-clock FIFO.
* **Reset:** both sides must be held in reset together for a few cycles of the slower clock.
  `mdp_rst_sync` gives each domain its own release.
* **Quasi-static configuration** (`cfg_*`) is read in whichever domain uses it without
  synchronisers, and the statistics counters live in the domain of the event they count. Both are
  meant to be changed/read while traffic is stopped. A real design would write them through a
  handshake.

## 5. Verification

| Test | What it proves |
|---|---|
| `test_rtc.py` | Bit-exact against a Python model on every cycle with random increment / trim / step changes; exact free-run; wrap; 1 trim LSB = exactly k·N LSBs after N clocks; a PI servo pulls a clock that is 100 ppm slow to sub-ns error |
| `test_framer.py` | Every message's stamp equals the clock value in the cycle its last byte was presented, over random frames with idle cycles inside frames |
| `test_async_fifo.py` | Seven clock relationships (faster / slower / equal / 6400 vs 6401 ps drifting through every alignment). Every cycle: head word is the oldest unread word, no write accepted past the depth, exactly one `wovf` pulse per dropped write, Gray pointers change at most 1 bit per cycle, count outputs are one-sided bounds. Mid-run reset. Crossing latency recorded |
| `test_pipeline.py` (both builds) | All the existing scoreboards (top of book, drops, order bytes, 12 counters) **plus**: the latency the hardware reports for **every order** equals the simulator's own timeline (capture edge of the triggering message's last byte → launch edge of the order), and last/min/max/sum/count are right. Clock sweep: nine core/network clock pairs, 111 MHz to 300 MHz, with different phases |

**CDC-aware simulation model.** Plain RTL simulation never produces a metastable flop, so it
cannot tell a Gray pointer from a binary one. With `-DMDP_CDC_SIM`, `mdp_sync2` resolves any bit
that changed less than 1 ns before the sampling edge to old-or-new at random (`test_cdc_model_exercised`
checks the model really fired: thousands of times per run). A correct Gray FIFO passes; a binary
pointer does not (mutation `write pointer crosses in BINARY`). Synthesis never sees the define.

**Mutation testing** (`make ptp-mutation`, results in `docs/mutation_report.txt`):
| Suite | Injected | Caught | Equivalent |
|---|---|---|---|
| RTC (`rtc`) | 8 | 8 | 0 |
| Async FIFO (`fifo`) | 13 | 13 | 0 |
| Framer stamps (`ts-framer`) | 6 | 5 | 1 |
| Pipeline stamps and statistics (`ts-pipe`) | 13 | 12 | 1 |
| CDC wiring (`cdc-pipe`) | 5 | 5 | 0 |
| **Total** | **45** | **43** | **2** |

The two equivalent mutants are unobservable by construction: a second end event cannot reach a unit (the tracker raises one per block), and a latency above 2^32 units (0.5 s) cannot occur, so the upper 8 bits of the latency sum are always zero. Both are marked `(equivalent)` in `tb_itch/mutation_test.py`. One survivor in the first run was a script bug (a mutant pattern with the wrong indentation, so it never ran); fixed and re-run, and it is caught.

## 6. Results (simulated)

| Measurement | Result |
|---|---|
| RTC nominal rate error | 0.015 ppb (increment 8.32 ns at 156.25 MHz); trim LSB 0.036 ppb, range +/-7.8 % |
| RTC servo test | a 58 ns uncorrected drift is held to under 0.001 ns over the last 200 updates |
| Async FIFO crossing delay | empty flag falls 2.06 to 3.19 read cycles after the write edge |
| FIFO random traffic | 7 clock pairs, about 2,500 words each, every dropped word reported, none lost silently |
| Tick-to-trade, one clock | 44.8 ns best, 51.2 ns worst (7 to 8 cycles) |
| Tick-to-trade, two clocks (core 250 MHz) | 70.4 ns best, 76.8 ns worst |
| Cost of CDC | about +20 to +25 ns (two crossings) |
| Yosys estimate, one clock | about 4,630 LUT, 3,600 FF, 4 block RAMs |
| Yosys estimate, two clocks | about 4,250 LUT, 3,660 FF, 4 block RAMs |

The Yosys figures are estimates and the LUT difference between builds is optimisation noise, not a saving from the CDC. They exclude RAM32M/RAM64M distributed-RAM primitives.

## 7. What is not verified

* **No Vivado run of this version.** Timing of the clock, the FIFOs and the extra stamp bits is
  unknown; `syn/cdc.xdc` and the `ASYNC_CORE` option of `syn/build.tcl` were written without Vivado
  and must be run once and corrected (`report_cdc`, `report_clock_interaction`).
* **Metastability is modelled, not physical.** The aperture model checks the *logic* of the
  crossing. It cannot show that a 2-flop synchroniser has enough MTBF at the real clock rates.
* **Two real CDC bugs are invisible in RTL simulation:** a 1-flop instead of a 2-flop
  synchroniser (the first flop's output is only "uncertain" in the model, never held), and a reset
  released on the wrong clock. They are caught by `ASYNC_REG` / `report_cdc` and review, not by tests.
* No hardware: no MAC, no PHY, no real PTP grandmaster. Latency is core logic between the AXI-Streams.
* Quasi-static configuration and counter read-out cross domains without synchronisers (section 4).
* Timestamp resolution is the clock period (6.4 ns); a design that needs sub-cycle stamps would add
  a TDC / SERDES-phase measurement.
