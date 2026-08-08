module accu (
    input              clk,
    input              rst_n,
    input      [7:0]   data_in,
    input              valid_in,
    output reg         valid_out,
    output reg [9:0]   data_out
);

    reg [9:0] acc;
    reg [1:0] cnt;

    wire [9:0] sum_next = acc + {2'b00, data_in};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc       <= 10'd0;
            cnt       <= 2'd0;
            data_out  <= 10'd0;
            valid_out <= 1'b0;
        end
        else begin
            if (valid_in) begin
                if (cnt == 2'd3) begin
                    data_out  <= sum_next;
                    valid_out <= 1'b1;
                    acc       <= 10'd0;
                    cnt       <= 2'd0;
                end
                else begin
                    acc       <= sum_next;
                    cnt       <= cnt + 2'd1;
                    valid_out <= 1'b0;
                end
            end
            else begin
                valid_out <= 1'b0;
                acc       <= acc;
                cnt       <= cnt;
                data_out  <= data_out;
            end
        end
    end

endmodule
