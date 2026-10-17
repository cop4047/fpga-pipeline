# mdpipe — a 10G ITCH 5.0 market-data → order FPGA pipeline (SystemVerilog + cocotb)

Receive Nasdaq **ITCH 5.0 over MoldUDP64** frames on a 64-bit AXI-Stream, keep a
price-level order book in hardware, fire an order on a price trigger, and emit the
order frame. **7–8 clock cycles (45–51 ns @ 156.25 MHz) tick-to-trade in RTL
simulation.** Built as a portfolio project for low-latency / HFT FPGA roles:
spec → Python golden model → C++ software baseline → RTL → constrained-random
verification with mutation testing → synthesis flow → (next) board.

```
s_axis -> itch_frame -> [FIFO] -> itch_book + trigger -> [FIFO] -> order_tx -> m_axis
          eth/ip/udp    bypass    decode, order table,     bypass     60B frame
          MoldUDP64                level RAM, occupancy
          seq tracking,
          message framer
```

Protocol rules and every design decision: **[docs/ITCH50.md](docs/ITCH50.md)**.
The earlier fixed-format "ITCH-lite" design (`mdp_top`, tags `v1`, `v2`, `v2.1`) is
kept and still builds; it is described in **[docs/SPEC.md](docs/SPEC.md)**.

## Results so far

### ITCH 5.0 / MoldUDP64 design (current)

| | |
|---|---|
| Protocol | ITCH 5.0 `A F E C X D U` (others parsed, skipped, counted) in MoldUDP64: sequence tracking (gap, duplicate, heartbeat, end of session), length-prefixed unaligned messages at 64 bits/clock |
| Tick-to-trade (simulated, idle pipeline) | **7–8 cycles = 45–51 ns @ 156.25 MHz (35–40 ns @ 200 MHz)**, by the byte lane where the message starts. ITCH-lite took 5: real framing and decode cost 2–3 cycles. |
| Software baseline | C++ port of the same rules, bit-exact with the model: 30.6 M msgs/s single core; about 100 ns frame → order built (±20 ns, laptop under WSL, not isolated) |
| Regression | 11 full-pipeline cocotb tests + 4 framer tests, ≈ 24 k random messages per run, all 8 drop reasons, bit-exact vs Python model |
| Mutation testing | framer 16/16 non-equivalent bugs caught (17 injected); book 49/49 (50 injected). Each equivalent mutant is explained in the script. |
| Lint | Verilator `-Wall` clean |
| yosys xc7 estimate | ≈ 4,300 LUT, ≈ 3,000 FF, 4 block RAMs (*estimate only*) |
| **Vivado timing / utilisation** | **not measured yet** for this design. Run `syn/build.tcl` then `syn/sweep.tcl`. |
| Hardware | not yet |

Latency is *core logic between the AXI-Streams* in simulation. It excludes
MAC/PCS/PHY and is not a hardware measurement.

### ITCH-lite design (v2.1), measured in Vivado

| | |
|---|---|
| Tick-to-trade (simulated) | 5 cycles = 32 ns @ 156.25 MHz (v1: 10 cycles) |
| **Vivado timing (post-route)** | **Closes at 156.25 MHz (WNS +0.496 ns) and 200 MHz (+0.088 ns). Does not close at 250 MHz (−1.036 ns) or 303 MHz (−1.429 ns).** Achieved Fmax ≈ 200 MHz. |
| Vivado utilisation | 5,820 LUTs (2,756 logic + 3,064 LUTRAM, 14.2 %), 1,798 FF, 0 BRAM, 0 DSP |

Vivado 2026.1, part `xc7k70tfbg676-2`, **out-of-context** (no MAC, no I/O
constraints). Slack varies by about ±0.3 ns between runs of the same design.

### Timing-closure log (ITCH-lite)

Each row is one iteration of the same sweep (WNS in ns, post-route):

| Iteration | 156.25 MHz | 200 MHz | 250 MHz | 303 MHz |
|---|---|---|---|---|
| Baseline | +0.592 | +0.059 | −0.702 | −1.504 |
| Order payload registers load every `BT` cycle (only `ord_valid` gated) | +0.607 | +0.021 | −0.759 | −1.745 |
| Execute clamp as one subtract + borrow bit (was compare → mux → subtract) | +0.496 | +0.088 | −1.036 | −1.429 |

What this showed:

- Neither change moved Fmax. The first removed a trigger-logic cone from a
  40-wide clock-enable; no measurable gain. The second removed a serial
  compare→subtract; the worst path simply moved elsewhere.
- My early hypothesis was that the priority encode or the async-read RAMs limited
  Fmax. The reports did not support that. At 4.0 ns the worst path was input
  decode: message-FIFO read pointer (fan-out 137) → LUTRAM read → bypass mux →
  32-bit price compare → drop-reason logic → `book/state` (13 logic levels).
