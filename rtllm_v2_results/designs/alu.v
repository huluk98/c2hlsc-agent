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

    wire signed [31:0] a_signed = a;
    wire signed [31:0] b_signed = b;

    reg [32:0] res;
    reg        overflow_r;

    assign r        = res[31:0];
    assign carry    = res[32];
    assign zero     = (r == 32'b0);
    assign negative = r[31];
    assign overflow = overflow_r;
    assign flag     = (aluc == SLT)  ? (a_signed < b_signed) :
                      (aluc == SLTU) ? (a < b)               : 1'bz;

    always @(*) begin
        res        = 33'b0;
        overflow_r = 1'b0;
        case (aluc)
            ADD: begin
                res        = {a[31], a} + {b[31], b};
                overflow_r = (a[31] == b[31]) && (res[31] != a[31]);
            end
            ADDU: begin
                res        = {1'b0, a} + {1'b0, b};
                overflow_r = 1'b0;
            end
            SUB: begin
                res        = {a[31], a} - {b[31], b};
                overflow_r = (a[31] != b[31]) && (res[31] != a[31]);
            end
            SUBU: begin
                res        = {1'b0, a} - {1'b0, b};
                overflow_r = 1'b0;
            end
            AND:  res = {1'b0, a & b};
            OR:   res = {1'b0, a | b};
            XOR:  res = {1'b0, a ^ b};
            NOR:  res = {1'b0, ~(a | b)};
            SLT:  res = {32'b0, (a_signed < b_signed)};
            SLTU: res = {32'b0, (a < b)};
            SLL:  res = {1'b0, (b << a)};
            SRL:  res = {1'b0, (b >> a)};
            SRA:  res = {1'b0, (b_signed >>> a)};
            SLLV: res = {1'b0, (b << a[4:0])};
            SRLV: res = {1'b0, (b >> a[4:0])};
            SRAV: res = {1'b0, (b_signed >>> a[4:0])};
            LUI:  res = {1'b0, a[15:0], 16'b0};
            default: begin
                res        = 33'bz;
                overflow_r = 1'b0;
            end
        endcase
    end

endmodule