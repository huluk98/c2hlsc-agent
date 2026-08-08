// clkgenerator.v
// Free-running simulation clock source.
// Produces a 50% duty-cycle square wave with full period = PERIOD time units.
//
// Phase: clk is HIGH for the first half period (t = 0 .. PERIOD/2) and makes its
// first transition (falling) at t = PERIOD/2, then toggles every PERIOD/2.

module clkgenerator #(
    parameter PERIOD = 10
) (
    output reg clk
);

    // Initial state of the clock, set from an initial block.
    initial begin
        clk = 1'b1;
    end

    // Toggle every half period: delay first, then invert.
    always begin
        #(PERIOD / 2) clk = ~clk;
    end

endmodule
