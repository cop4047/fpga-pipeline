# ITCH 5.0 support — scope and design decisions

Status: **steps 1, 2 and 3 done** (RTL complete and verified in simulation; Vivado timing for the ITCH design
not yet measured). Step 1: Python model `model/itch50.py`, vector generator,
C++ port `baseline/cpu_itch50.cpp` (bit-exact, sanitizer-clean). Step 2: RTL framer
`rtl/mdp_itch_frame.sv` + `rtl/mdp_itch_unit.sv`, verified against the model (see
"Framer" below). Step 3: book, top level and full-pipeline regression (see "Book and pipeline" below). Step 4 remains: Vivado
timing and the README timing log. This replaces "ITCH-lite" (fixed 16-byte
messages) with the real Nasdaq TotalView-ITCH 5.0 message layouts carried in
MoldUDP64, the way the feed is actually delivered. Layouts below were checked
against Nasdaq's specifications (ITCH 5.0 and MoldUDP64).

## Transport

```
Ethernet 14 | IPv4 20 | UDP 8 | MoldUDP64 header 20 | message blocks ...
                                  session[10] seq[8] count[2]   (all big-endian)
message block = length[2] + message[length]     (blocks are packed back-to-back)
```

- MoldUDP64 payload starts at byte 42; the first message block at byte 62.
  62 mod 8 = 6, and every message after it starts at an arbitrary byte offset.
- `count = 0` is a heartbeat, `count = 0xFFFF` is end of session. Neither carries
  messages; both carry the next expected sequence number.
- Gap handling: the design tracks the expected sequence number. On a gap it
  raises a counter and flag (it cannot recover; a real system requests a
  retransmission out of band). Duplicates (seq below expected) are skipped.

## Messages handled by the book

All integers big-endian. Stock is 8 ASCII bytes, space padded. Price(4) is an
unsigned integer with 4 implied decimals ($12.3400 = 123400).

| Type | Len | Layout after the 11-byte common header (type[1] locate[2] tracking[2] timestamp[6]) | Effect |
|---|---|---|---|
| `A` Add | 36 | ref[8] side[1] shares[4] stock[8] price[4] | add order |
| `F` Add + MPID | 40 | as `A`, then attribution[4] | add order (attribution ignored) |
| `E` Executed | 31 | ref[8] shares[4] match[8] | reduce order by shares |
| `C` Executed with price | 36 | as `E`, printable[1] price[4] | reduce order (execution price ignored) |
| `X` Cancel | 23 | ref[8] shares[4] | reduce order by shares (partial cancel) |
| `D` Delete | 19 | ref[8] | remove order |
| `U` Replace | 35 | oldref[8] newref[8] shares[4] price[4] | remove old, add new on same side and symbol |

Every other message type (`S`, `R`, `H`, `P`, `Q`, `I`, ...) is parsed for its
length, skipped, and counted as `ignored`. It is not an error.

## Design decisions (and what they cost)

| Decision | Choice | Cost / limit |
|---|---|---|
| Symbols | Watchlist of `N_SYM` (4) configured 8-byte stock names; an Add whose stock is not on the list is dropped `NOT_WATCHED` (counted, not an error). | Real use needs more symbols; the watchlist compare is a CAM-like block that grows with `N_SYM`. |
| Order reference | 64-bit refs, direct-mapped table indexed by the low `ORD_AW` bits, **full 64-bit ref stored as a tag**. A lookup whose tag differs is `UNK_REF`; an Add into an occupied slot is `COLLISION` (new drop reason). | ITCH refs are mostly increasing, so low bits spread well, but collisions are possible and are dropped, not chained. State this limit. |
| Price window | 64 ticks from `cfg_base_px`, tick size a parameter (default 100 = $0.01). Price not on a tick, or outside the window: `BAD_PRICE`. | Fixed window as before. |
| Replace (`U`) | Looked up by old ref; side, symbol and remaining level come from the table; new price must be inside the window. | Two book updates per message, so 6–8 cycles. |
| Execute/Cancel above remaining | Clamped to remaining (as today). | Real feeds do not do this; it is a defensive rule and is listed as such. |
| Timestamps, locate, tracking, match numbers | Parsed, not used. | |
| Minimum block length | A block shorter than 12 bytes (the shortest real ITCH message) ends the packet and is counted as `trunc`. | Lets the framer assume at most one block starts per beat. A packet with tiny blocks is treated as malformed. |

