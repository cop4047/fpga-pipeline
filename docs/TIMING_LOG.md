# Vivado timing log

One row per RTL change that was run through Vivado (xc7k70tfbg676-2, out-of-context, post-route).
Each run's raw reports are in `syn/runs/<run>/` (`info.txt` records the git commit). Placement noise is
about +/-0.5 ns at failing targets, so only differences larger than that mean anything.

| Run | Change | git | 200 MHz | 222 MHz | 250 MHz | 286 MHz | Limiting path |
|---|---|---|---|---|---|---|---|
| 01-baseline-lutram | order table inferred as distributed RAM | e89e9e2 | +0.111 | -0.279 | -0.545 | -0.808 | index reg -> 144-load read address -> 1024:1 LUT mux; msg FIFO -> framer -> table address in one cycle |
| 02-bram-order-table (INVALID) | `ram_style = "block"` on the order table | 93a3062 | -0.335 | -0.215 | -0.362 | -0.691 | Built with `ASYNC_CORE=1` left over in the Tcl console, so this is a two-clock build with the core clock fixed at 4.0 ns; the four columns are the network-clock targets and mostly show placement noise on the core-clock paths. Not comparable with run 01. The attribute was also ignored: 0 block RAMs, 4,448 LUTRAMs in the book. |
| 03-single-write-port | one write port per memory (init clear merged into the normal write); block RAM now used | 5b323ef + the book change (committed afterwards) | +0.196 | -0.406 | -0.919 | -1.336 | One clock. Resources fell a lot (book 7,308 -> 1,701 LUTs, 4,448 -> 0 LUTRAMs, whole design 3,985 LUTs) but Fmax did not improve: still passes 200 MHz, fails 222 MHz. Limit moved to level RAM (block RAM clock-to-out 1.8 ns) -> 32-bit add/sub carry chain (7-8 CARRY4) -> `new_q != 0` -> occupancy mask (128-load enable). At 156.25 MHz: WNS +0.753 ns. |
| 04-empty-test-by-compare | empty-level test by comparing level with +/-size instead of the sum, so the mask no longer waits for the carry chain | (next commit) | pending | pending | pending | pending | pending |

Noise reference (same two-clock design, core clock 4.0 ns, only the network target changed): worst core-clock slack -0.145, -0.215, -0.335, -0.362, -0.691 ns. Treat differences under about 0.5 ns as noise.

How to add a run: `set RUN NN-short-name` and set `ASYNC_CORE` explicitly (0 for one clock, 1 for two), `source build.tcl`, then `syn\run_sweep_par.bat NN-short-name`.
