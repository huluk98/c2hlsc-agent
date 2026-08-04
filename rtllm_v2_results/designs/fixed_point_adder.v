module fixed_point_adder #(
    parameter Q = 15,
    parameter N = 32
) (
    input  wire [N-1:0] a,
    input  wire [N-1:0] b,
    output wire [N-1:0] c
);

    reg [N-1:0] res;

    always @(*) begin
        // Default assignment keeps the block fully specified (no latches).
        res = {N{1'b0}};

        if (a[N-1] == b[N-1]) begin
            // Same sign: add magnitudes, keep the common sign.
            res[N-2:0] = a[N-2:0] + b[N-2:0];
            res[N-1]   = a[N-1];
        end else begin
            // Opposite signs: subtract the smaller magnitude from the larger.
            if (a[N-2:0] > b[N-2:0]) begin
                res[N-2:0] = a[N-2:0] - b[N-2:0];
                res[N-1]   = 1'b0;
            end else if (a[N-2:0] < b[N-2:0]) begin
                res[N-2:0] = b[N-2:0] - a[N-2:0];
                // Sign follows b; magnitude is non-zero here, so never -0.
                res[N-1]   = 1'b1;
            end else begin
                // Equal magnitudes, opposite signs: exact zero, always +0.
                res = {N{1'b0}};
            end
        end
    end

    assign c = res;

endmodule