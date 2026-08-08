module sequence_detector (
    input  wire clk,
    input  wire rst_n,
    input  wire data_in,
    output reg  sequence_detected
);

    // State encoding: tracks the longest matched prefix of the pattern 1001
    localparam [2:0] IDLE = 3'd0,  // ""
                     S1   = 3'd1,  // "1"
                     S2   = 3'd2,  // "10"
                     S3   = 3'd3,  // "100"
                     S4   = 3'd4;  // "1001" -> match

    reg [2:0] state;
    reg [2:0] next_state;

    // Next-state decode (overlapping detection)
    always @(*) begin
        case (state)
            IDLE:    next_state = data_in ? S1   : IDLE;
            S1:      next_state = data_in ? S1   : S2;
            S2:      next_state = data_in ? S1   : S3;
            S3:      next_state = data_in ? S4   : IDLE;
            S4:      next_state = data_in ? S1   : S2;
            default: next_state = IDLE;
        endcase
    end

    // Moore output decode
    always @(*) begin
        sequence_detected = (state == S4);
    end

    // State register with asynchronous active-low reset
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= IDLE;
        else
            state <= next_state;
    end

endmodule
