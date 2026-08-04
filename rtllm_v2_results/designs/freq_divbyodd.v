module freq_divbyodd #(
    parameter NUM_DIV = 5
) (
    input  wire clk,
    input  wire rst_n,
    output wire clk_div
);

    // Width sufficient to hold values up to NUM_DIV-1
    localparam CNT_W = (NUM_DIV <= 2)   ? 1 :
                       (NUM_DIV <= 4)   ? 2 :
                       (NUM_DIV <= 8)   ? 3 :
                       (NUM_DIV <= 16)  ? 4 :
                       (NUM_DIV <= 32)  ? 5 :
                       (NUM_DIV <= 64)  ? 6 :
                       (NUM_DIV <= 128) ? 7 :
                       (NUM_DIV <= 256) ? 8 : 32;

    localparam HALF = (NUM_DIV - 1) / 2;

    reg [CNT_W-1:0] cnt1;
    reg [CNT_W-1:0] cnt2;
    reg             clk_div1;
    reg             clk_div2;

    // Rising-edge domain
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt1 <= {CNT_W{1'b0}};
        end else if (cnt1 == NUM_DIV - 1) begin
            cnt1 <= {CNT_W{1'b0}};
        end else begin
            cnt1 <= cnt1 + 1'b1;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            clk_div1 <= 1'b0;
        end else if ((cnt1 == {CNT_W{1'b0}}) || (cnt1 == HALF)) begin
            clk_div1 <= ~clk_div1;
        end
    end

    // Falling-edge domain
    always @(negedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt2 <= {CNT_W{1'b0}};
        end else if (cnt2 == NUM_DIV - 1) begin
            cnt2 <= {CNT_W{1'b0}};
        end else begin
            cnt2 <= cnt2 + 1'b1;
        end
    end

    always @(negedge clk or negedge rst_n) begin
        if (!rst_n) begin
            clk_div2 <= 1'b0;
        end else if ((cnt2 == {CNT_W{1'b0}}) || (cnt2 == HALF)) begin
            clk_div2 <= ~clk_div2;
        end
    end

    assign clk_div = clk_div1 | clk_div2;

endmodule