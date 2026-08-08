// 3-bit unsigned magnitude comparator
// Purely combinational: outputs are a one-hot function of the current A and B.

module comparator_3bit (
    input  wire [2:0] A,
    input  wire [2:0] B,
    output reg        A_greater,
    output reg        A_equal,
    output reg        A_less
);

    wire gt, eq, lt;

    // Structural bit-slice comparison, MSB first.
    // gt: A > B, eq: A == B, lt: A < B (unsigned)
    compare_bit u_bit2 (
        .a       (A[2]),
        .b       (B[2]),
        .gt_in   (1'b0),
        .eq_in   (1'b1),
        .lt_in   (1'b0),
        .gt_out  (gt2),
        .eq_out  (eq2),
        .lt_out  (lt2)
    );

    wire gt2, eq2, lt2;
    wire gt1, eq1, lt1;

    compare_bit u_bit1 (
        .a       (A[1]),
        .b       (B[1]),
        .gt_in   (gt2),
        .eq_in   (eq2),
        .lt_in   (lt2),
        .gt_out  (gt1),
        .eq_out  (eq1),
        .lt_out  (lt1)
    );

    compare_bit u_bit0 (
        .a       (A[0]),
        .b       (B[0]),
        .gt_in   (gt1),
        .eq_in   (eq1),
        .lt_in   (lt1),
        .gt_out  (gt),
        .eq_out  (eq),
        .lt_out  (lt)
    );

    // Drive all three outputs unconditionally so no latch is inferred.
    always @(*) begin
        A_greater = gt;
        A_equal   = eq;
        A_less    = lt;
    end

endmodule

module compare_bit (
    input  wire a,
    input  wire b,
    input  wire gt_in,
    input  wire eq_in,
    input  wire lt_in,
    output wire gt_out,
    output wire eq_out,
    output wire lt_out
);

    // A decision already made by higher bits sticks; ties are broken here.
    assign gt_out = gt_in | (eq_in &  a & ~b);
    assign lt_out = lt_in | (eq_in & ~a &  b);
    assign eq_out = eq_in & (a ~^ b);

endmodule
