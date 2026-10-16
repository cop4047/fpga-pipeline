// mdp_msg_asm -- pairs two realigned payload words into one ITCH-lite message.
//
// Message (16 bytes, network byte order):
//   word0: [0] type  [1] side  [2:3] symbol  [4:7] order ref
//   word1: [0:3] price (ticks)  [4:7] shares
// The frame's message count bounds how many messages are emitted, so Ethernet
// padding and truncated trailers are ignored.  No end-of-frame input is needed:
// the first word of the next frame (w_first) reloads all state.
//
// m_data = {type[7:0], side[7:0], sym[15:0], ref[31:0], price[31:0], shares[31:0]}
module mdp_msg_asm (
    input  logic         clk,
    input  logic         rst,
    input  logic         w_valid,
    input  logic         w_first,
    input  logic [63:0]  w_data,
    input  logic [15:0]  w_count,
    output logic         m_valid,
    output logic [127:0] m_data
);
    function automatic logic [31:0] bswap32(input logic [31:0] x);
        bswap32 = {x[7:0], x[15:8], x[23:16], x[31:24]};
    endfunction

    logic [63:0] w0;
    logic        have_w0;
    logic [15:0] remaining;

    wire [15:0] rem_eff  = w_first ? w_count : remaining;
    wire        have_eff = w_first ? 1'b0    : have_w0;

    always_ff @(posedge clk) begin
        m_valid <= 1'b0;
        if (rst) begin
            have_w0   <= 1'b0;
            remaining <= 16'd0;
            w0        <= 64'd0;
            m_data    <= 128'd0;
        end else if (w_valid) begin
            remaining <= rem_eff;
            have_w0   <= have_eff;
            if (rem_eff != 16'd0) begin
                if (!have_eff) begin
                    w0      <= w_data;
                    have_w0 <= 1'b1;
                end else begin
                    m_valid   <= 1'b1;
                    m_data    <= {w0[7:0], w0[15:8], w0[23:16], w0[31:24],
                                  bswap32(w0[63:32]),
                                  bswap32(w_data[31:0]),
                                  bswap32(w_data[63:32])};
                    remaining <= rem_eff - 16'd1;
                    have_w0   <= 1'b0;
                end
            end
        end
    end
endmodule
