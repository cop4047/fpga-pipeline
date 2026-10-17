# a 10G market-data → order FPGA pipeline (SystemVerilog + cocotb)

Parse a UDP market-data frame, keep a price-level order book in hardware, fire
an order on a trigger, and emit the order frame — between two 64-bit
AXI-Streams, **5 clock cycles (32 ns @ 156.25 MHz) tick-to-trade in RTL
simulation**. Built as a portfolio project for low-latency / HFT FPGA roles:
spec → Python golden model → RTL → constrained-random verification with
mutation testing → synthesis flow → (next) board.

```
s_axis -> hdr_parse -> msg_asm -> [FIFO] -> book + trigger -> [FIFO] -> order_tx -> m_axis
          eth/ip/udp   16B msgs    bypass    order table                bypass      60B frame
          4B realign                         level RAM, occupancy
```

Full detail: **[docs/SPEC.md](docs/SPEC.md)** · interview prep:
**[docs/INTERVIEW.md](docs/INTERVIEW.md)**

## Results so far

| | |
|---|---|
| Tick-to-trade (simulated, idle pipeline) | **5 cycles = 32 ns @ 156.25 MHz, or 25 ns @ 200 MHz** (v1: 10 cycles / 64 ns — tags `v1`, `v2`). The cycle count is from RTL simulation; the 200 MHz figure applies that count to the clock Vivado closed timing at. |
| Book throughput | 3 cycles/msg (Add), 4 (Delete/Execute) ≈ 52 / 39 M msg/s |
| Regression | 10 cocotb tests, ≈ 11.6 k random messages bit-exact vs Python model |
| Mutation testing | 14 injected RTL bugs, 14 caught (`docs/mutation_report.txt`) |
| Lint | Verilator `-Wall` clean |
| yosys xc7 estimate | ≈ 2170 LUT, ≈ 1690 FF, 4 BRAM18 + LUTRAM (*estimate only; Vivado differs, see below*) |
| **Vivado timing (post-route)** | **Closes at 156.25 MHz (WNS +0.496 ns) and 200 MHz (+0.088 ns). Does not close at 250 MHz (−1.036 ns) or 303 MHz (−1.429 ns).** Achieved Fmax ≈ 200 MHz. |
| Vivado utilisation | 5,820 LUTs (2,756 logic + 3,064 LUTRAM, 14.2 %), 1,798 FF, 0 BRAM, 0 DSP. Vivado used distributed RAM where yosys predicted 4 BRAM18. |
| Hardware | not yet |

Vivado 2026.1, part `xc7k70tfbg676-2`, **out-of-context** (no MAC, no I/O
constraints), `syn/build.tcl` then `syn/sweep.tcl`. Slack varies by about ±0.3 ns
between runs of the same design, so small differences on the failing targets are
noise; the pass/fail pattern is stable.

Latency is *core logic between the AXI-Streams* in simulation. It excludes
MAC/PCS/PHY and is not a hardware measurement. The protocol is **"ITCH-lite"**
(fixed 16-byte Add/Delete/Execute), not Nasdaq ITCH 5.0.

### Timing-closure log

Each row is one iteration of the same sweep (WNS in ns, post-route):

| Iteration | 156.25 MHz | 200 MHz | 250 MHz | 303 MHz |
|---|---|---|---|---|
| Baseline | +0.592 | +0.059 | −0.702 | −1.504 |
| Order payload registers load every `BT` cycle (only `ord_valid` gated) | +0.607 | +0.021 | −0.759 | −1.745 |
| Execute clamp as one subtract + borrow bit (was compare → mux → subtract) | +0.496 | +0.088 | −1.036 | −1.429 |

What this showed:

- Neither change moved Fmax. The first removed a trigger-logic cone from a
  40-wide clock-enable; no measurable gain. The second removed a serial
  compare→subtract (the 4.0 ns worst path had been the execute update into the
  order table); the worst path simply moved elsewhere.
- My early hypothesis was that the priority encode or the async-read RAMs limited
  Fmax. The reports did not support that. At 4.0 ns the worst path is now input
  decode: message-FIFO read pointer (fan-out 137) → LUTRAM read → bypass mux →
  32-bit `i_px_ok` compare → drop-reason logic → `book/state` (13 logic levels,
  4.94 ns, about 80 % routing delay).
- Several paths sit at roughly 4.5–5 ns, so fixing one only exposes the next.
  Candidates for 250 MHz: compute the price-window check in `mdp_msg_asm` and
  carry it with the message (no added latency), or add an input register stage
  (+1 cycle). Not done; 200 MHz is the honest claim.
