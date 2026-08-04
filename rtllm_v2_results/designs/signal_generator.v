module signal_generator (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [4:0] wave
);

    // 0 = counting up, 1 = counting down
    reg state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= 1'b0;
            wave  <= 5'd0;
        end else begin
            case (state)
                1'b0: begin
                    // counting up
                    if (wave == 5'd31) begin
                        // reached the top: turn around, never overflow past 31
                        wave  <= wave - 5'd1;
                        state <= 1'b1;
                    end else begin
                        wave  <= wave + 5'd1;
                        state <= 1'b0;
                    end
                end
                1'b1: begin
                    // counting down
                    if (wave == 5'd0) begin
                        // reached the bottom: turn around, never underflow past 0
                        wave  <= wave + 5'd1;
                        state <= 1'b0;
                    end else begin
                        wave  <= wave - 5'd1;
                        state <= 1'b1;
                    end
                end
            endcase
        end
    end

endmodule