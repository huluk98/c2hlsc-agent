//-----------------------------------------------------------------------------
// comparator_4bit
//
// Purely combinational 4-bit unsigned magnitude comparator built from a chain
// of bit-level full subtractors (borrow-ripple network).
//
//   {borrow, diff} = A - B
//   A_less    = borrow out of the MSB
//   A_equal   = no borrow and diff == 0
//   A_greater = no borrow and diff != 0
//
// Exactly one output is high for every one of the 256 input combinations.
//-----------------------------------------------------------------------------

module comparator_4bit (
    input  wire [3:0] A,
    input  wire [3:0] B,
    output wire       A_greater,
    output wire       A_equal,
    output wire       A_less
);

    // Difference bits and ripple borrow chain (borrow[0] is the borrow-in = 0)
    wire [3:0] diff;
    wire [4:0] borrow;

    assign borrow[0] = 1'b0;

    full_subtractor fs0 (
        .a    (A[0]),
        .b    (B[0]),
        .bin  (borrow[0]),
        .d    (diff[0]),
        .bout (borrow[1])
    );

    full_subtractor fs1 (
        .a    (A[1]),
        .b    (B[1]),
        .bin  (borrow[1]),
        .d    (diff[1]),
        .bout (borrow[2])
    );

    full_subtractor fs2 (
        .a    (A[2]),
        .b    (B[2]),
        .bin  (borrow[2]),
        .d    (diff[2]),
        .bout (borrow[3])
    );

    full_subtractor fs3 (
        .a    (A[3]),
        .b    (B[3]),
        .bin  (borrow[3]),
        .d    (diff[3]),
        .bout (borrow[4])
    );

    // Borrow out of the MSB means A < B
    wire final_borrow = borrow[4];

    // Zero detect on the 4-bit difference
    wire diff_is_zero = ~(diff[3] | diff[2] | diff[1] | diff[0]);

    // One-hot by construction: no priority arbitration needed
    assign A_less    =  final_borrow;
    assign A_equal   = ~final_borrow &  diff_is_zero;
    assign A_greater = ~final_borrow & ~diff_is_zero;

endmodule

module full_subtractor (
    input  wire a,
    input  wire b,
    input  wire bin,
    output wire d,
    output wire bout
);

    assign d    = a ^ b ^ bin;
    assign bout = (~a & b) | (~a & bin) | (b & bin);

endmodule