- Raising the clock mostly buys throughput. Single-order latency improves only
  from fewer cycles or a faster clock on the same 5 cycles (32 ns → 25 ns).

## Quick start

```bash
pip install cocotb pytest            # + iverilog and verilator from your package manager
make sim          # cocotb regression, ~12 s
make lint         # Verilator -Wall
make mutation     # inject bugs, check the testbench catches them, ~2 min
make yosys        # open-source resource estimate
# with Vivado on PATH:
make vivado PART=xc7k70tfbg676-2 PERIOD=6.4
make sweep
```

From the Vivado Tcl console (run `build.tcl` first: the sweep reopens its
`mdp_top_synth.dcp`, and a stale checkpoint gives stale numbers):

```tcl
cd C:/path/to/fpga-pipeline/syn
source build.tcl
source sweep.tcl      ;# 6.4 / 5.0 / 4.0 / 3.3 ns, writes out/sweep.csv and out/worst_paths_<period>ns.rpt
```

The free Vivado BASIC licence covers `xc7k70tfbg676-2`. If `get_parts` returns
nothing, check that the selected licence is not the Alveo one.

Windows: run the simulation in WSL2 (Icarus + cocotb are painless there) and keep
Vivado native for synthesis; the Vivado Tcl scripts are plain batch Tcl.

## Layout

```
rtl/    mdp_hdr_parse  mdp_msg_asm  mdp_fifo  mdp_book  mdp_order_tx  mdp_top
model/  mdp_model.py   golden model + frame builders + constrained-random generator
tb/     test_mdpipe.py (cocotb)  mutation_test.py  Makefile  results.json
syn/    build.tcl  sweep.tcl  yosys.ys  board_template.xdc
docs/   SPEC.md  INTERVIEW.md  mutation_report.txt
```

## Next steps

1. **Vivado: done to 200 MHz.** The sweep and the iteration log are above. Keep
   `docs/SPEC.md` §7.2 and `docs/INTERVIEW.md` question 7 in step with them
   (question 7 still describes the original hypothesis).
2. **Own every line.** Read the RTL against `docs/INTERVIEW.md` until you can
   redraw each block and explain each trade-off without the files. Interviewers
   will ask you to modify the design on a whiteboard.
3. **Then the board (Phase 5).** Integrate a
   10G MAC (Forencich `verilog-ethernet`), cross clocks with your Gray-coded
   FIFO from the PTP project, and pick a board with real SFP+ and 10G-capable
   transceivers (Artix-7 GTP transceivers cannot do 10G). Check the part is in
   your Vivado edition's device list *before* buying. A 1G version of the same
   architecture is a valid first hardware milestone if 10G is a stretch.
4. **Extend it your way** (roadmap in `docs/SPEC.md` §8): the PTP timestamping
   core for hardware latency measurement, a pipelined book, formal checks.
   Changes you make and can explain are worth more than the repo as delivered.

## What you can honestly put on the CV

Say *simulated* until you have hardware numbers, and don't claim real ITCH.

> **Low-latency market-data-to-order FPGA pipeline** (SystemVerilog, cocotb) —
> Designed a 64-bit AXI-Stream pipeline: Ethernet/UDP header screening with
> payload realignment, ITCH-style message decode, a hardware price-level order
> book (order table, level RAM, occupancy bitmap + priority encode) and an
> edge-armed price trigger generating the order frame. Iterated tick-to-trade
> from 10 to 5 clock cycles (32 ns @ 156.25 MHz, 25 ns @ 200 MHz; RTL
> simulation) by removing idle-path queueing and merging pipeline stages.
>
> Verified against a Python golden model with constrained-random traffic,
> functional coverage and mutation testing (14/14 injected bugs caught). Closed
> timing at 200 MHz post-route (out-of-context, Vivado 2026.1, Kintex-7
> xc7k70tfbg676-2) and traced the 250 MHz limit to input-decode logic through
> three sweep iterations.

Do not claim 250 MHz, a hardware latency, or real ITCH. "Simulated" stays on
every latency number until there is a board.

## Honest limitations

Fixed 64-tick price window, direct-mapped 1024-entry order table, one message in
flight in the book, no matching or risk logic, no back-pressure (overflow is
counted, not prevented). Timing is out-of-context only: no MAC, no I/O timing,
no board. Fmax is about 200 MHz, not the 250 MHz that would suit a 128-bit/25G
datapath. All listed in `docs/SPEC.md` §5.4 and §10.