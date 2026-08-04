// barrel_shifter: 8-bit barrel shifter built from 2-to-1 muxes.
//
// Each stage performs a LEFT rotate by 4, 2 and 1 positions, selected by
// ctrl[2], ctrl[1] and ctrl[0] respectively.  Bits leaving the top of the
// byte re-enter at the bottom, so no bits are lost and no zero-fill is needed.
// Total rotate amount = ctrl[2]*4 + ctrl[1]*2 + ctrl[0].
//
// Purely combinational: no clock, no reset, zero latency.

module barrel_shifter (
    input  wire [7:0] in,
    input  wire [2:0] ctrl,
    output wire [7:0] out
);

    // Stage 1: rotate left by 4 when ctrl[2] is high
    wire [7:0] stage1;

    mux2X1 u_s1_0 (.in0(in[0]), .in1(in[4]), .sel(ctrl[2]), .out(stage1[0]));
    mux2X1 u_s1_1 (.in0(in[1]), .in1(in[5]), .sel(ctrl[2]), .out(stage1[1]));
    mux2X1 u_s1_2 (.in0(in[2]), .in1(in[6]), .sel(ctrl[2]), .out(stage1[2]));
    mux2X1 u_s1_3 (.in0(in[3]), .in1(in[7]), .sel(ctrl[2]), .out(stage1[3]));
    mux2X1 u_s1_4 (.in0(in[4]), .in1(in[0]), .sel(ctrl[2]), .out(stage1[4]));
    mux2X1 u_s1_5 (.in0(in[5]), .in1(in[1]), .sel(ctrl[2]), .out(stage1[5]));
    mux2X1 u_s1_6 (.in0(in[6]), .in1(in[2]), .sel(ctrl[2]), .out(stage1[6]));
    mux2X1 u_s1_7 (.in0(in[7]), .in1(in[3]), .sel(ctrl[2]), .out(stage1[7]));

    // Stage 2: rotate left by 2 when ctrl[1] is high
    wire [7:0] stage2;

    mux2X1 u_s2_0 (.in0(stage1[0]), .in1(stage1[6]), .sel(ctrl[1]), .out(stage2[0]));
    mux2X1 u_s2_1 (.in0(stage1[1]), .in1(stage1[7]), .sel(ctrl[1]), .out(stage2[1]));
    mux2X1 u_s2_2 (.in0(stage1[2]), .in1(stage1[0]), .sel(ctrl[1]), .out(stage2[2]));
    mux2X1 u_s2_3 (.in0(stage1[3]), .in1(stage1[1]), .sel(ctrl[1]), .out(stage2[3]));
    mux2X1 u_s2_4 (.in0(stage1[4]), .in1(stage1[2]), .sel(ctrl[1]), .out(stage2[4]));
    mux2X1 u_s2_5 (.in0(stage1[5]), .in1(stage1[3]), .sel(ctrl[1]), .out(stage2[5]));
    mux2X1 u_s2_6 (.in0(stage1[6]), .in1(stage1[4]), .sel(ctrl[1]), .out(stage2[6]));
    mux2X1 u_s2_7 (.in0(stage1[7]), .in1(stage1[5]), .sel(ctrl[1]), .out(stage2[7]));

    // Stage 3: rotate left by 1 when ctrl[0] is high
    mux2X1 u_s3_0 (.in0(stage2[0]), .in1(stage2[7]), .sel(ctrl[0]), .out(out[0]));
    mux2X1 u_s3_1 (.in0(stage2[1]), .in1(stage2[0]), .sel(ctrl[0]), .out(out[1]));
    mux2X1 u_s3_2 (.in0(stage2[2]), .in1(stage2[1]), .sel(ctrl[0]), .out(out[2]));
    mux2X1 u_s3_3 (.in0(stage2[3]), .in1(stage2[2]), .sel(ctrl[0]), .out(out[3]));
    mux2X1 u_s3_4 (.in0(stage2[4]), .in1(stage2[3]), .sel(ctrl[0]), .out(out[4]));
    mux2X1 u_s3_5 (.in0(stage2[5]), .in1(stage2[4]), .sel(ctrl[0]), .out(out[5]));
    mux2X1 u_s3_6 (.in0(stage2[6]), .in1(stage2[5]), .sel(ctrl[0]), .out(out[6]));
    mux2X1 u_s3_7 (.in0(stage2[7]), .in1(stage2[6]), .sel(ctrl[0]), .out(out[7]));

endmodule


// 2-to-1 multiplexer: out = sel ? in1 : in0
module mux2X1 (
    input  wire in0,
    input  wire in1,
    input  wire sel,
    output wire out
);

    assign out = sel ? in1 : in0;

endmodule