- Several paths sit at roughly 4.5–5 ns, so fixing one only exposes the next.
  The ITCH 5.0 book registers the message first and decodes over D1/D2/RD, which
  removes that particular path by construction; whether it helps is what the next
  Vivado run will show.

## Quick start

```bash
pip install cocotb pytest            # + iverilog and verilator from your package manager
make itch-pipe            # full ITCH 5.0 pipeline cocotb regression, ~35 s
make itch-sim             # framer-only regression
make itch-lint            # Verilator -Wall on the ITCH design
make itch-mutation        # framer: inject 17 bugs, ~3 min
make itch-book-mutation   # book: inject 50 bugs, ~12 min
make itch-yosys           # open-source resource estimate
make sim lint mutation    # the same for the older ITCH-lite design
# software baseline (bit-exact check against the model, then benchmark)
cd baseline && make itch-check && make itch-bench
# with Vivado on PATH:
make vivado PART=xc7k70tfbg676-2 PERIOD=6.4
make sweep
```

From the Vivado Tcl console (run `build.tcl` first: the sweep reopens its
`<top>_synth.dcp`, and a stale checkpoint gives stale numbers):

```tcl
cd C:/path/to/fpga-pipeline/syn
source build.tcl      ;# ITCH 5.0 design (mdp_itch_top): reports in syn/out_itch
source sweep.tcl      ;# 6.4 / 5.5 / 5.0 / 4.5 / 4.0 ns -> syn/out_itch/sweep.csv
# the older design: set TOP mdp_top  before sourcing either script (reports in syn/out)
```

The free Vivado BASIC licence covers `xc7k70tfbg676-2`. If `get_parts` returns
nothing, check that the selected licence is not the Alveo one.

Windows: run the simulation in WSL2 (Icarus + cocotb are painless there) and keep
Vivado native for synthesis; the Vivado Tcl scripts are plain batch Tcl.

## Layout

```
rtl/       ITCH 5.0:  mdp_itch_frame  mdp_itch_unit  mdp_itch_book  mdp_itch_top
           shared:    mdp_fifo  mdp_order_tx
           ITCH-lite: mdp_hdr_parse  mdp_msg_asm  mdp_book  mdp_top
model/     itch50.py   ITCH 5.0 / MoldUDP64 golden model, frame builders, generator
           mdp_model.py   ITCH-lite model (also supplies the order-frame builder)
baseline/  cpu_itch50.cpp  gen_itch_vectors.py   C++ software baseline, bit-exact check + benchmark
tb_itch/   test_framer.py  test_pipeline.py  mutation_test.py  Makefile
tb/        ITCH-lite testbench and mutation test
syn/       build.tcl  sweep.tcl  yosys.ys  yosys_itch.ys  board_template.xdc
docs/      ITCH50.md (current design)  SPEC.md (ITCH-lite)  INTERVIEW.md  mutation_report.txt
```

## Next steps

1. **Vivado on the ITCH 5.0 design.** `source build.tcl`, `source sweep.tcl`, read
   `syn/out_itch/worst_paths_*.rpt`, record the result as new rows of the timing
   log. Expect 200 MHz to need work (the D2 price-to-level compare and the
   best-ask encode are the likely long paths). A real "WNS −x → +y after Z" entry
   is worth more than any claim.
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

Say *simulated* until you have hardware numbers. Add a timing clause only after
the ITCH design has its own Vivado run.

> **Low-latency ITCH 5.0 market-data-to-order FPGA pipeline** (SystemVerilog,
> cocotb, Python, C++) — Designed a 64-bit AXI-Stream pipeline that parses
> Nasdaq ITCH 5.0 over MoldUDP64 (unaligned variable-length messages, sequence
> gap/duplicate handling), maintains a hardware price-level order book with
> 64-bit-tagged order lookup, cancel/replace and a symbol watchlist, and fires
> an edge-armed price trigger into an order frame: 7–8 cycles (45–51 ns @
> 156.25 MHz) tick-to-trade in RTL simulation.
>
> Verified bit-exact against a Python golden model and a C++ software baseline
> (30 M msgs/s) with constrained-random traffic and mutation testing (65 of 65
> non-equivalent injected bugs caught across framer and book).

Do not claim a hardware latency, "real ITCH data" (not replayed yet), or any
Fmax for this design before it is measured. The 200 MHz post-route result belongs
to the earlier ITCH-lite core and must be labelled as such.

## Honest limitations

Fixed 64-tick price window, 4-symbol watchlist, direct-mapped 1024-entry order
table (collisions are dropped, not chained), one message in flight in the book
(about one Add per 5 cycles, close to the line rate for 38-byte messages), no
matching or risk logic, no back-pressure (overflow is counted, not prevented),
sequence gaps are detected but not recovered, only the first 36 bytes of a
message are used. Verification is simulation against my own model of the
specification; real Nasdaq sample data has not been replayed yet. Timing is
out-of-context only: no MAC, no I/O timing, no board. Details in
`docs/ITCH50.md`, `docs/SPEC.md` §5.4 and §10.
