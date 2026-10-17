# CPU baseline (C++)

Two programs: `cpu_baseline` (the original ITCH-lite format) and `cpu_itch50` (real
ITCH 5.0 inside MoldUDP64, see `../docs/ITCH50.md`). Each is checked bit-for-bit
against its Python golden model (`../model/mdp_model.py`, `../model/itch50.py`).

The same pipeline as the RTL, written as fast, plain C++ for one CPU core, so the
FPGA numbers have an honest comparison.

```
make check           # ITCH-lite: C++ output must equal the Python golden model, bit for bit
make bench PIN=2     # pin to core 2 (Linux/WSL); run on an idle machine
make itch-check      # ITCH 5.0 / MoldUDP64, same check
make itch-bench PIN=2
```

`make check` compares top-of-book events, drop reasons, the exact 60-byte order
frames and the counters against `model/mdp_model.py` on 20,000 random frames.
A deliberately broken copy (`<=` changed to `<` in the trigger) fails the check,
so the check can fail.

## What the benchmark measures, and what it does not

Measured: time from "a whole frame is in memory" to "the order frame has been
built in memory", one core, warm caches, no allocation or I/O in the hot path.

Not measured (and a real CPU deployment pays for all of these): NIC DMA, PCIe,
the kernel or a kernel-bypass stack, interrupt or polling cost, the transmit
path. The FPGA figure (5 cycles) likewise excludes MAC/PHY, so both are
"core compute only". Do not present either as wire-to-wire.

Other caveats to state when quoting numbers:
- The workload is the constrained-random generator, not a real feed: about a
  third of messages are deliberately invalid, and frames hold 1-4 messages.
- Latencies are per frame, so a frame's figure includes every message before the
  trigger in that frame.
- Numbers depend on the CPU, frequency scaling and what else is running. Quote
  the machine, the compiler flags and `--pin`; run it a few times.

## Honest reading

On core compute alone, a well-written CPU path is within a small factor of the
FPGA core (tens of nanoseconds versus tens of nanoseconds). The FPGA's real
advantage is determinism and the removal of the NIC/PCIe/OS path, which this
program deliberately does not measure. Measuring that needs hardware.
