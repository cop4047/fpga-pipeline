# mdpipe — 10G market-data → order pipeline: specification

Status: **v2** — RTL complete, verified in simulation (cocotb + Python golden
model, 10 tests, mutation-tested). Vivado timing/area numbers: *not yet measured*
(see §7).

## 1. Purpose and scope

A feed-handler-to-order-gateway datapath of the kind found in low-latency
trading FPGAs: parse a UDP market-data frame, maintain a price-level order book
in hardware, decide to trade with a simple trigger, and emit an order frame —
all between two 64-bit AXI-Streams, in single-digit clock cycles.

**In scope:** Ethernet/IPv4/UDP screening, payload realignment, message decode,
order book, trigger, order-frame generation, statistics, a bit-exact
reference model, and a verification environment.

**Out of scope (deliberately):**

| Not included | Why / what a real design does |
|---|---|
| 10G MAC / PCS / PHY | Reuse a proven core (e.g. Alex Forencich's `verilog-ethernet`); not the interesting part to rewrite. See §9. |
| Real Nasdaq ITCH 5.0, MoldUDP64 | The message set here is **"ITCH-lite"**: fixed 16-byte Add/Delete/Execute. Real ITCH is variable-length with sequence/gap handling. Do not describe this as ITCH 5.0 support. |
| Matching engine, risk checks, alpha model | The trigger (§4.4) is a placeholder where a strategy plugs in. |
| IP/UDP checksum verification, fragmentation, VLANs | Not needed for a dedicated feed; header fields are checked only as listed in §4.1. |

## 2. Architecture

```
  s_axis (64b, no back-pressure)
     |
     v
 mdp_hdr_parse    check eth/ip/udp fields, realign payload by 4 bytes
     | aligned 64b words + message count
     v
 mdp_msg_asm      2 words -> one 128-bit message record
     | m_valid / m_data
     v
 msg FIFO (64)    bypassed when empty and the book is ready
     |
     v
 mdp_book         validate -> order table -> level RAM -> occupancy mask
     |            -> best bid/ask -> trigger          (taps: top-of-book, drops)
     | order request {sym, level, qty}
     v
 order FIFO (8)   bypassed when empty and the transmitter is idle
     |
     v
 mdp_order_tx     build 60-byte frame, stream 8 beats
     |
     v
  m_axis (64b)
```

One clock (target **156.25 MHz**, 6.4 ns; 64 bits x 156.25 MHz = 10 Gb/s).
Parameters of `mdp_top`: `SYM_W=2` (4 symbols), `LVL_W=6` (64-tick price
window), `ORD_AW=10` (1024 live order refs), `MSGQ_AW=6`, `ORDQ_AW=3`.

## 3. Interfaces and formats

### 3.1 Market-data input (AXI-Stream, 64-bit, byte 0 = `tdata[7:0]`)

`tvalid`, `tlast`, `tdata`. No `tready` (a feed cannot be stalled) and no
`tkeep` (payload length is given by the message count). FCS is assumed stripped
by the MAC.

| Byte offset | Field | Checked |
|---|---|---|
| 12–13 | Ethertype `0x0800` | yes |
| 14 | IPv4 version/IHL `0x45` | yes |
| 23 | IP protocol `17` (UDP) | yes |
| 36–37 | UDP destination port | yes, == `cfg_md_dport` |
| 42–43 | Message count `n` (big-endian) | used |
| 44 + 16k | Message k | — |

Frames shorter than 7 beats (49+ bytes) are counted bad. The payload starts at
byte 44, i.e. **4 bytes into a beat**, so `mdp_hdr_parse` emits
`{cur[31:0], prev[63:32]}` from beat 6 onward (a 4-byte realignment). All header
checks resolve by beat 4, before the first payload word is emitted, so they add
no latency.

### 3.2 ITCH-lite message (16 bytes, network byte order)

| Byte | Field |
|---|---|
| 0 | type: `A` Add (0x41), `D` Delete (0x44), `E` Execute (0x45) |
| 1 | side: `B` (0x42) or `S` (0x53) — used by Add only |
| 2–3 | symbol index (Add only) |
| 4–7 | order reference |
| 8–11 | price in ticks (Add only) |
| 12–15 | shares (Add, Execute) |

### 3.3 Order output (60-byte frame = Ethernet minimum without FCS)

Ethernet(14) | IPv4(20, static, checksum from `cfg_ip_csum`) | UDP(8, checksum 0) |
payload(16): `'O' 'B' sym[2] client_id[4] price[4] qty[4]` | 2 pad bytes.
Eight beats; the last has `tkeep = 0x0F`. `client_id` starts at 1 and increments
per order.

### 3.4 Configuration inputs (static while running)

`cfg_md_dport`, `cfg_base_px`, `cfg_buy_px`, `cfg_qty`, `cfg_en`, and the
order-gateway framing `cfg_dst_mac`, `cfg_src_mac`, `cfg_src_ip`,
`cfg_dst_ip`, `cfg_sport`, `cfg_odport`, `cfg_ip_csum`. After reset the book
sweeps its RAMs for 2^`ORD_AW` clocks; `o_ready` goes high when done. Traffic
sent before then is queued or counted as overflow.

### 3.5 Observability

Taps: `tap_tob_*` (top-of-book after every accepted update), `tap_drop_*`
(rejected message + reason). Counters: `cnt_frm_good`, `cnt_frm_bad`, `cnt_msg`,
`cnt_drop`, `cnt_ord`, `cnt_ovf` (message- or order-FIFO overflow).

## 4. Behavioural rules

These are implemented identically in `rtl/mdp_book.sv` and
`model/mdp_model.py`; the testbench enforces that they stay in lock-step.

### 4.1 Frame acceptance
A frame is **good** iff ethertype, IHL, protocol and UDP port match and it is at
least 7 beats long. Otherwise `cnt_frm_bad`. For a good frame with count `n`
and `W = beats − 6` aligned words, `min(n, ⌊W/2⌋)` complete messages are
processed; trailing padding and truncated messages are ignored.

### 4.2 Message validation — precedence is part of the spec

| # | Condition (first match wins) | Reason code |
|---|---|---|
| 1 | type not in {A,D,E}, or Add with side not in {B,S} | 5 `BAD_TYPE` |
| 2 | order ref ≥ 2^`ORD_AW` | 6 `BAD_REF` |
| 3 | Add: symbol ≥ 4 | 1 `BAD_SYM` |
| 4 | Add: price outside `[base, base+64)` | 2 `BAD_PRICE` |
| 5 | Add or Execute: shares = 0 | 7 `BAD_QTY` |
| 6 | Add: ref already live | 3 `DUP_REF` |
| 7 | Delete/Execute: ref not live | 4 `UNK_REF` |

### 4.3 Book update
* **Add**: insert order into the table; add shares to its `{symbol, side, level}`;
  set the occupancy bit.
* **Delete**: remove the order; subtract its remaining shares from the level.
* **Execute**: `delta = shares ≥ remaining ? remaining : shares` (over-execution
  is clamped); subtract `delta`; remove the order when nothing remains.
* The occupancy bit follows `level quantity ≠ 0`. There is no matching: the book
  may be crossed.

### 4.4 Trigger
After every accepted update of symbol *s*: `cond = en ∧ ask_valid(s) ∧
best_ask(s) ≤ buy_px`. An order (BUY at the best ask, `qty`) is emitted on the
**rising edge** of `cond` per symbol (`fired[s]` arms/disarms); when `cond`
falls, the symbol re-arms. The compare is done in the level domain
(`ask_level ≤ buy_px − base_px`, with the cases `buy_px < base` → never and
`buy_px − base ≥ 64` → always handled explicitly).
Consequence: only an *Add* can fire a new order (removing liquidity can only
raise the best ask).

## 5. Performance

### 5.1 Definition
**Tick-to-trade** = clock edge that captures the *last* beat of the market-data
frame → clock edge on which the *first* beat of the order frame is launched at
`m_axis`, idle pipeline, single-message frame. This is **core-logic latency
between the two AXI-Streams**, measured in RTL simulation. It excludes
MAC/PCS/PHY and wire serialisation, and is not a hardware measurement.

### 5.2 Measured (simulation, `tb/results.json`)

| Stage (cycles after last input beat captured) | v1 | v2 |
|---|---|---|
| header parse → aligned word registered | 0 | 0 |
| message assembled | 1 | 1 |
| book accepts message (cycle in which it is accepted starts after this edge) | 2 | 1 |
| order request / top-of-book tap launched | 8 | 4 |
| first order beat launched | **10** | **5** |
| ns @ 156.25 MHz | 64.0 | **32.0** |

v1 → v2 changes: bypass both FIFOs when empty (−2), validate at accept instead
of a separate decode state (−1), Add fast-path reading the level RAM at accept
time (−1), trigger in the level domain so priority-encode and fire decision
share a state (−1). Git tags `v1` and `v2` hold both designs.

### 5.3 Throughput
Book occupancy per message: **3 cycles** (Add) / **4 cycles** (Delete, Execute)
→ ≈ 52 M / 39 M messages/s at 156.25 MHz. A maximum-size frame delivers one
message per 2 cycles, so bursts queue: the MTU test (91 messages) peaks at 30 of
64 message-FIFO entries. Order transmission takes 8 cycles per order.

### 5.4 Limits (by design, all *counted*, never silent)
* No back-pressure: a sustained message rate above the book's capacity, or order
  rate above 1 per 8 cycles, overflows the FIFOs; `cnt_ovf` counts it
  (`test_overload_is_counted`).
* Prices only inside a 64-tick window starting at `cfg_base_px`.
* Order refs must be < 1024 (direct-mapped table, no hashing).
* Statically configured: re-centering the window requires reset.

## 6. Verification

Environment: cocotb 2.x on Icarus Verilog; Python golden model
(`model/mdp_model.py`) fed the same frames; scoreboards on the top-of-book tap,
drop tap, order AXI-Stream bytes and statistics counters.

| Test | What it pins down |
|---|---|
| `test_reset_state` | post-reset idle state, counters zero |
| `test_directed_trigger` | fire, quiet while armed-off, re-arm, per-symbol independence, clamped execute, client-id sequence |
| `test_directed_errors` | every drop reason, precedence, every header reject, count > / < payload, runt frames |
| `test_threshold_edges` | threshold below / at / above the price window (found by mutation testing) |
| `test_back_to_back_min_frames` | zero-gap single-message frames |
| `test_order_storm` | order bursts faster than TX drain; bypass/enqueue hand-off (found by mutation testing) |
| `test_random` | 4 seeds x 1200 frames, random gaps, 4 % bad headers, 8 % bad messages; functional-coverage assertions (all 7 reasons, all symbols fire, ≥ 50 orders) |
| `test_latency` | tick-to-trade + per-stage first-seen times, regression-checked |
| `test_mtu_frame` | 91-message frame absorbed by the FIFO |
| `test_overload_is_counted` | sustained overload is detected, design recovers |

Totals in one regression: ≈ 11.6 k messages, ≈ 4.8 k frames, ≈ 110 random orders,
all compared bit-exactly. **Mutation testing** (`make mutation`, report in
`docs/mutation_report.txt`) injects 14 single-point bugs across parser,
assembler, book, trigger and both bypasses: 14/14 caught. Two blind spots in the
first version of the tests (threshold boundary, coincident enqueue/dequeue) were
found this way and closed with the tests above.

*Why cocotb and not UVM:* HFT FPGA teams overwhelmingly use Python/cocotb or
in-house SV frameworks; the free toolchain here has no UVM-capable simulator;
a Python model is far easier to keep bit-exact than an SV one. Adding a UVM-style
testbench for a single block is possible but would be decoration.

*Not done:* formal proofs (SymbiYosys) of FIFO/book invariants; hardware-in-the-loop.

## 7. Implementation results

### 7.1 Open-source estimate (yosys `synth_xilinx -family xc7`, `make yosys`)
Resource **estimate only** — yosys' mapping differs from Vivado's, and yosys
reports no timing.

| LUT (2-6 input) | FF | LUTRAM (RAM32M/RAM64M) | BRAM (RAMB18) | CARRY4 |
|---|---|---|---|---|
| ≈ 2170 | ≈ 1690 | 14 / 43 | 4 | 152 |

### 7.2 Vivado (to be filled in — run `make vivado` and `make sweep`)

| Part | Target | WNS (ns) | Fmax est. | LUT | FF | BRAM | Critical path |
|---|---|---|---|---|---|---|---|
| xc7k70t-2 | 6.4 ns | — | — | — | — | — | — |
| | 5.0 ns | — | — | | | | |
| | 4.0 ns | — | — | | | | |

Expected critical-path candidates to look at first: the `BT` state (occupancy
mask → 64-way priority encode → level compare → `fired`/order register) and the
Add path's level-RAM read → 32-bit add → write. Record one iteration
(e.g. "WNS −0.4 → +0.1 ns after X") — that is the story interviewers probe.

## 8. Optimisation roadmap (ordered by value)

1. **Close timing and report Fmax** (§7.2); pipeline `BT` only if it limits.
2. **Speculative / cut-through transmit**: start streaming the constant 42-byte
   header before the book has decided, and poison the frame (e.g. bad FCS) if
   the trigger does not fire — needs MAC support. The first order byte can then
   leave before the decision completes.
3. **Pipeline the book** (accept a message every cycle) with RAM write→read
   forwarding for back-to-back updates of the same level/order.
4. **Per-symbol trigger parameters / imbalance (quantity-based) trigger.**
5. **Tick-to-trade measured in hardware** with the PTP timestamp core
   (timestamp at ingress SOF and egress SOF; histogram in BRAM).
6. **Formal**: SymbiYosys on the FIFOs, order-table/level-RAM consistency, and
   the "exactly one order per rising edge" property.

## 9. Integration notes

* **MAC/PCS:** take 64-bit AXI-Stream from a 10G MAC (Forencich `eth_mac_10g`,
  XGMII/PCS 64b/66b). Its RX clock is a transceiver clock: cross into the core
  clock with the Gray-coded async FIFO from the PTP project, or run the core on
  the same clock if the MAC and core share `clk`.
* **Timestamps:** the PTP/timestamp core can tag frames at the MAC boundary to
  measure true wire-to-wire latency.
* **Board constraints:** `syn/board_template.xdc`.

## 10. Honest caveats
* Latency numbers are RTL-simulation clock cycles, not hardware measurements.
* The simplified protocol and fixed-window book are teaching-grade, not
  production-grade; every simplification is listed above.
* `syn/*.tcl` were written without a Vivado install to test against.
