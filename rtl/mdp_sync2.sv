// mdp_sync2 -- two-flop synchroniser for signals crossing into `clk`.
//
// ONLY use it on (a) single-bit signals or (b) Gray-coded buses, where at most one
// bit changes per source clock, so whichever way the first flop resolves a bit that
// changed inside its setup/hold aperture the value seen is either the old or the
// new one.  A multi-bit binary bus through this module is a bug.
//
// Simulation model (define MDP_CDC_SIM): plain RTL simulation never produces a
// metastable flop, so it cannot tell a Gray pointer from a binary one.  With the
// define, any bit of `d` that changed less than APERTURE_PS before the sampling
// edge resolves to old or new at random.  Gray pointers still work (one bit moves
// at a time); a binary pointer does not.  Synthesis never sees the define.
module mdp_sync2 #(
    parameter int W = 1
) (
    input  logic         clk,
    input  logic [W-1:0] d,
    output logic [W-1:0] q
);
`ifdef MDP_CDC_SIM
    timeunit 1ps;
    timeprecision 1ps;
    localparam real APERTURE_PS = 1000.0;

    logic [W-1:0] s1, s2;
    logic [W-1:0] d_cur, d_prev;          // d, and d before its most recent change
    real          t_chg;
    int unsigned  n_hit;                  // bits that resolved randomly (visible to the testbench)
    initial begin
        d_cur = '0; d_prev = '0; t_chg = -1.0e12; n_hit = 0; s1 = '0; s2 = '0;
    end
    always @(d) begin
        d_prev = d_cur;
        d_cur  = d;
        t_chg  = $realtime;
    end
    always @(posedge clk) begin
        for (int i = 0; i < W; i++) begin
            if (($realtime - t_chg) < APERTURE_PS && d[i] !== d_prev[i] && d_prev[i] !== 1'bx) begin
                n_hit = n_hit + 1;
                s1[i] <= ($urandom_range(1, 0) != 0) ? d[i] : d_prev[i];
            end else begin
                s1[i] <= d[i];
            end
        end
        s2 <= s1;
    end
    assign q = s2;
`else
    (* ASYNC_REG = "TRUE" *) logic [W-1:0] s1;
    (* ASYNC_REG = "TRUE" *) logic [W-1:0] s2;
    always_ff @(posedge clk) begin
        s1 <= d;
        s2 <= s1;
    end
    assign q = s2;
`endif
endmodule
