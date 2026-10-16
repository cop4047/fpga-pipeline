# mdpipe — a 10G market-data → order FPGA pipeline (SystemVerilog + cocotb)

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
| Tick-to-trade (simulated, idle pipeline) | **5 cycles = 32 ns @ 156.25 MHz** (v1: 10 cycles / 64 ns — tags `v1`, `v2`) |
| Book throughput | 3 cycles/msg (Add), 4 (Delete/Execute) ≈ 52 / 39 M msg/s |
| Regression | 10 cocotb tests, ≈ 11.6 k random messages bit-exact vs Python model |
| Mutation testing | 14 injected RTL bugs, 14 caught (`docs/mutation_report.txt`) |
| Lint | Verilator `-Wall` clean |
| yosys xc7 estimate | ≈ 2170 LUT, ≈ 1690 FF, 4 BRAM18 + LUTRAM (*estimate*) |
| **Vivado timing / Fmax** | **not measured yet — see "Your turn" below** |
| Hardware | not yet |

Latency is *core logic between the AXI-Streams* in simulation. It excludes
MAC/PCS/PHY and is not a hardware measurement. The protocol is **"ITCH-lite"**
(fixed 16-byte Add/Delete/Execute), not Nasdaq ITCH 5.0.

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

## Your turn (what I could not do for you)

1. **Run Vivado.** `make vivado` and `make sweep`, then fill the table in
   `docs/SPEC.md` §7.2 and the line above. The Tcl was written without a Vivado
   to test against — expect to fix small version-specific things. Pick a part
   your free Vivado edition supports (an unsupported part errors at synthesis,
   not in the part chooser). Record at least one timing-closure iteration
   ("WNS −x → +y after Z"); that is the thing interviewers dig into.
2. **Own every line.** Read the RTL against `docs/INTERVIEW.md` until you can
   redraw each block and explain each trade-off without the files. Interviewers
   will ask you to modify the design on a whiteboard.
3. **Then the board (Phase 5).** Only after Vivado closes timing: integrate a
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
> from 10 to 5 clock cycles (32 ns @ 156.25 MHz, RTL simulation) by removing
> idle-path queueing and merging pipeline stages.
>
> Verified against a Python golden model with constrained-random traffic,
> functional coverage and mutation testing (14/14 injected bugs caught); closed
> timing at *[X MHz]* on *[part]* in Vivado *(fill in once measured)*.

If you finish only the Vivado step, the last clause becomes true; until then,
delete it.

## Honest limitations

Fixed 64-tick price window, direct-mapped 1024-entry order table, one message in
flight in the book, no matching or risk logic, no back-pressure (overflow is
counted, not prevented). All listed in `docs/SPEC.md` §5.4 and §10.
