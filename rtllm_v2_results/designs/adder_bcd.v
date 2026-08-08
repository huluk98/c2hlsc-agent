// 4-bit BCD adder: adds two BCD digits plus a carry-in and produces
// a BCD-corrected sum digit with a decimal carry-out.

module adder_bcd (
    input  [3:0] A,
    input  [3:0] B,
    input        Cin,
    output reg [3:0] Sum,
    output reg       Cout
);

    // 5-bit binary total so the binary carry participates in the compare
    wire [4:0] total;
    wire [4:0] corrected;

    assign total     = {1'b0, A} + {1'b0, B} + {4'b0000, Cin};
    assign corrected = total + 5'd6;

    always @(*) begin
        if (total > 5'd9) begin
            Sum  = corrected[3:0];
            Cout = 1'b1;
        end else begin
            Sum  = total[3:0];
            Cout = 1'b0;
        end
    end

endmodule
