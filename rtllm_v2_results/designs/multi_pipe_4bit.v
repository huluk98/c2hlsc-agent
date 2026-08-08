//======================================================================
// 4-bit unsigned pipelined multiplier
//   - partial products generated combinationally (generate block)
//   - two levels of registers:
//       level 1 : pairwise sums of the partial products
//       level 2 : final product register (mul_out)
//   - latency = 2 clock cycles, throughput = 1 result/cycle
//   - asynchronous active-low reset
//======================================================================

module multi_pipe_4bit #(
    parameter size = 4
)(
    input                     clk,
    input                     rst_n,
    input  [size-1:0]         mul_a,
    input  [size-1:0]         mul_b,
    output reg [2*size-1:0]   mul_out
);

    // ------------------------------------------------------------------
    // Zero-extension of the inputs to 2*size bits
    // ------------------------------------------------------------------
    wire [2*size-1:0] mul_a_ext = {{size{1'b0}}, mul_a};
    wire [2*size-1:0] mul_b_ext = {{size{1'b0}}, mul_b};

    // ------------------------------------------------------------------
    // Partial products : temp[i] = mul_b[i] ? (mul_a_ext << i) : 0
    // ------------------------------------------------------------------
    wire [2*size-1:0] temp [size-1:0];

    genvar i;
    generate
        for (i = 0; i < size; i = i + 1) begin : gen_partial_product
            assign temp[i] = mul_b_ext[i] ? (mul_a_ext << i) : {(2*size){1'b0}};
        end
    endgenerate

    // ------------------------------------------------------------------
    // Pipeline level 1 : pairwise addition of the partial products
    //   sum[0] <= temp[0] + temp[1]
    //   sum[1] <= temp[2] + temp[3]
    // ------------------------------------------------------------------
    reg  [2*size-1:0] sum [(size/2)-1:0];

    integer k;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (k = 0; k < size/2; k = k + 1)
                sum[k] <= {(2*size){1'b0}};
        end
        else begin
            for (k = 0; k < size/2; k = k + 1)
                sum[k] <= temp[2*k] + temp[2*k+1];
        end
    end

    // ------------------------------------------------------------------
    // Combinational sum of the level-1 registers
    // ------------------------------------------------------------------
    reg [2*size-1:0] sum_total;
    integer m;
    always @(*) begin
        sum_total = {(2*size){1'b0}};
        for (m = 0; m < size/2; m = m + 1)
            sum_total = sum_total + sum[m];
    end

    // ------------------------------------------------------------------
    // Pipeline level 2 : final product register
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            mul_out <= {(2*size){1'b0}};
        else
            mul_out <= sum_total;
    end

endmodule
