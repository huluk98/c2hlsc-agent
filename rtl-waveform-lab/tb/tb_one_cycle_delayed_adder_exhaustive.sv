`timescale 1ns/1ps

// Black-box acceptance test: only documented ports are observed. Equivalent
// implementations may use any internal architecture that satisfies the contract.
module tb_one_cycle_delayed_adder_exhaustive;

    logic       clk = 1'b0;
    logic       rst;
    logic       in_valid;
    logic [7:0] a;
    logic [7:0] b;
    logic       out_valid;
    logic [8:0] sum;

    logic       model_valid;
    logic [8:0] model_sum;
    integer     errors;
    integer     accepted;
    integer     checked;
    integer     ai;
    integer     bi;

    one_cycle_delayed_adder dut (
        .clk       (clk),
        .rst       (rst),
        .in_valid  (in_valid),
        .a         (a),
        .b         (b),
        .out_valid (out_valid),
        .sum       (sum)
    );

    always #5 clk = ~clk;

    task automatic clock_and_check;
        logic       sampled_rst;
        logic       sampled_valid;
        logic [7:0] sampled_a;
        logic [7:0] sampled_b;
        logic       expected_valid;
        logic [8:0] expected_sum;
        integer     reference_sum;
        begin
            @(posedge clk);
            sampled_rst   = rst;
            sampled_valid = in_valid;
            sampled_a     = a;
            sampled_b     = b;
            expected_valid = sampled_rst ? 1'b0 : model_valid;
            expected_sum   = model_sum;

            #1ps;
            if (out_valid !== expected_valid) begin
                errors = errors + 1;
                $display("ERROR valid: rst=%0b input=(%0d,%0d,v=%0b) expected=%0b got=%0b",
                         sampled_rst, sampled_a, sampled_b, sampled_valid,
                         expected_valid, out_valid);
            end
            if ((sampled_rst && (sum !== 9'd0)) ||
                (!sampled_rst && expected_valid && (sum !== expected_sum))) begin
                errors = errors + 1;
                $display("ERROR sum: rst=%0b expected=%0d got=%0d",
                         sampled_rst, sampled_rst ? 9'd0 : expected_sum, sum);
            end
            if (!sampled_rst && expected_valid) checked = checked + 1;

            if (sampled_rst) begin
                model_valid = 1'b0;
                model_sum   = 9'd0;
            end else begin
                model_valid = sampled_valid;
                if (sampled_valid) begin
                    // Use integer arithmetic in the oracle so the checker does
                    // not duplicate the DUT's expression-width implementation.
                    reference_sum = sampled_a;
                    reference_sum = reference_sum + sampled_b;
                    model_sum = reference_sum[8:0];
                    accepted = accepted + 1;
                end
            end
        end
    endtask

    task automatic drive_before_edge(
        input logic       next_rst,
        input logic       next_valid,
        input logic [7:0] next_a,
        input logic [7:0] next_b
    );
        begin
            @(negedge clk);
            rst      = next_rst;
            in_valid = next_valid;
            a        = next_a;
            b        = next_b;
            clock_and_check();
        end
    endtask

    initial begin
        errors      = 0;
        accepted    = 0;
        checked     = 0;
        model_valid = 1'b0;
        model_sum   = 9'd0;
        rst         = 1'b1;
        in_valid    = 1'b0;
        a           = 8'd0;
        b           = 8'd0;

        clock_and_check();
        drive_before_edge(1'b1, 1'b0, 8'd255, 8'd255);
        drive_before_edge(1'b0, 1'b0, 8'd99, 8'd77);

        // Produce a valid output, then assert reset between clock edges. A
        // synchronous reset must not change registered outputs until posedge.
        drive_before_edge(1'b0, 1'b1, 8'd4, 8'd5);
        drive_before_edge(1'b0, 1'b0, 8'd0, 8'd0);
        #2;
        rst = 1'b1;
        #1ps;
        if ((out_valid !== 1'b1) || (sum !== 9'd9)) begin
            errors = errors + 1;
            $display("ERROR: synchronous reset changed outputs between edges");
        end
        rst = 1'b0;
        #1ps;
        if ((out_valid !== 1'b1) || (sum !== 9'd9)) begin
            errors = errors + 1;
            $display("ERROR: a reset pulse missing every rising edge changed outputs");
        end

        // Reset must win even when in_valid is simultaneously high; that input
        // is not accepted and no result may appear after reset is released.
        drive_before_edge(1'b1, 1'b1, 8'd255, 8'd255);
        drive_before_edge(1'b0, 1'b0, 8'd0, 8'd0);

        // A pending result must be discarded when synchronous reset wins.
        drive_before_edge(1'b0, 1'b1, 8'd1, 8'd2);
        drive_before_edge(1'b1, 1'b0, 8'd0, 8'd0);
        drive_before_edge(1'b0, 1'b0, 8'd0, 8'd0);

        // Stream every unsigned 8-bit operand pair at one input per cycle.
        for (ai = 0; ai < 256; ai = ai + 1) begin
            for (bi = 0; bi < 256; bi = bi + 1) begin
                drive_before_edge(1'b0, 1'b1, ai[7:0], bi[7:0]);
            end
        end

        // Drain the last result, then prove that a bubble follows it.
        drive_before_edge(1'b0, 1'b0, 8'd123, 8'd45);
        drive_before_edge(1'b0, 1'b0, 8'd0, 8'd0);

        if (errors != 0) begin
            $fatal(1, "FAIL: %0d black-box mismatch(es)", errors);
        end
        if ((accepted != 65538) || (checked != 65537)) begin
            $fatal(1, "FAIL: harness accounting accepted=%0d checked=%0d",
                   accepted, checked);
        end

        $display("PASS: all 65,536 unsigned operand pairs plus reset, flush, bubble, latency, and throughput checks succeeded.");
        $finish;
    end

endmodule
