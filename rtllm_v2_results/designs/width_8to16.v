module width_8to16 (
    input              clk,
    input              rst_n,
    input              valid_in,
    input      [7:0]   data_in,
    output reg         valid_out,
    output reg [15:0]  data_out
);

    reg [7:0] data_lock;
    reg       flag;

    // flag: 0 => next valid beat is the first byte of a pair
    //       1 => a first byte is pending in data_lock
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            flag <= 1'b0;
        end else if (valid_in) begin
            flag <= ~flag;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            data_lock <= 8'b0;
        end else if (valid_in && !flag) begin
            data_lock <= data_in;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            data_out <= 16'b0;
        end else if (valid_in && flag) begin
            data_out <= {data_lock, data_in};
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
        end else begin
            valid_out <= valid_in && flag;
        end
    end

endmodule