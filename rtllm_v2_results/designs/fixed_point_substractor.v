module fixed_point_subtractor
#(
    parameter Q = 15,
    parameter N = 32
)
(
    input  [N-1:0] a,
    input  [N-1:0] b,
    output [N-1:0] c
);

    // Internal result register (sign-magnitude format)
    reg [N-1:0] res;

    assign c = res;

    always @(*) begin
        if (a[N-1] == b[N-1]) begin
            // Same sign: magnitudes are subtracted, larger magnitude wins the sign
            if (a[N-2:0] >= b[N-2:0]) begin
                res[N-2:0] = a[N-2:0] - b[N-2:0];
                res[N-1]   = a[N-1];
            end
            else begin
                res[N-2:0] = b[N-2:0] - a[N-2:0];
                res[N-1]   = ~a[N-1];
            end
        end
        else begin
            // Different sign: magnitudes are added, sign of a is kept
            res[N-2:0] = a[N-2:0] + b[N-2:0];
            res[N-1]   = a[N-1];
        end

        // Zero is always represented with a positive sign
        if (res[N-2:0] == {(N-1){1'b0}})
            res[N-1] = 1'b0;
    end

endmodule