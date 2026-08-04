//-----------------------------------------------------------------------------
// 1-bit full subtractor: computes D = X - Y - Bin, with borrow-out Bout.
//-----------------------------------------------------------------------------
module full_subtractor_1bit (
    input  wire X,
    input  wire Y,
    input  wire Bin,
    output wire D,
    output wire Bout
);

    wire x_xor_y;

    assign x_xor_y = X ^ Y;
    assign D       = x_xor_y ^ Bin;
    assign Bout    = (~X & Y) | (~x_xor_y & Bin);

endmodule

//-----------------------------------------------------------------------------
// 1-bit magnitude comparator slice (bit-level comparator primitive).
// Reports per-bit equality; used to detect a zero difference.
//-----------------------------------------------------------------------------
module comparator_1bit (
    input  wire X,
    input  wire Y,
    output wire EQ
);

    assign EQ = ~(X ^ Y);

endmodule

//-----------------------------------------------------------------------------
// 4-bit unsigned magnitude comparator built from bit-level comparators and a
// ripple-borrow subtractor chain.  Purely combinational.
//   A_less    : borrow-out of A - B
//   A_equal   : difference is zero and no borrow
//   A_greater : no borrow and difference non-zero
// Exactly one output is high for any input combination.
//-----------------------------------------------------------------------------
module comparator_4bit (
    input  wire [3:0] A,
    input  wire [3:0] B,
    output wire       A_greater,
    output wire       A_equal,
    output wire       A_less
);

    wire [3:0] diff;        // A - B
    wire [4:0] borrow;      // borrow[0] = borrow-in, borrow[4] = borrow-out
    wire [3:0] bit_eq;      // per-bit equality from bit-level comparators
    wire       eq_all;      // all bits equal
    wire       no_borrow;   // no borrow occurred -> A >= B

    assign borrow[0] = 1'b0;

    full_subtractor_1bit fs0 (
        .X    (A[0]),
        .Y    (B[0]),
        .Bin  (borrow[0]),
        .D    (diff[0]),
        .Bout (borrow[1])
    );

    full_subtractor_1bit fs1 (
        .X    (A[1]),
        .Y    (B[1]),
        .Bin  (borrow[1]),
        .D    (diff[1]),
        .Bout (borrow[2])
    );

    full_subtractor_1bit fs2 (
        .X    (A[2]),
        .Y    (B[2]),
        .Bin  (borrow[2]),
        .D    (diff[2]),
        .Bout (borrow[3])
    );

    full_subtractor_1bit fs3 (
        .X    (A[3]),
        .Y    (B[3]),
        .Bin  (borrow[3]),
        .D    (diff[3]),
        .Bout (borrow[4])
    );

    // Bit-level comparators: equality of the difference against zero is
    // equivalent to bitwise equality of A and B.
    comparator_1bit c0 (.X(A[0]), .Y(B[0]), .EQ(bit_eq[0]));
    comparator_1bit c1 (.X(A[1]), .Y(B[1]), .EQ(bit_eq[1]));
    comparator_1bit c2 (.X(A[2]), .Y(B[2]), .EQ(bit_eq[2]));
    comparator_1bit c3 (.X(A[3]), .Y(B[3]), .EQ(bit_eq[3]));

    assign eq_all    = bit_eq[0] & bit_eq[1] & bit_eq[2] & bit_eq[3];
    assign no_borrow = ~borrow[4];

    assign A_less    = borrow[4];
    assign A_equal   = no_borrow & eq_all & (diff == 4'b0000);
    assign A_greater = no_borrow & ~eq_all;

endmodule