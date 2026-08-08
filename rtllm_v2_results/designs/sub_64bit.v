//-----------------------------------------------------------------------------
// sub_64bit : 64-bit two's-complement subtractor with signed overflow flag
//
//   result   = A - B  (truncated modulo 2^64, natural wrap, no saturation)
//   overflow = 1 when the signed difference cannot be represented in 64 bits
//
// Purely combinational: no clock, no reset, no state, zero cycle latency.
//-----------------------------------------------------------------------------

module sub_64bit (
    input  wire [63:0] A,
    input  wire [63:0] B,
    output wire [63:0] result,
    output wire        overflow
);

    // A - B implemented as A + (~B) + 1 through a ripple-carry adder chain.
    wire [63:0] b_inv;
    wire [64:0] carry;

    assign b_inv   = ~B;
    assign carry[0] = 1'b1;          // carry-in of 1 completes the two's complement

    genvar i;
    generate
        for (i = 0; i < 64; i = i + 1) begin : sub_stage
            full_adder u_fa (
                .a    (A[i]),
                .b    (b_inv[i]),
                .cin  (carry[i]),
                .sum  (result[i]),
                .cout (carry[i+1])
            );
        end
    endgenerate

    // Signed overflow from the sign bits of A, B and the truncated result:
    //   positive overflow : (+A) - (-B) yields a negative result
    //   negative overflow : (-A) - (+B) yields a non-negative result
    // When A and B share a sign bit, overflow is impossible.
    assign overflow = ( ~A[63] &  B[63] &  result[63]) |
                      (  A[63] & ~B[63] & ~result[63]);

endmodule

module full_adder (
    input  wire a,
    input  wire b,
    input  wire cin,
    output wire sum,
    output wire cout
);

    assign sum  = a ^ b ^ cin;
    assign cout = (a & b) | (a & cin) | (b & cin);

endmodule
