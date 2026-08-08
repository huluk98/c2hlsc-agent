module square_wave (clk, freq, wave_out);

    input        clk;
    input  [7:0] freq;
    output       wave_out;

    // Registered output: driven only from the clocked block below.
    reg          wave_out = 1'b0;

    // Cycle counter between toggles; wraps naturally in 8 bits.
    reg  [7:0]   count    = 8'd0;

    // Toggle every `freq` clock cycles: match on count == freq-1,
    // computed with 8-bit wrapping arithmetic (freq == 0 -> 8'hFF).
    always @(posedge clk) begin
        if (count == (freq - 8'd1)) begin
            count    <= 8'd0;
            wave_out <= ~wave_out;
        end
        else begin
            count    <= count + 8'd1;
        end
    end

endmodule
