module adder_pipe_64bit #(
    parameter DATA_WIDTH = 64,
    parameter STG_WIDTH  = 16
) (
    input  wire                   clk,
    input  wire                   rst_n,
    input  wire                   i_en,
    input  wire [DATA_WIDTH-1:0]  adda,
    input  wire [DATA_WIDTH-1:0]  addb,
    output wire [DATA_WIDTH:0]    result,
    output wire                   o_en
);

    // Number of pipeline stages (one STG_WIDTH-bit ripple slice per stage)
    localparam N = DATA_WIDTH / STG_WIDTH;

    // ---------------------------------------------------------------
    // Pipeline registers
    //   a_r/b_r : remaining operand bits, shifted right one slice per stage
    //   s_r     : accumulated sum, shifted right one slice per stage so the
    //             first slice ends up in the LSBs after N stages
    //   c_r     : carry out of the slice computed in that stage
    //   e_r     : enable pipeline
    // ---------------------------------------------------------------
    reg [DATA_WIDTH-1:0] a_r [0:N-1];
    reg [DATA_WIDTH-1:0] b_r [0:N-1];
    reg [DATA_WIDTH-1:0] s_r [0:N-1];
    reg                  c_r [0:N-1];
    reg                  e_r [0:N-1];

    genvar g;
    generate
        for (g = 0; g < N; g = g + 1) begin : stage
            wire [DATA_WIDTH-1:0] a_s = (g == 0) ? adda                 : a_r[g-1];
            wire [DATA_WIDTH-1:0] b_s = (g == 0) ? addb                 : b_r[g-1];
            wire [DATA_WIDTH-1:0] s_s = (g == 0) ? {DATA_WIDTH{1'b0}}   : s_r[g-1];
            wire                  c_s = (g == 0) ? 1'b0                 : c_r[g-1];
            wire                  e_s = (g == 0) ? i_en                 : e_r[g-1];

            // STG_WIDTH-bit slice add with incoming carry
            wire [STG_WIDTH:0] sum_s = {1'b0, a_s[STG_WIDTH-1:0]} +
                                       {1'b0, b_s[STG_WIDTH-1:0]} +
                                       {{STG_WIDTH{1'b0}}, c_s};

            always @(posedge clk or negedge rst_n) begin
                if (!rst_n) begin
                    a_r[g] <= {DATA_WIDTH{1'b0}};
                    b_r[g] <= {DATA_WIDTH{1'b0}};
                    s_r[g] <= {DATA_WIDTH{1'b0}};
                    c_r[g] <= 1'b0;
                    e_r[g] <= 1'b0;
                end else begin
                    a_r[g] <= {{STG_WIDTH{1'b0}}, a_s[DATA_WIDTH-1:STG_WIDTH]};
                    b_r[g] <= {{STG_WIDTH{1'b0}}, b_s[DATA_WIDTH-1:STG_WIDTH]};
                    s_r[g] <= {sum_s[STG_WIDTH-1:0], s_s[DATA_WIDTH-1:STG_WIDTH]};
                    c_r[g] <= sum_s[STG_WIDTH];
                    e_r[g] <= e_s;
                end
            end
        end
    endgenerate

    // Final carry out concatenated with the assembled sum
    assign result = {c_r[N-1], s_r[N-1]};
    assign o_en   = e_r[N-1];

endmodule