//-----------------------------------------------------------------------------
// sub_64bit : 64-bit two's-complement subtractor with signed overflow flag
//
// Purely combinational, zero latency. No clock or reset.
//   result   = A - B  (wraps modulo 2^64; low-order difference always truthful)
//   overflow = 1 when the signed difference is not representable in 64 bits
//-----------------------------------------------------------------------------
module sub_64bit (
    input  wire [63:0] A,
    input  wire [63:0] B,
    output wire [63:0] result,
    output wire        overflow
);

    // Binary subtraction; carry/borrow out of bit 63 is discarded.
    assign result = A - B;

    // Overflow can only occur when the operand signs differ.
    //   positive overflow: (+A) - (-B) yields a negative result
    //   negative overflow: (-A) - (+B) yields a non-negative result
    assign overflow = (~A[63] &  B[63] &  result[63]) |
                      ( A[63] & ~B[63] & ~result[63]);

endmodule