//======================================================================
// float_multi : IEEE-754 single-precision (binary32) multiplier
//
//   clk : clock
//   rst : synchronous, active-high reset
//   a,b : 32-bit IEEE-754 operands
//   z   : 32-bit IEEE-754 product (registered, held between results)
//
// Sequential, counter-sequenced datapath:
//   c0 unpack / sample      c4 extract mantissa + g/r/s
//   c1 special cases        c5 normalise result / denormalise
//   c2 normalise inputs     c6 round to nearest even
//   c3 multiply             c7 pack
// The counter free-runs; a change on a/b restarts the sequence so the
// result is always available a fixed 8 cycles after the operands settle.
//======================================================================

module float_multi (
    input  wire        clk,
    input  wire        rst,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output reg  [31:0] z
);

    // exponents are stored bias-removed, 10-bit two's complement
    localparam [9:0] EXP_M127 = 10'd897;   // -127 (raw exponent field 0)
    localparam [9:0] EXP_M126 = 10'd898;   // -126 (smallest normal exponent)
    localparam [9:0] EXP_P128 = 10'd128;   // +128 (raw exponent field 255)

    reg  [2:0]  counter;
    reg  [23:0] a_mantissa, b_mantissa, z_mantissa;
    reg  [9:0]  a_exponent, b_exponent, z_exponent;
    reg         a_sign, b_sign, z_sign;
    reg  [49:0] product;
    reg         guard_bit, round_bit, sticky;

    // shadow copies of the operands, used to detect a new multiplication
    reg  [31:0] a_reg, b_reg;

    // combinational working variables (blocking assignment only)
    reg  [23:0] m_tmp;
    reg  [9:0]  e_tmp;
    reg         g_tmp, r_tmp, s_tmp;
    integer     i;

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
            a_reg      <= 32'd0;
            b_reg      <= 32'd0;
        end
        //--------------------------------------------------------------
        // c0 : unpack (also entered whenever the operands change)
        //--------------------------------------------------------------
        else if ((counter == 3'd0) || (a != a_reg) || (b != b_reg)) begin
            a_reg      <= a;
            b_reg      <= b;
            a_sign     <= a[31];
            a_exponent <= {2'b00, a[30:23]} - 10'd127;
            a_mantissa <= {1'b0, a[22:0]};
            b_sign     <= b[31];
            b_exponent <= {2'b00, b[30:23]} - 10'd127;
            b_mantissa <= {1'b0, b[22:0]};
            counter    <= 3'd1;
        end
        else begin
            case (counter)

            //----------------------------------------------------------
            // c1 : NaN / infinity / zero
            //----------------------------------------------------------
            3'd1: begin
                if (((a_exponent == EXP_P128) && (a_mantissa != 24'd0)) ||
                    ((b_exponent == EXP_P128) && (b_mantissa != 24'd0))) begin
                    z       <= 32'hFFC00000;          // quiet NaN
                    counter <= 3'd0;
                end
                else if (a_exponent == EXP_P128) begin        // a = +/-inf
                    if ((b_exponent == EXP_M127) && (b_mantissa == 24'd0))
                        z <= 32'hFFC00000;                    // inf * 0
                    else
                        z <= {a_sign ^ b_sign, 8'hFF, 23'd0};
                    counter <= 3'd0;
                end
                else if (b_exponent == EXP_P128) begin        // b = +/-inf
                    if ((a_exponent == EXP_M127) && (a_mantissa == 24'd0))
                        z <= 32'hFFC00000;                    // 0 * inf
                    else
                        z <= {a_sign ^ b_sign, 8'hFF, 23'd0};
                    counter <= 3'd0;
                end
                else if (((a_exponent == EXP_M127) && (a_mantissa == 24'd0)) ||
                         ((b_exponent == EXP_M127) && (b_mantissa == 24'd0))) begin
                    z       <= {a_sign ^ b_sign, 31'd0};      // signed zero
                    counter <= 3'd0;
                end
                else begin
                    counter <= 3'd2;
                end
            end

            //----------------------------------------------------------
            // c2 : insert hidden bit / pre-normalise denormal operands
            //----------------------------------------------------------
            3'd2: begin
                if (a_exponent == EXP_M127) begin
                    m_tmp = a_mantissa;
                    e_tmp = EXP_M126;
                    for (i = 0; i < 24; i = i + 1) begin
                        if (m_tmp[23] == 1'b0) begin
                            m_tmp = m_tmp << 1;
                            e_tmp = e_tmp - 10'd1;
                        end
                    end
                    a_mantissa <= m_tmp;
                    a_exponent <= e_tmp;
                end
                else begin
                    a_mantissa <= {1'b1, a_mantissa[22:0]};
                end

                if (b_exponent == EXP_M127) begin
                    m_tmp = b_mantissa;
                    e_tmp = EXP_M126;
                    for (i = 0; i < 24; i = i + 1) begin
                        if (m_tmp[23] == 1'b0) begin
                            m_tmp = m_tmp << 1;
                            e_tmp = e_tmp - 10'd1;
                        end
                    end
                    b_mantissa <= m_tmp;
                    b_exponent <= e_tmp;
                end
                else begin
                    b_mantissa <= {1'b1, b_mantissa[22:0]};
                end

                counter <= 3'd3;
            end

            //----------------------------------------------------------
            // c3 : multiply mantissas, combine signs and exponents
            //----------------------------------------------------------
            3'd3: begin
                product    <= a_mantissa * b_mantissa * 4;
                z_sign     <= a_sign ^ b_sign;
                z_exponent <= a_exponent + b_exponent + 10'd1;
                counter    <= 3'd4;
            end

            //----------------------------------------------------------
            // c4 : extract mantissa and guard / round / sticky
            //----------------------------------------------------------
            3'd4: begin
                z_mantissa <= product[49:26];
                guard_bit  <= product[25];
                round_bit  <= product[24];
                sticky     <= |product[23:0];
                counter    <= 3'd5;
            end

            //----------------------------------------------------------
            // c5 : normalise; gradual underflow towards a denormal
            //----------------------------------------------------------
            3'd5: begin
                m_tmp = z_mantissa;
                e_tmp = z_exponent;
                g_tmp = guard_bit;
                r_tmp = round_bit;
                s_tmp = sticky;

                if (m_tmp[23] == 1'b0) begin                 // at most one shift
                    m_tmp = {m_tmp[22:0], g_tmp};
                    e_tmp = e_tmp - 10'd1;
                    g_tmp = r_tmp;
                    r_tmp = 1'b0;
                end

                for (i = 0; i < 26; i = i + 1) begin
                    if ($signed(e_tmp) < -126) begin
                        s_tmp = s_tmp | r_tmp;
                        r_tmp = g_tmp;
                        g_tmp = m_tmp[0];
                        m_tmp = m_tmp >> 1;
                        e_tmp = e_tmp + 10'd1;
                    end
                end

                if ($signed(e_tmp) < -126) begin             // total underflow
                    m_tmp = 24'd0;
                    g_tmp = 1'b0;
                    r_tmp = 1'b0;
                    e_tmp = EXP_M126;
                end

                z_mantissa <= m_tmp;
                z_exponent <= e_tmp;
                guard_bit  <= g_tmp;
                round_bit  <= r_tmp;
                sticky     <= s_tmp;
                counter    <= 3'd6;
            end

            //----------------------------------------------------------
            // c6 : round to nearest even
            //----------------------------------------------------------
            3'd6: begin
                if (guard_bit && (round_bit | sticky | z_mantissa[0])) begin
                    z_mantissa <= z_mantissa + 24'd1;
                    if (z_mantissa == 24'hFFFFFF)
                        z_exponent <= z_exponent + 10'd1;
                end
                counter <= 3'd7;
            end

            //----------------------------------------------------------
            // c7 : pack, handling overflow and denormal encoding
            //----------------------------------------------------------
            3'd7: begin
                if ($signed(z_exponent) > 127)
                    z <= {z_sign, 8'hFF, 23'd0};                        // inf
                else if (($signed(z_exponent) == -126) && (z_mantissa[23] == 1'b0))
                    z <= {z_sign, 8'h00, z_mantissa[22:0]};             // denormal
                else
                    z <= {z_sign, z_exponent[7:0] + 8'd127, z_mantissa[22:0]};
                counter <= 3'd0;
            end

            default: counter <= 3'd0;

            endcase
        end
    end

endmodule
