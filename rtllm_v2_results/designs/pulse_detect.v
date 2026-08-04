module pulse_detect (
    input  wire clk,
    input  wire rst_n,
    input  wire data_in,
    output reg  data_out
);

    // State encoding
    localparam S_IDLE = 2'd0;  // no valid leading 0 seen yet
    localparam S_ZERO = 2'd1;  // leading 0 observed
    localparam S_ONE  = 2'd2;  // 0 followed by 1 observed

    reg [1:0] state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
        end else begin
            case (state)
                S_IDLE: begin
                    // wait for the leading 0 of a pulse
                    if (data_in == 1'b0)
                        state <= S_ZERO;
                    else
                        state <= S_IDLE;
                end

                S_ZERO: begin
                    // 0 seen; a 1 advances the pulse
                    if (data_in == 1'b1)
                        state <= S_ONE;
                    else
                        state <= S_ZERO;
                end

                S_ONE: begin
                    // 0 -> 1 seen; a 0 now completes the pulse and also
                    // serves as the leading 0 of the next pulse
                    if (data_in == 1'b0)
                        state <= S_ZERO;
                    else
                        state <= S_IDLE;  // 1 held too long: not a pulse
                end

                default: state <= S_IDLE;
            endcase
        end
    end

    // Output asserted on the final cycle of the pulse (0 -> 1 -> 0)
    always @(*) begin
        data_out = (state == S_ONE) && (data_in == 1'b0);
    end

endmodule