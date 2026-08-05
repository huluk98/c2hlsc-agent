`timescale 1ns/1ps

module one_cycle_delayed_adder (
    input  logic       clk,
    input  logic       rst,
    input  logic       in_valid,
    input  logic [7:0] a,
    input  logic [7:0] b,
    output logic       out_valid,
    output logic [8:0] sum
);

    // These registers hold the transaction waiting for the next output edge.
    logic       pending_valid;
    logic [8:0] pending_sum;

    always_ff @(posedge clk) begin
        if (rst) begin
            pending_valid <= 1'b0;
            pending_sum   <= 9'd0;
            out_valid     <= 1'b0;
            sum           <= 9'd0;
        end else begin
            // First, move last cycle's pending transaction to the output.
            out_valid <= pending_valid;
            if (pending_valid) begin
                sum <= pending_sum;
            end

            // Then, sample this cycle's input for use on the next edge.
            pending_valid <= in_valid;
            if (in_valid) begin
                pending_sum <= {1'b0, a} + {1'b0, b};
            end
        end
    end

endmodule