## What will change in the numbers (be ready to report it)

The current 5-cycle tick-to-trade assumes a fixed 16-byte message aligned to two
beats. A real `A` message plus its 2-byte length is 38 bytes: the parser must see
about 5 beats of it before the decision is known, and the framer must cope with
messages starting at any byte offset and several messages per beat. **Expect the
cycle count to rise and the 200 MHz closure to need re-checking.** Whatever the
new numbers are, they replace the old ones in the README, with a new iteration
in the timing log.

## Plan, in order

1. **Model and vectors (software only).** `model/itch50.py`: layouts, MoldUDP64
   framer, reference book with the rules above, constrained-random generator
   (including gaps, duplicates, heartbeats, ignored types, replaces, collisions,
   bad prices). Port to the C++ baseline; bit-exact check as today.
2. **RTL framer.** Byte-stream message extractor for variable-length, unaligned
   blocks at 64 bits per cycle. This is the hard part; tested alone first.
3. **Book changes.** 64-bit tagged order table, watchlist match, cancel and
   replace paths, new drop reasons.
4. **Regression, mutation, lint, sweep.** New mutants for the new logic; rerun
   the timing sweep; update README and the timing log.
5. **Real data (optional).** Convert a real Nasdaq sample file (length-prefixed
   messages) into MoldUDP64 frames and replay it through model, C++ and RTL.
   Only claim "real ITCH data" if this step is done.

## Framer (step 2): what was built and how it was checked

`mdp_itch_frame` takes the 64-bit AXI-Stream (with `tkeep`), checks the headers, tracks
MoldUDP64 sequence numbers and hands over one complete ITCH message per cycle
(`m_valid`, `m_len`, `m_data` = first 40 bytes in network order, byte 0 at the top).

How it works:

- A small tracker follows the 2-byte length prefixes. It keeps one number, the distance
  to the end of the newest block (which is also the start of the next), reads each length
  with an 8:1 byte mux, and so finds every block boundary one beat at a time.
- Two `mdp_itch_unit` blocks take turns. Each rotates the 16-byte window of
  {previous beat, current beat} by the block's start lane, so message bytes land in fixed
  word positions as the beats arrive. No byte-by-byte insertion network is needed.
- A block is only reported when the tracker sees its last byte arrive. A truncated block
  therefore never completes and is dropped at the end of the frame.
- Sequence handling: a 64-bit expected-sequence register, a two-stage compare after beat 7,
  a count of leading duplicates to discard, and a gap/resync pulse.

Checks (all against `model/itch50.py`):

| Check | Result |
|---|---|
| Messages (length + first 40 bytes) and 7 counters, 3 directed + random tests | pass |
| Random traffic: 12,000 frames back to back with no idle cycles; 6,000 frames with heavy idle (30% of cycles) | pass |
| Mutation testing: 17 injected framer bugs | 16 killed; 1 equivalent (documented in `tb_itch/mutation_test.py`) |
| Verilator `-Wall` | clean |

Added latency: the message is presented **1 to 2 cycles after the beat that holds its last
byte**, depending on its start lane (1 cycle for start lanes 3, 4 and 5, otherwise 2). Part of
that is the realignment (a word completes one beat after the beat that starts it) and part is
the flush cycle after `tlast`. This will be reported in the full-pipeline latency once step 3
connects the framer to the book.

Not yet done: timing (Vivado) for the framer, and any claim about its Fmax or area.

## Book and pipeline (step 3): what was built and how it was checked

`rtl/mdp_itch_book.sv` applies the rules above to one framed message at a time; `rtl/mdp_itch_top.sv`
wires framer -> message FIFO (bypassed when empty) -> book -> order FIFO -> `mdp_order_tx`.
The older `mdp_top` (ITCH-lite) is untouched so v1/v2 stay reproducible.

