// parallel2serial.v
// 4-bit parallel to serial converter, MSB first.
// Every 4 clock cycles a new 4-bit word is captured on d and shifted out
// one bit per cycle on dout, starting with d[3] on the cycle where
// valid_out is high.

module parallel2serial (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [3:0] d,
    output wire       valid_out,
    output wire       dout
);

    reg [1:0] cnt;    // free running 2-bit phase counter
    reg [3:0] data;   // word currently being serialized (left-rotated)
    reg       valid;  // registered valid flag

    // dout is a tap on the MSB of the rotate register so that it lines up in
    // the same cycle as valid_out.
    assign dout      = data[3];
    assign valid_out = valid;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt   <= 2'd0;
            data  <= 4'd0;
            valid <= 1'b0;
        end
        else if (cnt == 2'd3) begin
            // last bit of the current word is on dout this cycle:
            // load the next parallel word and flag it valid for next cycle
            data  <= d;
            cnt   <= 2'd0;
            valid <= 1'b1;
        end
        else begin
            cnt   <= cnt + 2'd1;
            valid <= 1'b0;
            data  <= {data[2:0], data[3]};  // rotate left, MSB -> LSB
        end
    end

endmodule
