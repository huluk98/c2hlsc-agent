// 64-bit Johnson (twisted ring) counter
// Shifts right, feeding the inverted LSB back into the MSB.
// Sequence (4-bit analogue): 0000, 1000, 1100, 1110, 1111, 0111, 0011, 0001, 0000

module JC_counter (
    input  wire        clk,
    input  wire        rst_n,
    output reg  [63:0] Q
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            Q <= 64'd0;
        end else begin
            // Q[0] == 0 -> append 1 at MSB; Q[0] == 1 -> append 0 at MSB
            Q <= {~Q[0], Q[63:1]};
        end
    end

endmodule
