//-----------------------------------------------------------------------------
// fixed_point_adder
//
// Parameterized sign-magnitude fixed-point adder.
//   a[N-1], b[N-1] : sign bits (0 = positive, 1 = negative)
//   a[N-2:0]       : magnitude, with Q fractional bits
//
// Purely combinational: c is a function of a and b with zero latency.
// Q does not appear in the datapath: both operands share the same binary
// point, so the magnitude add/subtract is a plain (N-1)-bit unsigned
// operation and the binary point is preserved by construction.
//-----------------------------------------------------------------------------

module fixed_point_adder #(
    parameter Q = 15,   // number of fractional bits
    parameter N = 32    // total number of bits (integer + fractional + sign)
) (
    input  wire [N-1:0] a,
    input  wire [N-1:0] b,
    output wire [N-1:0] c
);

    // Internal register holding the sign/magnitude result.
    reg [N-1:0] res;

    always @(*) begin
        if (a[N-1] == b[N-1]) begin
            // Same sign: add absolute values, keep the common sign.
            // A carry out of the (N-1)-bit magnitude is dropped (wraps mod 2^(N-1)),
            // the sign bit is left undisturbed.
            res[N-2:0] = a[N-2:0] + b[N-2:0];
            res[N-1]   = a[N-1];
        end
        else if (a[N-2:0] > b[N-2:0]) begin
            // Opposite signs, |a| > |b| : result takes a's sign.
            res[N-2:0] = a[N-2:0] - b[N-2:0];
            res[N-1]   = a[N-1];
        end
        else begin
            // Opposite signs, |b| >= |a| : result takes b's sign,
            // except an exactly zero magnitude which is forced positive.
            res[N-2:0] = b[N-2:0] - a[N-2:0];
            res[N-1]   = (a[N-2:0] == b[N-2:0]) ? 1'b0 : b[N-1];
        end
    end

    assign c = res;

endmodule
