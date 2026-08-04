//======================================================================
// float_multi : IEEE-754 single precision floating point multiplier
//               Multi-cycle (8 state) sequential implementation.
//======================================================================
module float_multi (
    input             clk,
    input             rst,
    input      [31:0] a,
    input      [31:0] b,
    output reg [31:0] z
);

    // ------------------------------------------------------------------
    // Internal state
    // ------------------------------------------------------------------
    reg  [2:0]  counter;

    reg  [23:0] a_mantissa, b_mantissa, z_mantissa;
    reg  [9:0]  a_exponent, b_exponent, z_exponent;
    reg         a_sign, b_sign, z_sign;

    reg  [49:0] product;
    reg         guard_bit, round_bit, sticky;

    // 24x24 mantissa multiplier (48 bits)
    wire [47:0] mant_prod = a_mantissa * b_mantissa;

    // shift amounts used when normalising sub-normal operands
    wire [4:0]  a_shift = lzc24(a_mantissa);
    wire [4:0]  b_shift = lzc24(b_mantissa);

    // ------------------------------------------------------------------
    // leading-zero count of a 24 bit value (counted from bit 23 down)
    // ------------------------------------------------------------------
    function [4:0] lzc24;
        input [23:0] v;
        integer i;
        reg    found;
        begin
            lzc24 = 5'd0;
            found = 1'b0;
            for (i = 23; i >= 0; i = i - 1) begin
                if (!found && v[i]) begin
                    lzc24 = 23 - i[4:0];
                    found = 1'b1;
                end
            end
        end
    endfunction

    // ------------------------------------------------------------------
    // Main sequencer
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst) begin
            counter    <= 3'd0;
            z          <= 32'd0;
            a_mantissa <= 24'd0;
            b_mantissa <= 24'd0;
            z_mantissa <= 24'd0;
            a_exponent <= 10'd0;
            b_exponent <= 10'd0;
            z_exponent <= 10'd0;
            a_sign     <= 1'b0;
            b_sign     <= 1'b0;
            z_sign     <= 1'b0;
            product    <= 50'd0;
            guard_bit  <= 1'b0;
            round_bit  <= 1'b0;
            sticky     <= 1'b0;
        end else begin
            case (counter)

            // ----------------------------------------------------------
            // 0 : unpack the operands
            // ----------------------------------------------------------
            3'd0: begin
                a_mantissa <= {1'b0, a[22:0]};
                b_mantissa <= {1'b0, b[22:0]};
                a_exponent <= a[30:23] - 127;
                b_exponent <= b[30:23] - 127;
                a_sign     <= a[31];
                b_sign     <= b[31];
                counter    <= 3'd1;
            end

            // ----------------------------------------------------------
            // 1 : special cases (NaN / Inf / Zero) and hidden bit insert
            // ----------------------------------------------------------
            3'd1: begin
                // either operand is NaN -> quiet NaN
                if ((($signed(a_exponent) == 128) && (a_mantissa != 24'd0)) ||
                    (($signed(b_exponent) == 128) && (b_mantissa != 24'd0))) begin
                    z       <= {1'b1, 8'hFF, 1'b1, 22'd0};
                    counter <= 3'd0;
                end
                // a is infinity
                else if ($signed(a_exponent) == 128) begin
                    if (($signed(b_exponent) == -127) && (b_mantissa == 24'd0)) begin
                        z <= {1'b1, 8'hFF, 1'b1, 22'd0};      // inf * 0 -> NaN
                    end else begin
                        z <= {a_sign ^ b_sign, 8'hFF, 23'd0}; // signed infinity
                    end
                    counter <= 3'd0;
                end
                // b is infinity
                else if ($signed(b_exponent) == 128) begin
                    if (($signed(a_exponent) == -127) && (a_mantissa == 24'd0)) begin
                        z <= {1'b1, 8'hFF, 1'b1, 22'd0};      // 0 * inf -> NaN
                    end else begin
                        z <= {a_sign ^ b_sign, 8'hFF, 23'd0};
                    end
                    counter <= 3'd0;
                end
                // either operand is zero -> signed zero
                else if ((($signed(a_exponent) == -127) && (a_mantissa == 24'd0)) ||
                         (($signed(b_exponent) == -127) && (b_mantissa == 24'd0))) begin
                    z       <= {a_sign ^ b_sign, 31'd0};
                    counter <= 3'd0;
                end
                // normal / sub-normal operands
                else begin
                    if ($signed(a_exponent) == -127)
                        a_exponent    <= -126;      // sub-normal, no hidden bit
                    else
                        a_mantissa[23] <= 1'b1;     // normal, insert hidden bit

                    if ($signed(b_exponent) == -127)
                        b_exponent    <= -126;
                    else
                        b_mantissa[23] <= 1'b1;

                    counter <= 3'd2;
                end
            end

            // ----------------------------------------------------------
            // 2 : normalise sub-normal operands
            // ----------------------------------------------------------
            3'd2: begin
                a_mantissa <= a_mantissa << a_shift;
                a_exponent <= a_exponent - a_shift;
                b_mantissa <= b_mantissa << b_shift;
                b_exponent <= b_exponent - b_shift;
                counter    <= 3'd3;
            end

            // ----------------------------------------------------------
            // 3 : multiply mantissas, combine signs, add exponents
            // ----------------------------------------------------------
            3'd3: begin
                product    <= {mant_prod, 2'b00};
                z_sign     <= a_sign ^ b_sign;
                z_exponent <= a_exponent + b_exponent + 1;
                counter    <= 3'd4;
            end

            // ----------------------------------------------------------
            // 4 : extract mantissa and guard / round / sticky bits
            // ----------------------------------------------------------
            3'd4: begin
                z_mantissa <= product[49:26];
                guard_bit  <= product[25];
                round_bit  <= product[24];
                sticky     <= (product[23:0] != 24'd0);
                counter    <= 3'd5;
            end

            // ----------------------------------------------------------
            // 5 : normalise the product (at most one left shift)
            // ----------------------------------------------------------
            3'd5: begin
                if (z_mantissa[23] == 1'b0) begin
                    z_exponent <= z_exponent - 1;
                    z_mantissa <= {z_mantissa[22:0], guard_bit};
                    guard_bit  <= round_bit;
                    round_bit  <= 1'b0;
                    sticky     <= sticky | round_bit;
                end
                counter <= 3'd6;
            end

            // ----------------------------------------------------------
            // 6 : round to nearest even
            // ----------------------------------------------------------
            3'd6: begin
                if (guard_bit && (round_bit || sticky || z_mantissa[0])) begin
                    z_mantissa <= z_mantissa + 1;
                    if (z_mantissa == 24'hFFFFFF)
                        z_exponent <= z_exponent + 1;
                end
                counter <= 3'd7;
            end

            // ----------------------------------------------------------
            // 7 : pack the result, handle overflow / underflow
            // ----------------------------------------------------------
            3'd7: begin
                if ($signed(z_exponent) > 127) begin
                    z <= {z_sign, 8'hFF, 23'd0};                 // overflow -> inf
                end else if ($signed(z_exponent) < -126) begin
                    z <= {z_sign, 31'd0};                        // underflow -> zero
                end else if (z_mantissa[23] == 1'b0) begin
                    z <= {z_sign, 31'd0};                        // not normalisable
                end else begin
                    z <= {z_sign, (z_exponent[7:0] + 8'd127), z_mantissa[22:0]};
                end
                counter <= 3'd0;
            end

            default: counter <= 3'd0;

            endcase
        end
    end

endmodule