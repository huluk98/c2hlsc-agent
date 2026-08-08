module multi_8bit (
    input  [7:0]  A,
    input  [7:0]  B,
    output reg [15:0] product
);

    integer i;
    reg [15:0] shifted_multiplicand;
    reg [15:0] accumulator;

    always @(*) begin
        accumulator          = 16'b0;
        shifted_multiplicand = {8'b0, A};
        for (i = 0; i < 8; i = i + 1) begin
            if (B[i] == 1'b1)
                accumulator = accumulator + shifted_multiplicand;
            shifted_multiplicand = shifted_multiplicand << 1;
        end
        product = accumulator;
    end

endmodule
