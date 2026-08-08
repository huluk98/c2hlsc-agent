//////////////////////////////////////////////////////////////////////////////
// multi_pipe_8bit : unsigned 8x8 pipelined multiplier
//   3-stage pipeline :  input registers -> partial sums -> final sum
//   Latency  : 3 clock cycles
//   Throughput : 1 result per clock
//////////////////////////////////////////////////////////////////////////////

module multi_pipe_8bit (
    input             clk,
    input             rst_n,
    input             mul_en_in,
    input      [7:0]  mul_a,
    input      [7:0]  mul_b,
    output            mul_en_out,
    output     [15:0] mul_out
);

    //------------------------------------------------------------------
    // Enable (valid) shift register : one bit per pipeline stage
    //------------------------------------------------------------------
    reg  [2:0]  mul_en_out_reg;

    //------------------------------------------------------------------
    // Stage 0 : input registers
    //------------------------------------------------------------------
    reg  [7:0]  mul_a_reg;
    reg  [7:0]  mul_b_reg;

    //------------------------------------------------------------------
    // Partial products (combinational)
    //------------------------------------------------------------------
    wire [15:0] temp0, temp1, temp2, temp3;
    wire [15:0] temp4, temp5, temp6, temp7;

    //------------------------------------------------------------------
    // Stage 1 : partial sums
    //------------------------------------------------------------------
    reg  [15:0] sum0, sum1, sum2, sum3;

    //------------------------------------------------------------------
    // Stage 2 : final product register
    //------------------------------------------------------------------
    reg  [15:0] mul_out_reg;

    //------------------------------------------------------------------
    // Valid pipeline : shifts in mul_en_in every cycle
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            mul_en_out_reg <= 3'b000;
        else
            mul_en_out_reg <= {mul_en_out_reg[1:0], mul_en_in};
    end

    //------------------------------------------------------------------
    // Input registers : loaded only while the input enable is active
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            mul_a_reg <= 8'h00;
            mul_b_reg <= 8'h00;
        end
        else if (mul_en_in) begin
            mul_a_reg <= mul_a;
            mul_b_reg <= mul_b;
        end
    end

    //------------------------------------------------------------------
    // Partial product generation : multiplicand gated by multiplier bit,
    // shifted to its own weight
    //------------------------------------------------------------------
    assign temp0 = mul_b_reg[0] ? {8'h00, mul_a_reg}       : 16'h0000;
    assign temp1 = mul_b_reg[1] ? {7'h00, mul_a_reg, 1'b0} : 16'h0000;
    assign temp2 = mul_b_reg[2] ? {6'h00, mul_a_reg, 2'b0} : 16'h0000;
    assign temp3 = mul_b_reg[3] ? {5'h00, mul_a_reg, 3'b0} : 16'h0000;
    assign temp4 = mul_b_reg[4] ? {4'h0,  mul_a_reg, 4'b0} : 16'h0000;
    assign temp5 = mul_b_reg[5] ? {3'h0,  mul_a_reg, 5'b0} : 16'h0000;
    assign temp6 = mul_b_reg[6] ? {2'h0,  mul_a_reg, 6'b0} : 16'h0000;
    assign temp7 = mul_b_reg[7] ? {1'b0,  mul_a_reg, 7'b0} : 16'h0000;

    //------------------------------------------------------------------
    // Partial sum stage
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sum0 <= 16'h0000;
            sum1 <= 16'h0000;
            sum2 <= 16'h0000;
            sum3 <= 16'h0000;
        end
        else if (mul_en_out_reg[0]) begin
            sum0 <= temp0 + temp1;
            sum1 <= temp2 + temp3;
            sum2 <= temp4 + temp5;
            sum3 <= temp6 + temp7;
        end
    end

    //------------------------------------------------------------------
    // Final accumulation stage
    //------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            mul_out_reg <= 16'h0000;
        else if (mul_en_out_reg[1])
            mul_out_reg <= sum0 + sum1 + sum2 + sum3;
    end

    //------------------------------------------------------------------
    // Outputs
    //------------------------------------------------------------------
    assign mul_en_out = mul_en_out_reg[2];
    assign mul_out    = mul_en_out ? mul_out_reg : 16'h0000;

endmodule
