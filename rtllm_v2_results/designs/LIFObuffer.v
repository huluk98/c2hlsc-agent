//------------------------------------------------------------------------------
// LIFObuffer : 4-entry deep, 4-bit wide last-in-first-out stack buffer.
//
//   - Stack grows downward: SP == 4 means empty, SP == 0 means full.
//   - Push (RW == 0) writes stack_mem[SP-1] and decrements SP.
//   - Pop  (RW == 1) reads  stack_mem[SP],  clears it, and increments SP.
//   - All state changes are gated by EN; Rst is synchronous and also gated
//     by EN, exactly as described ("if EN is high: if Rst is high ...").
//   - EMPTY / FULL are combinational decodes of SP, so they track SP in the
//     same cycle SP changes (prevents a push while actually full).
//------------------------------------------------------------------------------

module LIFObuffer (
    input  wire [3:0] dataIn,
    input  wire       RW,
    input  wire       EN,
    input  wire       Rst,
    input  wire       Clk,
    output reg        EMPTY,
    output reg        FULL,
    output reg  [3:0] dataOut
);

    // Stack memory: 4 words of 4 bits.
    reg [3:0] stack_mem [0:3];

    // Stack pointer: 3 bits so that the empty encoding (4) is representable.
    reg [2:0] SP;

    integer i;

    // ---------------------------------------------------------------------
    // Flags: combinational decode of the stack pointer.
    // ---------------------------------------------------------------------
    always @(*) begin
        EMPTY = (SP == 3'd4);
        FULL  = (SP == 3'd0);
    end

    // ---------------------------------------------------------------------
    // Sequential stack behaviour.
    // ---------------------------------------------------------------------
    always @(posedge Clk) begin
        if (EN) begin
            if (Rst) begin
                SP      <= 3'd4;
                dataOut <= 4'b0000;
                for (i = 0; i < 4; i = i + 1) begin
                    stack_mem[i] <= 4'b0000;
                end
            end
            else begin
                if ((RW == 1'b0) && !FULL) begin
                    // Push
                    stack_mem[SP - 3'd1] <= dataIn;
                    SP                   <= SP - 3'd1;
                end
                else if ((RW == 1'b1) && !EMPTY) begin
                    // Pop
                    dataOut       <= stack_mem[SP];
                    stack_mem[SP] <= 4'b0000;
                    SP            <= SP + 3'd1;
                end
                // Otherwise (push while full / pop while empty): hold state.
            end
        end
        // EN low: freeze SP, memory and dataOut (and therefore the flags).
    end

endmodule