Book pipeline (cycle 0 = the cycle the message is accepted):

| State | Cycle | Work |
|---|---|---|
| accept (IDLE or BT) | 0 | capture 36 bytes + length; read the order-table slot for `ref[9:0]` |
| D1 | 1 | decode type and length; side; 4-way 64-bit watchlist compare; `px - base`; select shares; order-table valid and 54-bit tag compare |
| D2 | 2 | `px - base` -> level (one-hot compare, exact multiples of the tick only); delta / remaining for execute, cancel, delete, replace; save the entry; unknown types and wrong lengths leave here |
| RD | 3 | the single accept/drop decision, in the model's priority order; issue the level-RAM read |
| WR | 4 | level RAM update, occupancy bit, order-table write (a replace runs RD/WR twice: remove old, add new) |
| BT | 5 | best bid / ask, trigger, order request and top-of-book tap; the next message may be accepted in this cycle |

Decisions made while building it:

- The order table stores `ref[63:10]` as a tag, so a lookup is exact on all 64 bits. Occupied slot with the same
  reference is `DUP_REF`, with a different one `COLLISION`; a failed lookup is `UNK_REF`.
- Level aggregate quantity is only ever observed as "empty or not" (occupancy bit), because no port exposes sizes.
  One consequence: swapping `+` and `-` in the level update (it stores the negated quantity) is an *equivalent* mutant.
- The trigger threshold in levels, `floor((buy_px - base) / TICK)`, comes from a 6-step binary search on registers
  instead of a divider. Configuration is static; it settles a few cycles after a change.
- `TICK` is a synthesis parameter (100 here). The price-to-level step compares `px - base` with each of the 64
  multiples of the tick, so it is exact and needs no divider; the cost is 64 small comparators.
- Because the book accepts a message every 5 cycles (Add) and a 38-byte Add arrives every 4.75 cycles at line rate,
  back-to-back Adds are close to the limit; a 35-message frame peaks at 2 queued messages. Short messages
  (an ignored 14-byte block arrives every 1.75 cycles and needs 3) can outrun it; the 64-entry FIFO absorbs
  bursts and overflow is counted, never silent (tested).

Results (simulation, Icarus + cocotb, bit-exact against `model/itch50.py`):

| Check | Result |
|---|---|
| Full-pipeline tests | 11 tests: directed trigger / drops / replace / width and boundary cases / base near 2^32 / duplicate watchlist names / threshold sweep / latency / burst and overload, plus constrained-random (3 seeds x 1,500 frames, about 24 k messages, 8 drop reasons all exercised) |
| Scoreboards | top-of-book events, drop reasons in order, exact bytes of every order frame, 12 counters |
| Mutation testing, book | 50 injected bugs: 49 killed, 1 equivalent (level quantity stored negated) |
| Mutation testing, framer | 17 bugs: 16 killed, 1 equivalent |
| Verilator `-Wall` | clean |
| Yosys xc7 estimate (whole ITCH design) | about 4,300 LUT, 3,000 FF, 3 x RAMB36 + 1 x RAMB18, 116 distributed-RAM cells (estimate only) |
| **Tick-to-trade, idle pipeline (simulated)** | **7 to 8 cycles = 44.8 to 51.2 ns at 156.25 MHz (35 to 40 ns at 200 MHz)**, depending on the byte lane where the Add message starts |

Where the cycles go (best case): the framer presents the message in the cycle after the last byte is captured
(+0 to +1), the book takes 6 cycles from accept to the order request, and the transmitter launches the first
beat one cycle after that. For comparison, ITCH-lite took 5 cycles. The 2 to 3 extra cycles are the price of a
real protocol (variable-length unaligned messages, 64-bit references with tag compare, a price-to-level
conversion that must be exact). Merging D1/D2/RD where timing allows is the obvious next optimisation; it is
deliberately not done yet because it needs Vivado feedback.

Not yet done: Vivado timing and utilisation for the ITCH design (run `syn/build.tcl`, then `syn/sweep.tcl`;
results go to `syn/out_itch`), step 5 (real Nasdaq sample data).
