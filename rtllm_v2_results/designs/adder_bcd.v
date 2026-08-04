module adder_bcd (
    input  wire [3:0] A,
    input  wire [3:0] B,
    input  wire       Cin,
    output wire [3:0] Sum,
    output wire       Cout
);

    // Raw 5-bit binary sum of the two BCD digits and the carry-in.
    wire [4:0] bin_sum;
    assign bin_sum = {1'b0, A} + {1'b0, B} + {4'b0000, Cin};

    // Correction needed when the binary sum exceeds 9, either because the
    // 4-bit adder carried out (bit 4) or because the low nibble is 1010-1111.
    wire correct;
    assign correct = bin_sum[4] | (bin_sum[3] & (bin_sum[2] | bin_sum[1]));

    // Add 6 when correcting; the low 4 bits are the valid BCD digit.
    wire [4:0] corrected;
    assign corrected = bin_sum + (correct ? 5'd6 : 5'd0);

    assign Sum  = corrected[3:0];
    assign Cout = correct;

endmodule