module freq_divbyfrac (
    input  wire clk,
    input  wire rst_n,
    output wire clk_div
);

    parameter MUL2_DIV_CLK = 7;          // 2 * 3.5
    localparam HALF = (MUL2_DIV_CLK - 1) / 2;

    reg [3:0] cnt;                       // counts 0 .. MUL2_DIV_CLK-1
    reg       clk_div1;                  // posedge-generated intermediate clock
    reg       clk_div2;                  // negedge-generated intermediate clock

    // ------------------------------------------------------------------
    // Modulo-MUL2_DIV_CLK counter on the rising edge of the source clock
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cnt <= 4'd0;
        else if (cnt == MUL2_DIV_CLK - 1)
            cnt <= 4'd0;
        else
            cnt <= cnt + 4'd1;
    end

    // ------------------------------------------------------------------
    // Intermediate clock 1 (rising edge domain): two uneven periods inside
    // the 7-cycle window -- one 4 source cycles long, one 3 source cycles
    // long, so the average period is 3.5 source cycles.
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div1 <= 1'b0;
        else
            clk_div1 <= (cnt == 4'd0) || (cnt == HALF + 1);
    end

    // ------------------------------------------------------------------
    // Intermediate clock 2 (falling edge domain): the same two pulses, one
    // delayed by half a source period, the other advanced by half a period.
    // ------------------------------------------------------------------
    always @(negedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div2 <= 1'b0;
        else
            clk_div2 <= (cnt == 4'd1) || (cnt == HALF + 1);
    end

    // ------------------------------------------------------------------
    // OR of the two half-period-offset clocks evens out the 4/3 imbalance
    // ------------------------------------------------------------------
    assign clk_div = clk_div1 | clk_div2;

endmodule