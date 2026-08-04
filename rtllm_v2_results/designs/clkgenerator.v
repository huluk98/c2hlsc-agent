//-----------------------------------------------------------------------------
// clkgenerator
//
// Free-running simulation clock source. No inputs: the module drives its
// output procedurally from time 0, toggling every PERIOD/2 time units to
// produce a 50% duty-cycle square wave with period PERIOD.
//-----------------------------------------------------------------------------
module clkgenerator #(
    parameter PERIOD = 10
) (
    output reg clk
);

    // Initial state at time 0. The declaration initializer makes clk defined
    // before any process starts, so a sample taken at time 0 never sees X.
    initial begin
        clk = 1'b0;
    end

    // Invert every half period.
    //
    // The toggle uses a NONBLOCKING assignment on purpose. The testbench
    // samples clk at the very same timestamps at which the clock changes
    // (t = PERIOD/2, PERIOD, 3*PERIOD/2, ...) and compares against the level
    // of the half period that just ended. With a blocking assignment the
    // update lands in the active region, where its ordering against the
    // testbench's sampling process depends on elaboration order; here the
    // module's process is elaborated first, so every sample saw the value of
    // the *next* half period and all comparisons were inverted.
    //
    // A nonblocking update is deferred to the NBA region, i.e. after every
    // active-region read at that timestamp, so the sampled level is the one
    // for the half period that is ending, deterministically and independently
    // of process ordering. The waveform itself is unchanged: clk is 0 over
    // [0, PERIOD/2), 1 over [PERIOD/2, PERIOD), and so on.
    always begin
        #(PERIOD / 2) clk <= ~clk;
    end

endmodule