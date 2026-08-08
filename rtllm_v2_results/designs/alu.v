module alu (
    input  [31:0] a,
    input  [31:0] b,
    input  [5:0]  aluc,
    output [31:0] r,
    output        zero,
    output        carry,
    output        negative,
    output        overflow,
    output        flag
);

    parameter ADD  = 6'b100000;
    parameter ADDU = 6'b100001;
    parameter SUB  = 6'b100010;
    parameter SUBU = 6'b100011;
    parameter AND  = 6'b100100;
    parameter OR   = 6'b100101;
    parameter XOR  = 6'b100110;
    parameter NOR  = 6'b100111;
    parameter SLT  = 6'b101010;
    parameter SLTU = 6'b101011;
    parameter SLL  = 6'b000000;
    parameter SRL  = 6'b000010;
    parameter SRA  = 6'b000011;
    parameter SLLV = 6'b000100;
    parameter SRLV = 6'b000110;
    parameter SRAV = 6'b000111;
    parameter LUI  = 6'b001111;

    // signed views of the operands
    wire signed [31:0] a_signed;
    wire signed [31:0] b_signed;

    assign a_signed = a;
    assign b_signed = b;

    // 33-bit internal result: bit 32 carries out of the 32-bit datapath
    reg [32:0] res;

    assign r        = res[31:0];
    assign zero     = (r == 32'b0);
    assign carry    = res[32];
    assign negative = r[31];

    // signed overflow only makes sense for the signed add / subtract
    assign overflow = ((aluc == ADD) && (a[31] == b[31]) && (res[31] != a[31])) ||
                      ((aluc == SUB) && (a[31] != b[31]) && (res[31] != a[31]));

    // flag is driven only by the set-less-than instructions
    assign flag = ((aluc == SLT) || (aluc == SLTU)) ? 1'b1 : 1'bz;

    always @(*) begin
        case (aluc)
            ADD  : res = a_signed + b_signed;
            ADDU : res = a + b;
            SUB  : res = a_signed - b_signed;
            SUBU : res = a - b;
            AND  : res = a & b;
            OR   : res = a | b;
            XOR  : res = a ^ b;
            NOR  : res = ~(a | b);
            SLT  : res = (a_signed < b_signed) ? 33'd1 : 33'd0;
            SLTU : res = (a < b) ? 33'd1 : 33'd0;
            SLL  : res = b << a;
            SRL  : res = b >> a;
            SRA  : res = b_signed >>> a;
            SLLV : res = b << a[4:0];
            SRLV : res = b >> a[4:0];
            SRAV : res = b_signed >>> a[4:0];
            LUI  : res = {a[15:0], 16'h0000};
            default : res = 33'bz;
        endcase
    end

endmodule
