//-----------------------------------------------------------------------------
// div_16bit : 16-bit dividend / 8-bit divisor restoring divider (combinational)
//   result = A / B   (16-bit quotient)
//   odd    = A % B   (16-bit remainder, upper bits zero when B != 0)
//   B == 0 : algorithm runs naturally -> result = 16'hFFFF, odd = A
//-----------------------------------------------------------------------------

module div_16bit (
    input  wire [15:0] A,
    input  wire [7:0]  B,
    output reg  [15:0] result,
    output reg  [15:0] odd
);

    // ---- combinational input capture -------------------------------------
    reg [15:0] a_reg;
    reg [7:0]  b_reg;

    always @(*) begin
        a_reg = A;
        b_reg = B;
    end

    // ---- restoring division ----------------------------------------------
    reg [15:0] rem;      // partial remainder
    reg [15:0] quo;      // quotient accumulator
    reg [15:0] den;      // zero-extended divisor
    integer    i;

    always @(*) begin
        rem = 16'd0;
        quo = 16'd0;
        den = {8'd0, b_reg};

        for (i = 15; i >= 0; i = i - 1) begin
            // shift in the next (highest remaining) dividend bit
            rem = {rem[14:0], a_reg[i]};

            if (rem >= den) begin
                rem    = rem - den;
                quo[i] = 1'b1;
            end else begin
                quo[i] = 1'b0;
            end
        end

        result = quo;
        odd    = rem;
    end

endmodule
