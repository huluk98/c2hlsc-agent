//======================================================================
// adder_32bit : 32-bit Carry-Lookahead Adder
//   Built from two 16-bit CLA blocks, each built from four 4-bit CLA
//   blocks with group generate/propagate lookahead logic.
//   Purely combinational, unsigned, carry-in hardwired to 0.
//======================================================================

module adder_32bit (
    input  [32:1] A,
    input  [32:1] B,
    output [32:1] S,
    output        C32
);

    wire c16;          // carry between the two 16-bit blocks
    wire pg_lo, gg_lo; // unused group terms of block 0
    wire pg_hi, gg_hi; // unused group terms of block 1

    // Low block : bits 1..16, carry-in = 0
    cla_16bit u_cla_lo (
        .a    (A[16:1]),
        .b    (B[16:1]),
        .cin  (1'b0),
        .s    (S[16:1]),
        .cout (c16),
        .pg   (pg_lo),
        .gg   (gg_lo)
    );

    // High block : bits 17..32, carry-in = carry-out of low block
    cla_16bit u_cla_hi (
        .a    (A[32:17]),
        .b    (B[32:17]),
        .cin  (c16),
        .s    (S[32:17]),
        .cout (C32),
        .pg   (pg_hi),
        .gg   (gg_hi)
    );

endmodule

module cla_16bit (
    input  [15:0] a,
    input  [15:0] b,
    input         cin,
    output [15:0] s,
    output        cout,
    output        pg,   // group propagate of the whole 16-bit block
    output        gg    // group generate  of the whole 16-bit block
);

    wire [3:0] blk_p;  // per-nibble group propagate
    wire [3:0] blk_g;  // per-nibble group generate
    wire       c4, c8, c12;

    // Second-level lookahead across the four 4-bit blocks
    assign c4   = blk_g[0] | (blk_p[0] & cin);
    assign c8   = blk_g[1] | (blk_p[1] & blk_g[0])
                           | (blk_p[1] & blk_p[0] & cin);
    assign c12  = blk_g[2] | (blk_p[2] & blk_g[1])
                           | (blk_p[2] & blk_p[1] & blk_g[0])
                           | (blk_p[2] & blk_p[1] & blk_p[0] & cin);
    assign cout = blk_g[3] | (blk_p[3] & blk_g[2])
                           | (blk_p[3] & blk_p[2] & blk_g[1])
                           | (blk_p[3] & blk_p[2] & blk_p[1] & blk_g[0])
                           | (blk_p[3] & blk_p[2] & blk_p[1] & blk_p[0] & cin);

    // Group terms of the entire 16-bit slice
    assign pg = blk_p[3] & blk_p[2] & blk_p[1] & blk_p[0];
    assign gg = blk_g[3] | (blk_p[3] & blk_g[2])
                         | (blk_p[3] & blk_p[2] & blk_g[1])
                         | (blk_p[3] & blk_p[2] & blk_p[1] & blk_g[0]);

    cla_4bit u_n0 (
        .a   (a[3:0]),   .b (b[3:0]),   .cin (cin),
        .s   (s[3:0]),   .pg (blk_p[0]), .gg (blk_g[0])
    );

    cla_4bit u_n1 (
        .a   (a[7:4]),   .b (b[7:4]),   .cin (c4),
        .s   (s[7:4]),   .pg (blk_p[1]), .gg (blk_g[1])
    );

    cla_4bit u_n2 (
        .a   (a[11:8]),  .b (b[11:8]),  .cin (c8),
        .s   (s[11:8]),  .pg (blk_p[2]), .gg (blk_g[2])
    );

    cla_4bit u_n3 (
        .a   (a[15:12]), .b (b[15:12]), .cin (c12),
        .s   (s[15:12]), .pg (blk_p[3]), .gg (blk_g[3])
    );

endmodule

module cla_4bit (
    input  [3:0] a,
    input  [3:0] b,
    input        cin,
    output [3:0] s,
    output       pg,
    output       gg
);

    wire [3:0] p, g;
    wire       c1, c2, c3;

    assign p = a ^ b;   // bit propagate
    assign g = a & b;   // bit generate

    assign c1 = g[0] | (p[0] & cin);
    assign c2 = g[1] | (p[1] & g[0])
                     | (p[1] & p[0] & cin);
    assign c3 = g[2] | (p[2] & g[1])
                     | (p[2] & p[1] & g[0])
                     | (p[2] & p[1] & p[0] & cin);

    assign s = p ^ {c3, c2, c1, cin};

    assign pg = p[3] & p[2] & p[1] & p[0];
    assign gg = g[3] | (p[3] & g[2])
                     | (p[3] & p[2] & g[1])
                     | (p[3] & p[2] & p[1] & g[0]);

endmodule
