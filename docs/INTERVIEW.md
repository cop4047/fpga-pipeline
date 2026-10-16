# Interview walkthrough — questions this project invites

Short answers tied to *this* design. Rewrite them in your own words; you will be
asked to go deeper on any of them.

**1. Walk me through the datapath.**
64-bit AXI-Stream in, no back-pressure. `mdp_hdr_parse` checks ethertype, IHL,
protocol and UDP port over beats 1–4 while keeping the previous beat, then from
beat 6 emits realigned words. `mdp_msg_asm` pairs two words into a 16-byte
message. The book validates, updates the order table / level RAM / occupancy
mask, priority-encodes best bid/ask, and a per-symbol edge-armed trigger emits
an order request. `mdp_order_tx` streams a 60-byte frame. Two FIFOs smooth
bursts and are bypassed when empty.

**2. Why 64 bits at 156.25 MHz?**
64 x 156.25 MHz = 10 Gb/s exactly: one beat per clock at line rate, no clock
conversion math, and it matches the XGMII/PCS 64b/66b data rate of a 10G MAC.

**3. Why is there a realignment stage?**
Eth 14 + IP 20 + UDP 8 + count 2 = 44 bytes, and 44 mod 8 = 4, so every message
word straddles two beats. Keeping the previous beat's upper 4 bytes and
emitting `{cur[31:0], prev[63:32]}` realigns with one register and no extra latency.

**4. Why no back-pressure? What happens on overload?**
An exchange feed cannot be paused. The only choices are to keep up or lose data
visibly. FIFO overflow drops and increments `cnt_ovf`; `test_overload_is_counted`
shows it is detected and the design recovers. Capacity: book ≈ 3–4 cycles/msg,
transmitter 8 cycles/order.

**5. How is the order book organised, and why?**
Per `{symbol, side}`: a 64-bit occupancy bitmap in flops plus a quantity RAM per
level. Best bid/ask is a priority encode over the bitmap — no RAM scan, constant
latency. Alternatives: sorted list (insert is O(n) / shifting registers), CAM
(expensive), tree (variable latency). Cost of the choice: a bounded price window.

**6. Delete/Execute carry only an order ref — how do you find the level?**
A direct-mapped order table indexed by the ref stores `{valid, sym, side, level,
remaining}`. Limit: refs must fit `ORD_AW` bits. A real design hashes 64-bit refs
(with collision handling) or uses a CAM/hash table with chaining.

**7. What limits Fmax?**
My bet: the `BT` state — occupancy bitmap → 64-way priority encode → level
compare → `fired`/order register — and the Add path's level-RAM read → 32-bit add
→ write. I made the trigger compare in the level domain to keep a 32-bit adder
out of the encode path. (Replace this with what the Vivado timing report says.)

**8. Where do the 5 cycles go, and how would you cut more?**
Parse 0, assemble +1, accept +2, table/level update +3, encode+trigger +4, first
order beat +5. v1 was 10: both FIFOs were always in the path, decode was its own
state, and the best-price encode and trigger were separate stages. Next:
speculative/cut-through transmit (send the constant header before the decision,
poison if not firing), and merging assemble into parse.

**9. How did you verify it?**
Python golden model fed identical frames; scoreboards on top-of-book tap, drop
tap, order bytes and counters. Directed tests for each rule, constrained-random
with functional-coverage assertions, then **mutation testing**: 14 injected bugs,
all caught. The first run left two survivors — threshold boundary (`buy_px`
exactly one window above base) and an order enqueue/dequeue coincidence — which
I closed with `test_threshold_edges` and `test_order_storm`. That is why the
tests exist.

**10. Why cocotb and not UVM?**
HFT FPGA teams mostly use Python/cocotb or in-house SV benches; a Python model is
easy to keep bit-exact; and UVM needs a commercial simulator. I can discuss UVM
structure (agents, sequences, scoreboard, coverage) — I chose the tool that fit.

**11. How does this connect to clock-domain crossing?**
The MAC's RX clock is a transceiver clock. The core's AXI-Stream must cross from
it via an async FIFO with Gray-coded pointers and 2-FF synchronisers — the block
in my PTP timestamping project. Timestamps taken at the MAC boundary give true
wire-to-wire latency.

**12. What would you do for more than one message per 3 cycles?**
Pipeline the book. Hazards appear when consecutive messages touch the same order
or level: RAM write→read forwarding (or stall on conflict), and the occupancy
update must be visible to the next encode. I would formally prove the
level-quantity ↔ occupancy-bit invariant before trusting it.

**13. What are the weaknesses of this project?** *(say them first — it is credible)*
Simulated latency only until Vivado/board numbers exist; simplified protocol
(not real ITCH 5.0, no sequence-gap handling); fixed price window; direct-mapped
order table; trigger is a placeholder; no risk checks.
