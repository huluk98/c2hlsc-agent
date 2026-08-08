// ROM: 256 x 16-bit read-only memory with combinational read port.
// addr selects one of 256 locations; dout follows addr with zero latency.

module ROM (
    input  wire  [7:0]  addr,
    output reg   [15:0] dout
);

    // 256 locations, 16 bits wide
    reg [15:0] mem [0:255];

    integer i;

    // Pre-load the ROM with fixed data.
    // Every location is given a defined value so no read returns X.
    initial begin
        for (i = 0; i < 256; i = i + 1) begin
            mem[i] = 16'h0000;
        end
        mem[0] = 16'hA0A0;
        mem[1] = 16'hB1B1;
        mem[2] = 16'hC2C2;
        mem[3] = 16'hD3D3;
    end

    // Continuous (combinational) read of the addressed location.
    always @(*) begin
        dout = mem[addr];
    end

endmodule
