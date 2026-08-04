module radix2_div (
    input  wire        clk,
    input  wire        rst,
    input  wire        sign,
    input  wire [7:0]  dividend,
    input  wire [7:0]  divisor,
    input  wire        opn_valid,
    input  wire        res_ready,
    output reg         res_valid,
    output wire [15:0] result
);

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------
    reg  [16:0] SR;            // {partial_remainder[8:0], quotient[7:0]}
    reg  [8:0]  NEG_DIVISOR;   // two's complement of |divisor|
    reg  [8:0]  cnt;           // one-hot iteration counter
    reg         start_cnt;
    reg         sign_r;
    reg         dividend_sign_r;
    reg         divisor_sign_r;

    // ------------------------------------------------------------------
    // Operand conditioning (magnitudes when signed)
    // ------------------------------------------------------------------
    wire [7:0] abs_dividend = (sign && dividend[7]) ? (~dividend + 8'd1) : dividend;
    wire [7:0] abs_divisor  = (sign && divisor[7])  ? (~divisor  + 8'd1) : divisor;

    // ------------------------------------------------------------------
    // Shift / subtract datapath
    // ------------------------------------------------------------------
    wire [9:0] sub_ext  = {1'b0, SR[16:8]} + {1'b0, NEG_DIVISOR};
    wire       carry    = sub_ext[9];
    wire [8:0] mux_out  = carry ? sub_ext[8:0] : SR[16:8];
    wire [16:0] SR_next = {mux_out[7:0], SR[7:0], carry};

    // ------------------------------------------------------------------
    // Sign correction of the final quotient / remainder
    // ------------------------------------------------------------------
    wire        quot_neg = sign_r & (dividend_sign_r ^ divisor_sign_r);
    wire        rem_neg  = sign_r & dividend_sign_r;
    wire [7:0]  final_quotient  = quot_neg ? (~SR[7:0]  + 8'd1) : SR[7:0];
    wire [7:0]  final_remainder = rem_neg  ? (~SR[16:9] + 8'd1) : SR[16:9];

    // ------------------------------------------------------------------
    // Sequential control
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst) begin
            SR              <= 17'd0;
            NEG_DIVISOR     <= 9'd0;
            cnt             <= 9'd0;
            start_cnt       <= 1'b0;
            sign_r          <= 1'b0;
            dividend_sign_r <= 1'b0;
            divisor_sign_r  <= 1'b0;
        end
        else if (start_cnt) begin
            if (cnt[8]) begin
                // Division finished: park the sign-corrected result in SR
                cnt       <= 9'd0;
                start_cnt <= 1'b0;
                SR        <= {1'b0, final_remainder, final_quotient};
            end
            else begin
                cnt <= {cnt[7:0], 1'b0};
                SR  <= SR_next;
            end
        end
        else if (opn_valid && !res_valid) begin
            // Latch operands, preload SR with |dividend| << 1
            SR              <= {8'd0, abs_dividend, 1'b0};
            NEG_DIVISOR     <= 9'd0 - {1'b0, abs_divisor};
            cnt             <= 9'd1;
            start_cnt       <= 1'b1;
            sign_r          <= sign;
            dividend_sign_r <= dividend[7];
            divisor_sign_r  <= divisor[7];
        end
    end

    // ------------------------------------------------------------------
    // Result valid handshake
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst)
            res_valid <= 1'b0;
        else if (start_cnt && cnt[8])
            res_valid <= 1'b1;
        else if (res_valid && res_ready)
            res_valid <= 1'b0;
    end

    assign result = SR[15:0];

endmodule