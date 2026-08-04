module fsm (
    input  wire IN,
    input  wire CLK,
    input  wire RST,
    output reg  MATCH
);

    // State encoding: length of the longest prefix of 10011 matched so far
    localparam [2:0] S0 = 3'd0,  // idle / no prefix
                     S1 = 3'd1,  // "1"
                     S2 = 3'd2,  // "10"
                     S3 = 3'd3,  // "100"
                     S4 = 3'd4;  // "1001"

    reg [2:0] state;
    reg [2:0] next_state;

    // Next-state logic (with longest-prefix backtracking)
    always @(*) begin
        case (state)
            S0:      next_state = IN ? S1 : S0;
            S1:      next_state = IN ? S1 : S2;   // "11" -> prefix "1"; "10" -> "10"
            S2:      next_state = IN ? S1 : S3;   // "101" -> prefix "1"; "100" -> "100"
            S3:      next_state = IN ? S4 : S0;   // "1001" -> "1001"; "1000" -> idle
            S4:      next_state = IN ? S1 : S2;   // "10011" match, reuse final 1 as "1";
                                                  // "10010" -> prefix "10"
            default: next_state = S0;
        endcase
    end

    // Mealy output: asserts in the same cycle as the 5th bit
    always @(*) begin
        if (RST)
            MATCH = 1'b0;
        else
            MATCH = (state == S4) && IN;
    end

    // State register: asynchronous active-high reset
    always @(posedge CLK or posedge RST) begin
        if (RST)
            state <= S0;
        else
            state <= next_state;
    end

endmodule