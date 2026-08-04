module multi_pipe_4bit #(
    parameter size = 4
) (
    input                     clk,
    input                     rst_n,
    input      [size-1:0]     mul_a,
    input      [size-1:0]     mul_b,
    output reg [2*size-1:0]   mul_out
);

    // Zero-extend inputs to 2*size bits
    wire [2*size-1:0] mul_a_extended;
    wire [2*size-1:0] mul_b_extended;

    assign mul_a_extended = {{size{1'b0}}, mul_a};
    assign mul_b_extended = {{size{1'b0}}, mul_b};

    // Partial products
    wire [2*size-1:0] temp [size-1:0];

    genvar i;
    generate
        for (i = 0; i < size; i = i + 1) begin : gen_partial_product
            assign temp[i] = mul_b_extended[i] ? (mul_a_extended << i)
                                               : {(2*size){1'b0}};
        end
    endgenerate

    // Stage 1 pipeline registers: pairwise sums of adjacent partial products
    reg [2*size-1:0] sum [size/2-1:0];

    integer j;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (j = 0; j < size/2; j = j + 1) begin
                sum[j] <= {(2*size){1'b0}};
            end
        end else begin
            for (j = 0; j < size/2; j = j + 1) begin
                sum[j] <= temp[2*j] + temp[2*j+1];
            end
        end
    end

    // Stage 2: final product
    integer k;
    reg [2*size-1:0] sum_total;

    always @(*) begin
        sum_total = {(2*size){1'b0}};
        for (k = 0; k < size/2; k = k + 1) begin
            sum_total = sum_total + sum[k];
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            mul_out <= {(2*size){1'b0}};
        end else begin
            mul_out <= sum_total;
        end
    end

endmodule