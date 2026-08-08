module width_8to16 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        valid_in,
    input  wire [7:0]  data_in,
    output reg         valid_out,
    output reg  [15:0] data_out
);

    reg [7:0] data_lock;
    reg       flag;

    // flag: 0 -> next valid byte is the first (high) byte
    //       1 -> one byte is buffered, next valid byte completes the word
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            flag <= 1'b0;
        else if (valid_in)
            flag <= ~flag;
        else
            flag <= flag;
    end

    // capture the first byte of a pair
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            data_lock <= 8'd0;
        else if (valid_in && !flag)
            data_lock <= data_in;
        else
            data_lock <= data_lock;
    end

    // registered outputs: pulse one cycle after the second valid byte
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            data_out  <= 16'd0;
        end
        else if (valid_in && flag) begin
            valid_out <= 1'b1;
            data_out  <= {data_lock, data_in};
        end
        else begin
            valid_out <= 1'b0;
            data_out  <= data_out;
        end
    end

endmodule
