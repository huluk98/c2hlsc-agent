module div_16bit (
    input  wire [15:0] A,
    input  wire [7:0]  B,
    output reg  [15:0] result,
    output reg  [15:0] odd
);

    reg [15:0] a_reg;
    reg [7:0]  b_reg;

    // First combinational block: capture the inputs into internal registers
    always @(*) begin
        a_reg = A;
        b_reg = B;
    end

    // Second combinational block: restoring division
    integer i;
    reg [8:0]  rem;      // partial remainder (one extra bit for the shifted-in dividend bit)
    reg [15:0] quot;

    always @(*) begin
        rem  = 9'd0;
        quot = 16'd0;
        for (i = 15; i >= 0; i = i - 1) begin
            rem = {rem[7:0], a_reg[i]};
            if (rem >= {1'b0, b_reg}) begin
                rem     = rem - {1'b0, b_reg};
                quot[i] = 1'b1;
            end else begin
                quot[i] = 1'b0;
            end
        end
        result = quot;
        odd    = {7'd0, rem};
    end

endmodule