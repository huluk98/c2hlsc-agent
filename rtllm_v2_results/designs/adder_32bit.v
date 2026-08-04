//==========================================================================
// 4-bit carry-lookahead unit
//   Produces the internal carries, plus group generate/propagate.
//==========================================================================
module cla_4bit (
    input  [4:1] A,
    input  [4:1] B,
    input        Cin,
    output [4:1] S,
    output       Gout,   // group generate
    output       Pout    // group propagate
);

    wire [4:1] g;        // bit generate
    wire [4:1] p;        // bit propagate (XOR form, used for the sum)
    wire [4:1] c;        // carry into each bit position

    assign g = A & B;
    assign p = A ^ B;

    assign c[1] = Cin;
    assign c[2] = g[1] | (p[1] & c[1]);
    assign c[3] = g[2] | (p[2] & g[1]) | (p[2] & p[1] & c[1]);
    assign c[4] = g[3] | (p[3] & g[2]) | (p[3] & p[2] & g[1]) |
                  (p[3] & p[2] & p[1] & c[1]);

    assign S = p ^ c;

    // Group terms: generate/propagate for the whole nibble.
    assign Gout = g[4] | (p[4] & g[3]) | (p[4] & p[3] & g[2]) |
                  (p[4] & p[3] & p[2] & g[1]);
    assign Pout = p[4] & p[3] & p[2] & p[1];

endmodule


//==========================================================================
// 16-bit CLA block
//   Four 4-bit CLA units joined by a second-level lookahead stage, so the
//   block carries come from lookahead logic rather than a ripple chain.
//==========================================================================
module cla_16bit (
    input  [16:1] A,
    input  [16:1] B,
    input         Cin,
    output [16:1] S,
    output        Cout
);

    wire [4:1] G;        // group generate  from each nibble
    wire [4:1] P;        // group propagate from each nibble
    wire [4:1] C;        // carry into each nibble

    // Second-level lookahead over the four nibbles.
    assign C[1] = Cin;
    assign C[2] = G[1] | (P[1] & C[1]);
    assign C[3] = G[2] | (P[2] & G[1]) | (P[2] & P[1] & C[1]);
    assign C[4] = G[3] | (P[3] & G[2]) | (P[3] & P[2] & G[1]) |
                  (P[3] & P[2] & P[1] & C[1]);

    assign Cout = G[4] | (P[4] & G[3]) | (P[4] & P[3] & G[2]) |
                  (P[4] & P[3] & P[2] & G[1]) |
                  (P[4] & P[3] & P[2] & P[1] & C[1]);

    cla_4bit u0 (.A(A[4:1]),   .B(B[4:1]),   .Cin(C[1]),
                 .S(S[4:1]),   .Gout(G[1]),  .Pout(P[1]));

    cla_4bit u1 (.A(A[8:5]),   .B(B[8:5]),   .Cin(C[2]),
                 .S(S[8:5]),   .Gout(G[2]),  .Pout(P[2]));

    cla_4bit u2 (.A(A[12:9]),  .B(B[12:9]),  .Cin(C[3]),
                 .S(S[12:9]),  .Gout(G[3]),  .Pout(P[3]));

    cla_4bit u3 (.A(A[16:13]), .B(B[16:13]), .Cin(C[4]),
                 .S(S[16:13]), .Gout(G[4]),  .Pout(P[4]));

endmodule


//==========================================================================
// 32-bit carry-lookahead adder
//   Two 16-bit CLA blocks; lower block takes carry-in 0 and its carry-out
//   feeds the upper block.  {C32, S} = A + B.
//==========================================================================
module adder_32bit (
    input  [32:1] A,
    input  [32:1] B,
    output [32:1] S,
    output        C32
);

    wire c16;            // carry between the two 16-bit blocks

    cla_16bit lower (
        .A   (A[16:1]),
        .B   (B[16:1]),
        .Cin (1'b0),
        .S   (S[16:1]),
        .Cout(c16)
    );

    cla_16bit upper (
        .A   (A[32:17]),
        .B   (B[32:17]),
        .Cin (c16),
        .S   (S[32:17]),
        .Cout(C32)
    );

endmodule