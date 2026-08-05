`timescale 1ns/1ps

module tb_one_cycle_delayed_adder;

    logic       clk;
    logic       rst;
    logic       in_valid;
    logic [7:0] a;
    logic [7:0] b;
    logic       out_valid;
    logic [8:0] sum;

    integer trace_fd;
    integer errors;

    // Reference-model state used by the scoreboard.
    logic       expected_pending_valid;
    logic [8:0] expected_pending_sum;
    logic       expected_out_valid;
    logic [8:0] expected_sum;

    one_cycle_delayed_adder dut (
        .clk       (clk),
        .rst       (rst),
        .in_valid  (in_valid),
        .a         (a),
        .b         (b),
        .out_valid (out_valid),
        .sum       (sum)
    );

    initial begin
        clk = 1'b0;
        forever #5 clk = ~clk;
    end

    // Sample inputs exactly at the rising edge, update an independent model,
    // then wait 1 ps so the DUT's nonblocking assignments have settled.
    task automatic observe_and_check(input integer edge_number);
        logic       sampled_rst;
        logic       sampled_in_valid;
        logic [7:0] sampled_a;
        logic [7:0] sampled_b;
        integer     sampled_time_ns;
        begin
            @(posedge clk);
            sampled_rst      = rst;
            sampled_in_valid = in_valid;
            sampled_a        = a;
            sampled_b        = b;
            sampled_time_ns  = $time;

            if (sampled_rst) begin
                expected_pending_valid = 1'b0;
                expected_pending_sum   = 9'd0;
                expected_out_valid     = 1'b0;
                expected_sum           = 9'd0;
            end else begin
                expected_out_valid = expected_pending_valid;
                if (expected_pending_valid) begin
                    expected_sum = expected_pending_sum;
                end

                expected_pending_valid = sampled_in_valid;
                if (sampled_in_valid) begin
                    expected_pending_sum = {1'b0, sampled_a} +
                                           {1'b0, sampled_b};
                end
            end

            #1ps;

            if ((out_valid !== expected_out_valid) ||
                (sum !== expected_sum)) begin
                errors = errors + 1;
                $display("ERROR at E%0d (%0d ns): rst=%0b in_valid=%0b a=%0d b=%0d | expected out_valid=%0b sum=%0d, got out_valid=%0b sum=%0d",
                         edge_number, sampled_time_ns, sampled_rst,
                         sampled_in_valid, sampled_a, sampled_b,
                         expected_out_valid, expected_sum, out_valid, sum);
            end

            if ((dut.pending_valid !== expected_pending_valid) ||
                (dut.pending_sum !== expected_pending_sum)) begin
                errors = errors + 1;
                $display("ERROR at E%0d (%0d ns): expected pending_valid=%0b pending_sum=%0d, got pending_valid=%0b pending_sum=%0d",
                         edge_number, sampled_time_ns,
                         expected_pending_valid, expected_pending_sum,
                         dut.pending_valid, dut.pending_sum);
            end

            $fwrite(trace_fd, "E%0d,%0d,%0b,%0b,%0d,%0d,%0b,%0b,%0d,%0b,%0d\n",
                    edge_number, sampled_time_ns, sampled_rst,
                    sampled_in_valid, sampled_a, sampled_b,
                    (!sampled_rst && sampled_in_valid),
                    dut.pending_valid, dut.pending_sum, out_valid, sum);

            $display("E%0d @ %0d ns: rst=%0b in_valid=%0b a=%0d b=%0d | pending_valid=%0b out_valid=%0b sum=%0d%s",
                     edge_number, sampled_time_ns, sampled_rst,
                     sampled_in_valid, sampled_a, sampled_b,
                     dut.pending_valid, out_valid, sum,
                     out_valid ? "" : " (sum ignored)");
        end
    endtask

    initial begin
        $dumpfile("build/wave.vcd");
        $dumpvars(0, tb_one_cycle_delayed_adder);

        trace_fd = $fopen("build/cycle_trace.csv", "w");
        if (trace_fd == 0) begin
            $fatal(1, "Could not open build/cycle_trace.csv");
        end
        $fwrite(trace_fd,
                "edge,time_ns,rst,in_valid,a,b,input_accepted,pending_valid,pending_sum,out_valid,sum\n");

        errors                 = 0;
        expected_pending_valid = 1'b0;
        expected_pending_sum   = 9'd0;
        expected_out_valid     = 1'b0;
        expected_sum           = 9'd0;

        // Reset is synchronous: it takes effect only on these rising edges.
        rst      = 1'b1;
        in_valid = 1'b0;
        a        = 8'd0;
        b        = 8'd0;
        observe_and_check(0);

        @(negedge clk);
        observe_and_check(1);

        // Falling-edge drives make T0 stable before rising edge E2.
        @(negedge clk);
        rst      = 1'b0;
        in_valid = 1'b1;
        a        = 8'd3;
        b        = 8'd5;
        observe_and_check(2);

        // Bubble: these arbitrary bus values must not become a transaction.
        @(negedge clk);
        in_valid = 1'b0;
        a        = 8'd42;
        b        = 8'd99;
        observe_and_check(3);

        @(negedge clk);
        in_valid = 1'b1;
        a        = 8'd200;
        b        = 8'd100;
        observe_and_check(4);

        // T2 immediately follows T1, demonstrating one-per-cycle throughput.
        @(negedge clk);
        in_valid = 1'b1;
        a        = 8'd255;
        b        = 8'd255;
        observe_and_check(5);

        @(negedge clk);
        in_valid = 1'b0;
        a        = 8'd17;
        b        = 8'd34;
        observe_and_check(6);

        @(negedge clk);
        observe_and_check(7);

        // Accept a flush-probe transaction, then reset before it can emerge.
        @(negedge clk);
        in_valid = 1'b1;
        a        = 8'd1;
        b        = 8'd2;
        observe_and_check(8);

        @(negedge clk);
        rst      = 1'b1;
        in_valid = 1'b0;
        a        = 8'd0;
        b        = 8'd0;
        observe_and_check(9);

        // The cycle after reset proves that the pending transaction was lost.
        @(negedge clk);
        rst = 1'b0;
        observe_and_check(10);

        $fclose(trace_fd);

        if (errors != 0) begin
            $fatal(1, "FAIL: %0d scoreboard mismatch(es)", errors);
        end

        $display("PASS: all 11 rising-edge checks succeeded; one-cycle latency, bubbles, consecutive transactions, 9-bit arithmetic, and reset flush verified.");
        $finish;
    end

endmodule
