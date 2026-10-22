// mdp_lat_stats -- tick-to-trade latency statistics from hardware timestamps.
// One `valid` pulse per order carries lat = egress timestamp - ingress timestamp (modulo
// 2^W, so a wrapped clock still gives the right difference).  Keeps last / min / max /
// sum / count; mean = sum / count is left to software.  Units: the timestamp's LSB.
module mdp_lat_stats #(
    parameter int W = 40
) (
    input  logic          clk,
    input  logic          rst,
    input  logic          clear,           // restart the statistics (e.g. after warm-up)
    input  logic          valid,
    input  logic [W-1:0]  lat,
    output logic          o_valid,         // registered copy of the event (per-order stream)
    output logic [W-1:0]  o_lat,
    output logic [W-1:0]  s_last,
    output logic [W-1:0]  s_min,
    output logic [W-1:0]  s_max,
    output logic [63:0]   s_sum,
    output logic [31:0]   s_cnt
);
    always_ff @(posedge clk) begin
        o_valid <= 1'b0;
        if (rst || clear) begin
            s_last <= '0;
            s_min  <= '1;
            s_max  <= '0;
            s_sum  <= '0;
            s_cnt  <= '0;
            if (rst) o_lat <= '0;
        end else if (valid) begin
            o_valid <= 1'b1;
            o_lat   <= lat;
            s_last  <= lat;
            if (lat < s_min) s_min <= lat;
            if (lat > s_max) s_max <= lat;
            s_sum   <= s_sum + 64'(lat);
            s_cnt   <= s_cnt + 32'd1;
        end
    end
endmodule
