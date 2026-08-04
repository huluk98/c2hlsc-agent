module multi_8bit (
    input  wire [7:0]  A,
    input  wire [7:0]  B,
    output reg  [15:0] product
);

    integer i;
    reg [15:0] shifted_multiplicand;

    always @(*) begin
        product = 16'd0;
        shifted_multiplicand = {8'd0, A};
        for (i = 0; i < 8; i = i + 1) begin
            if (B[i])
                product = product + shifted_multiplicand;
            shifted_multiplicand = shifted_multiplicand << 1;
        end
    end

endmodule