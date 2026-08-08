module pulse_detect (
    input      clk,
    input      rst_n,
    input      data_in,
    output reg data_out
);

    // State encoding
    localparam S_IDLE   = 2'b00;  // last sample was 0, waiting for the rising edge
    localparam S_DETECT = 2'b01;  // saw 0 then 1, waiting for trailing 0
    localparam S_DONE   = 2'b10;  // trailing 0 seen, pulse reported this cycle
    localparam S_HIGH   = 2'b11;  // data_in held high for more than one cycle

    reg [1:0] state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= S_IDLE;
            data_out <= 1'b0;
        end
        else begin
            case (state)
                S_IDLE: begin
                    // after reset the line is already known to be 0, so a 1
                    // here immediately starts a pulse
                    if (data_in == 1'b1)
                        state <= S_DETECT;
                    else
                        state <= S_IDLE;
                    data_out <= 1'b0;
                end

                S_DETECT: begin
                    if (data_in == 1'b0) begin
                        state    <= S_DONE;
                        data_out <= 1'b1;    // 0-1-0 seen in exactly 3 cycles
                    end
                    else begin
                        // the 1 lasts longer than one cycle: not a pulse
                        state    <= S_HIGH;
                        data_out <= 1'b0;
                    end
                end

                S_DONE: begin
                    // the trailing 0 doubles as the leading 0 of the next pulse
                    if (data_in == 1'b1)
                        state <= S_DETECT;
                    else
                        state <= S_IDLE;
                    data_out <= 1'b0;
                end

                S_HIGH: begin
                    // wait for the line to fall; that 0 becomes the leading 0
                    if (data_in == 1'b1)
                        state <= S_HIGH;
                    else
                        state <= S_IDLE;
                    data_out <= 1'b0;
                end

                default: begin
                    state    <= S_IDLE;
                    data_out <= 1'b0;
                end
            endcase
        end
    end

endmodule
