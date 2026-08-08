//------------------------------------------------------------------------------
// freq_divbyodd : divide the input clock by an odd number (NUM_DIV, default 5)
//
//   Two modulo-NUM_DIV counters run off the two edges of clk:
//     cnt1 / clk_div1  -> posedge domain
//     cnt2 / clk_div2  -> negedge domain
//   Each half-clock rises on the edge where its counter is 0 and falls on the
//   edge where it reaches (NUM_DIV-1)/2, giving a period of exactly NUM_DIV clk
//   cycles that starts right after reset is released.
//   OR-ing the two (clk_div2 lags clk_div1 by half a clk period) yields a
//   50% duty cycle output at f(clk)/NUM_DIV.
//------------------------------------------------------------------------------

module freq_divbyodd #(
    parameter NUM_DIV = 5
) (
    input  wire clk,
    input  wire rst_n,
    output wire clk_div
);

    localparam CNT_W = (NUM_DIV > 1) ? $clog2(NUM_DIV) : 1;

    reg [CNT_W-1:0] cnt1;
    reg [CNT_W-1:0] cnt2;
    reg             clk_div1;
    reg             clk_div2;

    // ---------------- positive edge domain ----------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cnt1 <= {CNT_W{1'b0}};
        else if (cnt1 == NUM_DIV - 1)
            cnt1 <= {CNT_W{1'b0}};
        else
            cnt1 <= cnt1 + 1'b1;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div1 <= 1'b0;
        else if ((cnt1 == {CNT_W{1'b0}}) || (cnt1 == (NUM_DIV - 1) / 2))
            clk_div1 <= ~clk_div1;
        else
            clk_div1 <= clk_div1;
    end

    // ---------------- negative edge domain ----------------
    always @(negedge clk or negedge rst_n) begin
        if (!rst_n)
            cnt2 <= {CNT_W{1'b0}};
        else if (cnt2 == NUM_DIV - 1)
            cnt2 <= {CNT_W{1'b0}};
        else
            cnt2 <= cnt2 + 1'b1;
    end

    always @(negedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div2 <= 1'b0;
        else if ((cnt2 == {CNT_W{1'b0}}) || (cnt2 == (NUM_DIV - 1) / 2))
            clk_div2 <= ~clk_div2;
        else
            clk_div2 <= clk_div2;
    end

    // ---------------- output combination ----------------
    assign clk_div = clk_div1 | clk_div2;

endmodule
