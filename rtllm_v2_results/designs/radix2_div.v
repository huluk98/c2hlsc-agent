//======================================================================
// radix2_div : simplified radix-2 (restoring) divider
//   8-bit signed / unsigned division
//   result = { remainder[7:0], quotient[7:0] }
//======================================================================

module radix2_div (
    input  wire        clk,
    input  wire        rst,
    input  wire        sign,
    input  wire [7:0]  dividend,
    input  wire [7:0]  divisor,
    input  wire        opn_valid,
    output reg         res_valid,
    input  wire        res_ready,
    output wire [15:0] result
);

    // ------------------------------------------------------------------
    // state
    // ------------------------------------------------------------------
    reg  [16:0] SR;             // {partial remainder , shifted dividend/quotient}
    reg  [8:0]  NEG_DIVISOR;    // two's complement of |divisor| (9-bit)
    reg  [3:0]  cnt;            // 1..8, done flagged by cnt[3]
    reg         start_cnt;      // busy
    reg  [7:0]  dividend_r;     // raw operands kept for the sign fix-up
    reg  [7:0]  divisor_r;
    reg         sign_r;
    reg  [15:0] result_r;       // holds the last completed result

    // ------------------------------------------------------------------
    // absolute values of the operands (magnitude only when sign = 1)
    // ------------------------------------------------------------------
    wire [7:0] abs_dividend = (sign & dividend[7]) ? (~dividend + 8'd1) : dividend;
    wire [7:0] abs_divisor  = (sign & divisor[7])  ? (~divisor  + 8'd1) : divisor;

    // ------------------------------------------------------------------
    // iteration datapath : compare / subtract / mux
    //   rem_win is the current partial remainder (9 bits so that
    //   2*rem + next_bit never overflows, even for divisors > 128)
    // ------------------------------------------------------------------
    wire [8:0] rem_win = SR[16:8];
    wire [9:0] sub_res = {1'b0, rem_win} + {1'b0, NEG_DIVISOR};
    wire       q_bit   = sub_res[9];                                // carry-out
    wire [7:0] mux_out = q_bit ? sub_res[7:0] : rem_win[7:0];

    // ------------------------------------------------------------------
    // final quotient / remainder with sign correction (truncate toward zero,
    // remainder takes the dividend's sign)
    // ------------------------------------------------------------------
    wire [7:0] quo_raw = {SR[6:0], q_bit};
    wire       quo_neg = sign_r & (dividend_r[7] ^ divisor_r[7]);
    wire       rem_neg = sign_r &  dividend_r[7];
    wire [7:0] quo_fix = quo_neg ? (~quo_raw + 8'd1) : quo_raw;
    wire [7:0] rem_fix = rem_neg ? (~mux_out + 8'd1) : mux_out;

    // a new request is taken only while idle and with no pending result
    wire accept = opn_valid & ~res_valid & ~start_cnt;

    // ------------------------------------------------------------------
    // control / sequencing
    // ------------------------------------------------------------------
    always @(posedge clk or posedge rst) begin
        if (rst) begin
            SR          <= 17'd0;
            NEG_DIVISOR <= 9'd0;
            cnt         <= 4'd0;
            start_cnt   <= 1'b0;
            res_valid   <= 1'b0;
            dividend_r  <= 8'd0;
            divisor_r   <= 8'd0;
            sign_r      <= 1'b0;
            result_r    <= 16'd0;
        end
        else if (start_cnt) begin
            if (cnt[3]) begin                       // cnt == 8 : division done
                cnt       <= 4'd0;
                start_cnt <= 1'b0;
                SR        <= {1'b0, rem_fix, quo_fix};
                result_r  <= {rem_fix, quo_fix};
                res_valid <= 1'b1;
            end
            else begin                              // one radix-2 step
                cnt <= cnt + 4'd1;
                SR  <= {mux_out, SR[7:0], q_bit};
            end
        end
        else if (res_valid) begin
            // hold the result until the consumer takes it
            if (res_ready)
                res_valid <= 1'b0;
        end
        else if (accept) begin
            dividend_r  <= dividend;
            divisor_r   <= divisor;
            sign_r      <= sign;
            SR          <= {8'd0, abs_dividend, 1'b0};
            NEG_DIVISOR <= (~{1'b0, abs_divisor}) + 9'd1;
            cnt         <= 4'd1;
            start_cnt   <= 1'b1;
        end
    end

    assign result = result_r;

endmodule
