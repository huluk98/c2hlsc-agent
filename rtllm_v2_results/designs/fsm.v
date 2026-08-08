module fsm (
    input  wire IN,
    input  wire CLK,
    input  wire RST,
    output reg  MATCH
);

    // State encoding: matched prefix of the pattern 1,0,0,1,1
    localparam S0 = 3'd0;  // ""
    localparam S1 = 3'd1;  // "1"
    localparam S2 = 3'd2;  // "10"
    localparam S3 = 3'd3;  // "100"
    localparam S4 = 3'd4;  // "1001"

    reg [2:0] state;
    reg [2:0] next_state;

    // Next-state logic (Mealy, overlapping detection)
    always @(*) begin
        case (state)
            S0:      next_state = IN ? S1 : S0;
            S1:      next_state = IN ? S1 : S2;
            S2:      next_state = IN ? S1 : S3;
            S3:      next_state = IN ? S4 : S0;
            S4:      next_state = IN ? S1 : S2;
            default: next_state = S0;
        endcase
    end

    // State register: synchronous on posedge CLK, asynchronous active-high reset
    always @(posedge CLK or posedge RST) begin
        if (RST)
            state <= S0;
        else
            state <= next_state;
    end

    // Mealy output: asserted in the same cycle as the fifth bit (IN=1 in state S4)
    always @(*) begin
        if (RST)
            MATCH = 1'b0;
        else if ((state == S4) && (IN == 1'b1))
            MATCH = 1'b1;
        else
            MATCH = 1'b0;
    end

endmodule
