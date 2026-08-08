module barrel_shifter (
    input  [7:0] in,
    input  [2:0] ctrl,
    output [7:0] out
);

    // Stage outputs
    wire [7:0] t1;   // after optional shift-right by 4 (ctrl[2])
    wire [7:0] t2;   // after optional shift-right by 2 (ctrl[1])

    // ---------------------------------------------------------------
    // Stage 1: shift right by 4 when ctrl[2] is high
    //          t1[i] = in[i+4], zero fill above
    // ---------------------------------------------------------------
    mux2X1 s1_0 (.in0(in[0]), .in1(in[4]), .sel(ctrl[2]), .out(t1[0]));
    mux2X1 s1_1 (.in0(in[1]), .in1(in[5]), .sel(ctrl[2]), .out(t1[1]));
    mux2X1 s1_2 (.in0(in[2]), .in1(in[6]), .sel(ctrl[2]), .out(t1[2]));
    mux2X1 s1_3 (.in0(in[3]), .in1(in[7]), .sel(ctrl[2]), .out(t1[3]));
    mux2X1 s1_4 (.in0(in[4]), .in1(1'b0),  .sel(ctrl[2]), .out(t1[4]));
    mux2X1 s1_5 (.in0(in[5]), .in1(1'b0),  .sel(ctrl[2]), .out(t1[5]));
    mux2X1 s1_6 (.in0(in[6]), .in1(1'b0),  .sel(ctrl[2]), .out(t1[6]));
    mux2X1 s1_7 (.in0(in[7]), .in1(1'b0),  .sel(ctrl[2]), .out(t1[7]));

    // ---------------------------------------------------------------
    // Stage 2: shift right by 2 when ctrl[1] is high
    //          t2[i] = t1[i+2], zero fill above
    // ---------------------------------------------------------------
    mux2X1 s2_0 (.in0(t1[0]), .in1(t1[2]), .sel(ctrl[1]), .out(t2[0]));
    mux2X1 s2_1 (.in0(t1[1]), .in1(t1[3]), .sel(ctrl[1]), .out(t2[1]));
    mux2X1 s2_2 (.in0(t1[2]), .in1(t1[4]), .sel(ctrl[1]), .out(t2[2]));
    mux2X1 s2_3 (.in0(t1[3]), .in1(t1[5]), .sel(ctrl[1]), .out(t2[3]));
    mux2X1 s2_4 (.in0(t1[4]), .in1(t1[6]), .sel(ctrl[1]), .out(t2[4]));
    mux2X1 s2_5 (.in0(t1[5]), .in1(t1[7]), .sel(ctrl[1]), .out(t2[5]));
    mux2X1 s2_6 (.in0(t1[6]), .in1(1'b0),  .sel(ctrl[1]), .out(t2[6]));
    mux2X1 s2_7 (.in0(t1[7]), .in1(1'b0),  .sel(ctrl[1]), .out(t2[7]));

    // ---------------------------------------------------------------
    // Stage 3: shift right by 1 when ctrl[0] is high
    //          out[i] = t2[i+1], zero fill above
    // ---------------------------------------------------------------
    mux2X1 s3_0 (.in0(t2[0]), .in1(t2[1]), .sel(ctrl[0]), .out(out[0]));
    mux2X1 s3_1 (.in0(t2[1]), .in1(t2[2]), .sel(ctrl[0]), .out(out[1]));
    mux2X1 s3_2 (.in0(t2[2]), .in1(t2[3]), .sel(ctrl[0]), .out(out[2]));
    mux2X1 s3_3 (.in0(t2[3]), .in1(t2[4]), .sel(ctrl[0]), .out(out[3]));
    mux2X1 s3_4 (.in0(t2[4]), .in1(t2[5]), .sel(ctrl[0]), .out(out[4]));
    mux2X1 s3_5 (.in0(t2[5]), .in1(t2[6]), .sel(ctrl[0]), .out(out[5]));
    mux2X1 s3_6 (.in0(t2[6]), .in1(t2[7]), .sel(ctrl[0]), .out(out[6]));
    mux2X1 s3_7 (.in0(t2[7]), .in1(1'b0),  .sel(ctrl[0]), .out(out[7]));

endmodule

module mux2X1 (
    input  in0,
    input  in1,
    input  sel,
    output out
);
    assign out = sel ? in1 : in0;
endmodule
