//======================================================================
// fixed_point_substractor
//
//   Sign-magnitude fixed-point subtractor:  c = a - b
//
//   Format : bit [N-1]   = sign (1 = negative)
//            bits[N-2:0] = magnitude, with Q fractional bits
//
//   Purely combinational, zero-cycle latency.  Q does not enter the
//   datapath because both operands share the same Q format; it is kept
//   as a parameter so the module matches the specified interface.
//======================================================================

module fixed_point_substractor #(
    parameter Q = 15,          // number of fractional bits
    parameter N = 32           // total number of bits (integer + fraction)
) (
    input  wire [N-1:0] a,     // first  operand (sign-magnitude)
    input  wire [N-1:0] b,     // second operand (sign-magnitude)
    output wire [N-1:0] c      // result of a - b (sign-magnitude)
);

    // N-bit register holding the result of the subtraction
    reg [N-1:0] res;

    assign c = res;

    always @(*) begin
        // default assignment -> no latch, every path drives res fully
        res = {N{1'b0}};

        if (a[N-1] == b[N-1]) begin
            //----------------------------------------------------------
            // Same sign : magnitudes subtract
            //----------------------------------------------------------
            if (a[N-2:0] > b[N-2:0]) begin
                // |a| > |b|  ->  result keeps the common sign
                res[N-2:0] = a[N-2:0] - b[N-2:0];
                res[N-1]   = a[N-1];
            end
            else begin
                // |a| <= |b| ->  result magnitude is |b| - |a|,
                //                sign is inverted (or zero)
                res[N-2:0] = b[N-2:0] - a[N-2:0];
                if (res[N-2:0] == {(N-1){1'b0}})
                    res[N-1] = 1'b0;      // never emit negative zero
                else
                    res[N-1] = ~a[N-1];
            end
        end
        else begin
            //----------------------------------------------------------
            // Different signs : subtracting b from a increases the
            // magnitude, so the absolute values are added and the
            // result carries the sign of a.
            //   a positive, b negative -> positive sum
            //   a negative, b positive -> negative sum
            //----------------------------------------------------------
            res[N-2:0] = a[N-2:0] + b[N-2:0];
            res[N-1]   = a[N-1];
        end
    end

endmodule

module fixed_point_subtractor #(
    parameter Q = 15,
    parameter N = 32
) (
    input  wire [N-1:0] a,
    input  wire [N-1:0] b,
    output wire [N-1:0] c
);

    fixed_point_substractor #(
        .Q (Q),
        .N (N)
    ) u_core (
        .a (a),
        .b (b),
        .c (c)
    );

endmodule
