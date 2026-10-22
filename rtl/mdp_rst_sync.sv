// mdp_rst_sync -- asynchronous assert, synchronous release.
// Each clock domain gets its own copy so its synchronous-reset logic leaves reset
// cleanly on that domain's clock.
module mdp_rst_sync (
    input  logic clk,
    input  logic arst,        // active high, may be asynchronous to clk
    output logic rst_o
);
    (* ASYNC_REG = "TRUE" *) logic [1:0] q;
    always_ff @(posedge clk or posedge arst) begin
        if (arst) q <= 2'b11;
        else      q <= {q[0], 1'b0};
    end
    assign rst_o = q[1];
endmodule
