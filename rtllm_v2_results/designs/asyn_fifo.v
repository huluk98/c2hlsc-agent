module dual_port_RAM #(
    parameter WIDTH = 8,
    parameter DEPTH = 16
)(
    input                          wclk,
    input                          wenc,
    input  [$clog2(DEPTH)-1:0]     waddr,
    input  [WIDTH-1:0]             wdata,
    input                          rclk,
    input                          renc,
    input  [$clog2(DEPTH)-1:0]     raddr,
    output reg [WIDTH-1:0]         rdata
);

    reg [WIDTH-1:0] RAM_MEM [0:DEPTH-1];

    always @(posedge wclk) begin
        if (wenc)
            RAM_MEM[waddr] <= wdata;
    end

    always @(posedge rclk) begin
        if (renc)
            rdata <= RAM_MEM[raddr];
    end

endmodule


module asyn_fifo #(
    parameter WIDTH = 8,
    parameter DEPTH = 16
)(
    input                   wclk,
    input                   rclk,
    input                   wrstn,
    input                   rrstn,
    input                   winc,
    input                   rinc,
    input  [WIDTH-1:0]      wdata,
    output                  wfull,
    output                  rempty,
    output [WIDTH-1:0]      rdata
);

    localparam ADDR_W = $clog2(DEPTH);

    reg  [ADDR_W:0] waddr_bin;
    reg  [ADDR_W:0] raddr_bin;
    reg  [ADDR_W:0] wptr;
    reg  [ADDR_W:0] rptr;

    reg  [ADDR_W:0] wptr_buff;
    reg  [ADDR_W:0] wptr_syn;
    reg  [ADDR_W:0] rptr_buff;
    reg  [ADDR_W:0] rptr_syn;

    wire [ADDR_W:0] waddr_bin_next;
    wire [ADDR_W:0] raddr_bin_next;

    wire            wenc;
    wire            renc;

    wire [ADDR_W-1:0] waddr;
    wire [ADDR_W-1:0] raddr;

    assign wenc = winc & ~wfull;
    assign renc = rinc & ~rempty;

    assign waddr_bin_next = waddr_bin + wenc;
    assign raddr_bin_next = raddr_bin + renc;

    assign waddr = waddr_bin[ADDR_W-1:0];
    assign raddr = raddr_bin[ADDR_W-1:0];

    // ---------------- write domain pointer ----------------
    always @(posedge wclk or negedge wrstn) begin
        if (!wrstn) begin
            waddr_bin <= {(ADDR_W+1){1'b0}};
            wptr      <= {(ADDR_W+1){1'b0}};
        end else begin
            waddr_bin <= waddr_bin_next;
            wptr      <= waddr_bin_next ^ (waddr_bin_next >> 1);
        end
    end

    // ---------------- read domain pointer ----------------
    always @(posedge rclk or negedge rrstn) begin
        if (!rrstn) begin
            raddr_bin <= {(ADDR_W+1){1'b0}};
            rptr      <= {(ADDR_W+1){1'b0}};
        end else begin
            raddr_bin <= raddr_bin_next;
            rptr      <= raddr_bin_next ^ (raddr_bin_next >> 1);
        end
    end

    // -------- write pointer synchronizer (into read domain) --------
    always @(posedge rclk or negedge rrstn) begin
        if (!rrstn) begin
            wptr_buff <= {(ADDR_W+1){1'b0}};
            wptr_syn  <= {(ADDR_W+1){1'b0}};
        end else begin
            wptr_buff <= wptr;
            wptr_syn  <= wptr_buff;
        end
    end

    // -------- read pointer synchronizer (into write domain) --------
    always @(posedge wclk or negedge wrstn) begin
        if (!wrstn) begin
            rptr_buff <= {(ADDR_W+1){1'b0}};
            rptr_syn  <= {(ADDR_W+1){1'b0}};
        end else begin
            rptr_buff <= rptr;
            rptr_syn  <= rptr_buff;
        end
    end

    // ---------------- full / empty ----------------
    assign rempty = (rptr == wptr_syn);
    assign wfull  = (wptr == {~rptr_syn[ADDR_W:ADDR_W-1], rptr_syn[ADDR_W-2:0]});

    // ---------------- storage ----------------
    dual_port_RAM #(
        .WIDTH (WIDTH),
        .DEPTH (DEPTH)
    ) u_dual_port_RAM (
        .wclk  (wclk),
        .wenc  (wenc),
        .waddr (waddr),
        .wdata (wdata),
        .rclk  (rclk),
        .renc  (renc),
        .raddr (raddr),
        .rdata (rdata)
    );

endmodule