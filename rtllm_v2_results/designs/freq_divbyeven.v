//-----------------------------------------------------------------------------
// freq_divbyeven
//
// Even-factor clock divider.  A 4-bit counter counts input clock cycles; every
// NUM_DIV/2 cycles the output clk_div toggles, giving an output period of
// NUM_DIV input clock cycles with a 50% duty cycle.
//
//   NUM_DIV : even division factor (default 6).  Must satisfy
//             NUM_DIV/2 - 1 <= 15, i.e. NUM_DIV <= 32, because cnt is 4 bits.
//-----------------------------------------------------------------------------

module freq_divbyeven #(
    parameter NUM_DIV = 6
) (
    input  wire clk,
    input  wire rst_n,
    output reg  clk_div
);

    // Value the counter must reach before the output toggles.
    localparam [3:0] TOGGLE_CNT = (NUM_DIV / 2) - 1;

    reg [3:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt     <= 4'd0;
            clk_div <= 1'b0;
        end
        else if (cnt < TOGGLE_CNT) begin
            cnt     <= cnt + 4'd1;
            clk_div <= clk_div;
        end
        else begin
            cnt     <= 4'd0;
            clk_div <= ~clk_div;
        end
    end

endmodule
