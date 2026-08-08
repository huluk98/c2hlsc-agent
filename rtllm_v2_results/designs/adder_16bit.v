module adder_16bit (
    input  [15:0] a,
    input  [15:0] b,
    input         Cin,
    output [15:0] y,
    output        Co
);

    wire carry_mid;

    adder_8bit u_low (
        .a   (a[7:0]),
        .b   (b[7:0]),
        .Cin (Cin),
        .y   (y[7:0]),
        .Co  (carry_mid)
    );

    adder_8bit u_high (
        .a   (a[15:8]),
        .b   (b[15:8]),
        .Cin (carry_mid),
        .y   (y[15:8]),
        .Co  (Co)
    );

endmodule

module adder_8bit (
    input  [7:0] a,
    input  [7:0] b,
    input        Cin,
    output [7:0] y,
    output       Co
);

    wire [8:0] c;

    assign c[0] = Cin;

    full_adder u_fa0 (.a(a[0]), .b(b[0]), .cin(c[0]), .sum(y[0]), .cout(c[1]));
    full_adder u_fa1 (.a(a[1]), .b(b[1]), .cin(c[1]), .sum(y[1]), .cout(c[2]));
    full_adder u_fa2 (.a(a[2]), .b(b[2]), .cin(c[2]), .sum(y[2]), .cout(c[3]));
    full_adder u_fa3 (.a(a[3]), .b(b[3]), .cin(c[3]), .sum(y[3]), .cout(c[4]));
    full_adder u_fa4 (.a(a[4]), .b(b[4]), .cin(c[4]), .sum(y[4]), .cout(c[5]));
    full_adder u_fa5 (.a(a[5]), .b(b[5]), .cin(c[5]), .sum(y[5]), .cout(c[6]));
    full_adder u_fa6 (.a(a[6]), .b(b[6]), .cin(c[6]), .sum(y[6]), .cout(c[7]));
    full_adder u_fa7 (.a(a[7]), .b(b[7]), .cin(c[7]), .sum(y[7]), .cout(c[8]));

    assign Co = c[8];

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
