//=====================================================================
// adder_pipe_64bit
//   64-bit ripple-carry adder split into four 16-bit pipeline stages.
//   Latency = 4 clock cycles, throughput = 1 add/cycle.
//   o_en is a 4-deep delayed copy of i_en, so it is asserted on exactly
//   the cycle the matching sum appears on `result`.
//=====================================================================

module adder_pipe_64bit #(
    parameter DATA_WIDTH = 64,
    parameter STG_WIDTH  = 16
) (
    input                        clk,
    input                        rst_n,
    input                        i_en,
    input      [DATA_WIDTH-1:0]  adda,
    input      [DATA_WIDTH-1:0]  addb,
    output reg [DATA_WIDTH:0]    result,
    output reg                   o_en
);

    // ---- stage 1 registers (after slice 0 has been added) -----------
    reg [47:0] a_s1, b_s1;      // operand bits [63:16] pipelined forward
    reg [15:0] sum_s1_0;        // partial sum of bits [15:0]
    reg        c_s1;            // carry into slice 1

    // ---- stage 2 registers (after slice 1 has been added) -----------
    reg [31:0] a_s2, b_s2;      // operand bits [63:32] pipelined forward
    reg [15:0] sum_s2_0, sum_s2_1;
    reg        c_s2;            // carry into slice 2

    // ---- stage 3 registers (after slice 2 has been added) -----------
    reg [15:0] a_s3, b_s3;      // operand bits [63:48] pipelined forward
    reg [15:0] sum_s3_0, sum_s3_1, sum_s3_2;
    reg        c_s3;            // carry into slice 3

    // ---- enable pipeline: 3 shift stages + o_en = 4 cycles ----------
    reg [2:0]  en_sr;

    // ---- combinational 16-bit ripple-carry slices -------------------
    wire [15:0] sum0, sum1, sum2, sum3;
    wire        cout0, cout1, cout2, cout3;

    rca16 u_rca0 (.a(adda[15:0]),  .b(addb[15:0]),  .cin(1'b0), .sum(sum0), .cout(cout0));
    rca16 u_rca1 (.a(a_s1[15:0]),  .b(b_s1[15:0]),  .cin(c_s1), .sum(sum1), .cout(cout1));
    rca16 u_rca2 (.a(a_s2[15:0]),  .b(b_s2[15:0]),  .cin(c_s2), .sum(sum2), .cout(cout2));
    rca16 u_rca3 (.a(a_s3),        .b(b_s3),        .cin(c_s3), .sum(sum3), .cout(cout3));

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_s1     <= 48'd0;
            b_s1     <= 48'd0;
            sum_s1_0 <= 16'd0;
            c_s1     <= 1'b0;

            a_s2     <= 32'd0;
            b_s2     <= 32'd0;
            sum_s2_0 <= 16'd0;
            sum_s2_1 <= 16'd0;
            c_s2     <= 1'b0;

            a_s3     <= 16'd0;
            b_s3     <= 16'd0;
            sum_s3_0 <= 16'd0;
            sum_s3_1 <= 16'd0;
            sum_s3_2 <= 16'd0;
            c_s3     <= 1'b0;

            en_sr    <= 3'd0;
            result   <= 65'd0;
            o_en     <= 1'b0;
        end
        else begin
            // ---------------- stage 1 -------------------------------
            a_s1     <= adda[63:16];
            b_s1     <= addb[63:16];
            sum_s1_0 <= sum0;
            c_s1     <= cout0;

            // ---------------- stage 2 -------------------------------
            a_s2     <= a_s1[47:16];
            b_s2     <= b_s1[47:16];
            sum_s2_0 <= sum_s1_0;
            sum_s2_1 <= sum1;
            c_s2     <= cout1;

            // ---------------- stage 3 -------------------------------
            a_s3     <= a_s2[31:16];
            b_s3     <= b_s2[31:16];
            sum_s3_0 <= sum_s2_0;
            sum_s3_1 <= sum_s2_1;
            sum_s3_2 <= sum2;
            c_s3     <= cout2;

            // ---------------- stage 4 (output) ----------------------
            result   <= {cout3, sum3, sum_s3_2, sum_s3_1, sum_s3_0};

            // ---------------- enable pipeline -----------------------
            en_sr    <= {en_sr[1:0], i_en};
            o_en     <= en_sr[2];
        end
    end

endmodule

module rca16 (
    input  [15:0] a,
    input  [15:0] b,
    input         cin,
    output [15:0] sum,
    output        cout
);
    wire [16:0] c;
    assign c[0] = cin;

    genvar i;
    generate
        for (i = 0; i < 16; i = i + 1) begin : bit_slice
            full_adder u_fa (
                .a    (a[i]),
                .b    (b[i]),
                .cin  (c[i]),
                .sum  (sum[i]),
                .cout (c[i+1])
            );
        end
    endgenerate

    assign cout = c[16];
endmodule

module full_adder (
    input  a,
    input  b,
    input  cin,
    output sum,
    output cout
);
    assign sum  = a ^ b ^ cin;
    assign cout = (a & b) | (a & cin) | (b & cin);
endmodule
