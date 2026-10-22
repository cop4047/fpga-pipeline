// mdp_async_fifo -- dual-clock first-word-fall-through FIFO, Gray-coded pointers.
//
//   write side (wclk):  binary + Gray write pointer; the Gray read pointer arrives
//                       through a 2-flop synchroniser; `wfull` is registered from the
//                       NEXT pointer values (Cummings, SNUG 2002).
//   read side  (rclk):  mirror image; `rempty` registered the same way.
// Pointers are AW+1 bits (the extra bit tells full from empty).  Only Gray-coded
// pointers cross clocks, so only one bit changes per transfer and the 2-flop
// synchroniser is safe.  The memory is written in wclk and read asynchronously in
// rclk (LUTRAM on Xilinx): a word is only read after its write pointer has crossed.
//
// Like the single-clock mdp_fifo, a write while full is dropped and counted on
// `wovf` instead of back-pressuring (the market-data path has no stall).
//
// Reset: wrst / rrst must be asserted TOGETHER (hold both for a few cycles of the
// slower clock); resetting one side alone corrupts the pointers.
module mdp_async_fifo #(
    parameter int W  = 64,
    parameter int AW = 4                     // depth 2**AW, AW >= 2
) (
    input  logic          wclk,
    input  logic          wrst,
    input  logic          wr_en,
    input  logic [W-1:0]  wr_data,
    output logic          wfull,
    output logic          wovf,              // pulse: a write was dropped
    output logic [AW:0]   wcount,            // write-side occupancy (never under-reports)
    input  logic          rclk,
    input  logic          rrst,
    input  logic          rd_en,
    output logic [W-1:0]  rd_data,           // valid while !rempty (first word falls through)
    output logic          rempty,
    output logic [AW:0]   rcount             // read-side occupancy (never over-reports)
);
    if (AW < 2) begin : g_chk
        $error("mdp_async_fifo: AW must be >= 2");
    end

    function automatic logic [AW:0] b2g(input logic [AW:0] b);
        b2g = (b >> 1) ^ b;
    endfunction
    function automatic logic [AW:0] g2b(input logic [AW:0] g);
        g2b[AW] = g[AW];
        for (int i = AW - 1; i >= 0; i--) g2b[i] = g2b[i+1] ^ g[i];
    endfunction

    logic [W-1:0] mem [0:(1<<AW)-1];
    logic [AW:0]  rbin, rgray_r;             // read side pointers (declared first: used by both halves)
    logic [AW:0]  wgray_r;                   // write pointer, in the rclk domain

    // ---------------------------------------------------------------- write side
    logic [AW:0] wbin, wgray;
    logic [AW:0] rgray_w;                    // read pointer, in the wclk domain
    wire         do_wr   = wr_en && !wfull;
    wire  [AW:0] wbin_n  = wbin + (AW+1)'(do_wr);
    wire  [AW:0] wgray_n = b2g(wbin_n);

    mdp_sync2 #(.W(AW+1)) u_r2w (.clk(wclk), .d(rgray_r), .q(rgray_w));

    always_ff @(posedge wclk) begin
        wovf <= 1'b0;
        if (wrst) begin
            wbin  <= '0;
            wgray <= '0;
            wfull <= 1'b0;
        end else begin
            wbin  <= wbin_n;
            wgray <= wgray_n;
            wfull <= (wgray_n == {~rgray_w[AW:AW-1], rgray_w[AW-2:0]});
            if (wr_en && wfull) wovf <= 1'b1;
        end
    end
    always_ff @(posedge wclk) begin
        if (do_wr) mem[wbin[AW-1:0]] <= wr_data;
    end
    assign wcount = wbin - g2b(rgray_w);

    // ----------------------------------------------------------------- read side
    wire         do_rd   = rd_en && !rempty;
    wire  [AW:0] rbin_n  = rbin + (AW+1)'(do_rd);
    wire  [AW:0] rgray_n = b2g(rbin_n);

    mdp_sync2 #(.W(AW+1)) u_w2r (.clk(rclk), .d(wgray), .q(wgray_r));

    always_ff @(posedge rclk) begin
        if (rrst) begin
            rbin    <= '0;
            rgray_r <= '0;
            rempty  <= 1'b1;
        end else begin
            rbin    <= rbin_n;
            rgray_r <= rgray_n;
            rempty  <= (rgray_n == wgray_r);
        end
    end
    assign rd_data = mem[rbin[AW-1:0]];
    assign rcount  = g2b(wgray_r) - rbin;
endmodule
