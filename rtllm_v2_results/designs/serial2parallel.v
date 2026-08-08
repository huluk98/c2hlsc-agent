module serial2parallel (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       din_serial,
    input  wire       din_valid,
    output reg  [7:0] dout_parallel,
    output reg        dout_valid
);

    // 4-bit counter tracking how many serial bits have been accepted (1..8)
    reg [3:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= 4'd0;
        end else if (din_valid) begin
            if (cnt == 4'd8)
                cnt <= 4'd1;
            else
                cnt <= cnt + 4'd1;
        end else begin
            cnt <= cnt;
        end
    end

    // Shift register: first bit received lands in the MSB, eighth bit in the LSB
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dout_parallel <= 8'd0;
        end else if (din_valid) begin
            dout_parallel <= {dout_parallel[6:0], din_serial};
        end else begin
            dout_parallel <= dout_parallel;
        end
    end

    // Valid pulses for exactly the one cycle in which dout_parallel holds the full byte
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dout_valid <= 1'b0;
        end else if (din_valid && (cnt == 4'd7)) begin
            dout_valid <= 1'b1;
        end else begin
            dout_valid <= 1'b0;
        end
    end

endmodule
