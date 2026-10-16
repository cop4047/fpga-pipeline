// mdp_fifo -- small synchronous first-word-fall-through FIFO.
// Asynchronous read (maps to LUTRAM on Xilinx).  A write while full is
// dropped and reported on `ovf` -- the pipeline never back-pressures.
module mdp_fifo #(
    parameter int W  = 128,
    parameter int AW = 5
) (
    input  logic          clk,
    input  logic          rst,
    input  logic          wr_en,
    input  logic [W-1:0]  wr_data,
    input  logic          rd_en,
    output logic [W-1:0]  rd_data,
    output logic          empty,
    output logic [AW:0]   count,
    output logic          ovf
);
    logic [W-1:0] mem [0:(1<<AW)-1];
    logic [AW:0]  wp, rp;

    assign count   = wp - rp;
    assign empty   = (wp == rp);
    assign rd_data = mem[rp[AW-1:0]];

    wire full = (count == (AW+1)'(1 << AW));

    always_ff @(posedge clk) begin
        ovf <= 1'b0;
        if (rst) begin
            wp <= '0;
            rp <= '0;
        end else begin
            if (wr_en) begin
                if (full) begin
                    ovf <= 1'b1;
                end else begin
                    mem[wp[AW-1:0]] <= wr_data;
                    wp <= wp + 1'b1;
                end
            end
            if (rd_en && !empty) rp <= rp + 1'b1;
        end
    end
endmodule
