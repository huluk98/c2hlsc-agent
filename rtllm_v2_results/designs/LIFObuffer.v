module LIFObuffer (
    input        Clk,
    input        Rst,
    input        EN,
    input        RW,
    input  [3:0] dataIn,
    output reg   EMPTY,
    output reg   FULL,
    output reg [3:0] dataOut
);

    // 4 entries x 4 bits of stack storage
    reg [3:0] stack_mem [0:3];

    // Stack pointer: counts down from 4 (empty) to 0 (full)
    reg [2:0] SP;

    integer i;

    always @(posedge Clk) begin
        if (EN) begin
            if (Rst) begin
                SP      <= 3'd4;
                EMPTY   <= 1'b1;
                FULL    <= 1'b0;
                dataOut <= 4'b0;
                for (i = 0; i < 4; i = i + 1)
                    stack_mem[i] <= 4'b0;
            end
            else begin
                // Push: RW low and room available
                if (!RW && !FULL) begin
                    stack_mem[SP - 3'd1] <= dataIn;
                    SP                   <= SP - 3'd1;
                    EMPTY                <= 1'b0;
                    FULL                 <= ((SP - 3'd1) == 3'd0) ? 1'b1 : 1'b0;
                end
                // Pop: RW high and data available
                else if (RW && !EMPTY) begin
                    dataOut        <= stack_mem[SP];
                    stack_mem[SP]  <= 4'b0;
                    SP             <= SP + 3'd1;
                    FULL           <= 1'b0;
                    EMPTY          <= ((SP + 3'd1) == 3'd4) ? 1'b1 : 1'b0;
                end
            end
        end
    end

endmodule