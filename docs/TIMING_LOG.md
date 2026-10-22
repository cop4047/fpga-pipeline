# Vivado timing log

One row per RTL change that was run through Vivado (xc7k70tfbg676-2, out-of-context, post-route).
Each run's raw reports are in `syn/runs/<run>/` (`info.txt` records the git commit). Placement noise is
about +/-0.5 ns at failing targets, so only differences larger than that mean anything.

| Run | Change | git | 200 MHz | 222 MHz | 250 MHz | 286 MHz | Limiting path |
|---|---|---|---|---|---|---|---|
| 01-baseline-lutram | order table inferred as distributed RAM | (commit of the timestamp/CDC update) | +0.111 | -0.279 | -0.545 | -0.808 | index reg -> 144-load read address -> 1024:1 LUT mux; msg FIFO -> framer -> table address in one cycle |

How to add a run: `set RUN NN-short-name`, `source build.tcl`, then `syn\run_sweep_par.bat NN-short-name`.
