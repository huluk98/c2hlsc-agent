//////////////////////////////////////////////////////////////////////////////
// freq_divbyfrac : fractional (3.5x) clock divider using double-edge clocking
//
//   MUL2_DIV_CLK = 7 = 2 * 3.5
//   cnt      : free running 0..6 counter on posedge clk
//   clk_div1 : posedge register, high during cnt==1 and cnt==5
//              -> two uneven periods, one of 4 and one of 3 source cycles
//   clk_div2 : negedge register, high during cnt==1.5..2.5 and cnt==4.5..5.5
//              -> one pulse delayed by half a source period, the other
//                 advanced by half a source period
//   clk_div  : clk_div1 | clk_div2 -> uniform period of 3.5 source cycles
//              (1.5 cycles high, 2 cycles low)
//////////////////////////////////////////////////////////////////////////////

module freq_divbyfrac (
    input  wire clk,
    input  wire rst_n,
    output wire clk_div
);

    parameter MUL2_DIV_CLK = 7;

    reg [3:0] cnt;
    reg       clk_div1;
    reg       clk_div2;

    // ---------------------------------------------------------------------
    // Free running counter : 0 .. MUL2_DIV_CLK-1
    // ---------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cnt <= 4'd0;
        else if (cnt == (MUL2_DIV_CLK - 1))
            cnt <= 4'd0;
        else
            cnt <= cnt + 4'd1;
    end

    // ---------------------------------------------------------------------
    // First intermediate clock : two pulses per 7 source cycles, giving the
    // uneven 4 / 3 cycle periods, posedge clocked
    //   high during cnt == 1 and cnt == 5
    // ---------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div1 <= 1'b0;
        else
            clk_div1 <= (cnt == 4'd0) || (cnt == ((MUL2_DIV_CLK + 1) / 2));
    end

    // ---------------------------------------------------------------------
    // Second intermediate clock : same two pulses, one delayed and one
    // advanced by half a source period, negedge clocked
    // ---------------------------------------------------------------------
    always @(negedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div2 <= 1'b0;
        else
            clk_div2 <= (cnt == 4'd1) || (cnt == ((MUL2_DIV_CLK + 1) / 2));
    end

    // ---------------------------------------------------------------------
    // Final fractional divided clock
    // ---------------------------------------------------------------------
    assign clk_div = clk_div1 | clk_div2;

endmodule
