// mdp_ptp_rtc -- free-running hardware real-time clock (the timestamp source of a
// PTP / IEEE 1588 hardware-timestamping unit).
//
// Time is a 64-bit fixed-point count of nanoseconds, 32.32 (32 integer bits of ns,
// 32 fractional bits), advanced by an increment every clock:
//     inc_eff = cfg_inc + sign_extend(cfg_trim)          (registered, 1 cycle)
//     time   <= set_en ? set_time : time + inc_eff
// For a 156.25 MHz clock cfg_inc = round(6.4 * 2^32) = 0x6_6666_6666; the 2^-32 ns
// resolution of the increment is 0.036 ppb of the clock rate, so a frequency servo
// can trim the rate in steps far finer than a crystal's drift (cfg_trim) and step
// the phase (set_en/set_time) -- the two controls a PTP slave's servo uses.
// The 32-bit integer part wraps every 4.29 s; PTP's 48-bit seconds field and the
// 1e9 wrap are NOT implemented (the design only needs differences between two
// timestamps taken from this one clock).
//
// All inputs are in the clock domain `clk`.
module mdp_ptp_rtc #(
    parameter int INC_W  = 40,                     // 8.32 fixed point
    parameter int TIME_W = 64                      // 32.32 fixed point
) (
    input  logic                     clk,
    input  logic                     rst,
    input  logic [INC_W-1:0]         cfg_inc,      // nominal increment per clock (ns, 8.32)
    input  logic signed [31:0]       cfg_trim,     // signed rate trim added to the increment (2^-32 ns per clock)
    input  logic                     set_en,       // load set_time at this clock edge
    input  logic [TIME_W-1:0]        set_time,
    output logic [TIME_W-1:0]        time_q
);
    logic [INC_W-1:0] inc_q;
    always_ff @(posedge clk) begin
        if (rst) begin
            inc_q  <= '0;
            time_q <= '0;
        end else begin
            inc_q  <= cfg_inc + INC_W'(cfg_trim);                 // sign-extended by the signed cast
            time_q <= set_en ? set_time : time_q + TIME_W'(inc_q);
        end
    end
endmodule
