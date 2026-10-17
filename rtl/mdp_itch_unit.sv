// mdp_itch_unit -- realigns ONE message block into fixed word positions.
//
// A block's body starts at an arbitrary byte lane of a beat.  With the previous
// beat (prv) and the current beat (cur) in hand, rotating the 16-byte window
// right by r lanes gives the next 8 body bytes aligned to a word boundary.  The
// unit therefore produces one aligned word per beat, writes it at a FIXED
// position in `msg` (word k -> msg[319-64k -: 64], so message byte 0 is always
// msg[319:312]), and counts words down.  Two units alternate between
// consecutive blocks so one can finish a block while the other starts the next.
//
// The unit never decides that a block is complete by itself: the parent's
// block tracker raises `end_ev` in the beat that holds the block's last byte,
// and only if that byte really arrived.  A truncated block therefore never
// finishes; the parent clears it at the end of the frame.
module mdp_itch_unit (
    input  logic         clk,
    input  logic         rst,
    input  logic         tick,        // a new {prv, cur} beat pair is valid this cycle
    input  logic [63:0]  prv,
    input  logic [63:0]  cur,
    input  logic         load,        // start a block (tick-aligned, unit must be idle)
    input  logic [2:0]   ld_r,        // lane where the body starts
    input  logic         ld_skip1,    // body starts in the NEXT beat: the first tick is a skip
    input  logic [15:0]  ld_len,      // body length in bytes
    input  logic [15:0]  ld_blk,      // index of this block within the packet
    input  logic         end_ev,      // last byte of this unit's block is in the current beat
    input  logic         clear,       // frame over: abandon any unfinished block
    input  logic [16:0]  skip,        // blocks with index < skip are retransmitted duplicates
    output logic         done_msg,    // pulse: block complete, `msg` / `o_len` hold it
    output logic         done_dup,    // pulse: block complete but a duplicate (discard)
    output logic [15:0]  o_len,
    output logic [319:0] msg
);
    logic        active, skip1, end_seen;
    logic [2:0]  r, widx;
    logic [13:0] wleft;
    logic [15:0] blk;

    wire [127:0] win  = {cur, prv};
    wire [63:0]  sh   = 64'(win >> {r, 3'b000});
    // body byte 0 sits in lane 0 (LSB); store words big-endian so message byte 0
    // ends up at the top of msg (network order)
    wire [63:0]  word = {sh[7:0], sh[15:8], sh[23:16], sh[31:24],
                         sh[39:32], sh[47:40], sh[55:48], sh[63:56]};
    wire         last = (wleft == 14'd1);
    wire         fin  = last && (end_seen || end_ev);

    always_ff @(posedge clk) begin
        done_msg <= 1'b0;
        done_dup <= 1'b0;
        if (rst) begin
            active   <= 1'b0;
            skip1    <= 1'b0;
            end_seen <= 1'b0;
            r        <= 3'd0;
            widx     <= 3'd0;
            wleft    <= 14'd0;
            blk      <= 16'd0;
            o_len    <= 16'd0;
            msg      <= 320'd0;
        end else begin
            if (end_ev && active) end_seen <= 1'b1;
            if (tick && active) begin
                if (skip1) begin
                    skip1 <= 1'b0;
                end else begin
                    if (widx == 3'd0) msg[319:256] <= word;
                    if (widx == 3'd1) msg[255:192] <= word;
                    if (widx == 3'd2) msg[191:128] <= word;
                    if (widx == 3'd3) msg[127:64]  <= word;
                    if (widx == 3'd4) msg[63:0]    <= word;
                    if (widx != 3'd7) widx <= widx + 3'd1;
                    if (!last) wleft <= wleft - 14'd1;
                    if (fin) begin
                        active <= 1'b0;
                        if ({1'b0, blk} < skip) done_dup <= 1'b1;
                        else                    done_msg <= 1'b1;
                    end
                end
            end
            if (load) begin
                active   <= 1'b1;
                skip1    <= ld_skip1;
                end_seen <= 1'b0;
                r        <= ld_r;
                widx     <= 3'd0;
                wleft    <= 14'((17'(ld_len) + 17'd7) >> 3);
                blk      <= ld_blk;
                o_len    <= ld_len;
            end
            if (clear) active <= 1'b0;
        end
    end
endmodule
