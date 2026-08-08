module instr_reg (
    input  wire        clk,
    input  wire        rst,
    input  wire [1:0]  fetch,
    input  wire [7:0]  data,
    output wire [2:0]  ins,
    output wire [4:0]  ad1,
    output wire [7:0]  ad2
);

    // Two 8-bit instruction holding registers:
    //   ins_p1 : instruction fetched from the register source (fetch == 2'b01)
    //   ins_p2 : instruction fetched from the RAM/ROM source  (fetch == 2'b10)
    reg [7:0] ins_p1;
    reg [7:0] ins_p2;

    always @(posedge clk or negedge rst) begin
        if (!rst) begin
            ins_p1 <= 8'b0000_0000;
            ins_p2 <= 8'b0000_0000;
        end
        else begin
            case (fetch)
                2'b01: begin
                    ins_p1 <= data;
                    ins_p2 <= ins_p2;
                end
                2'b10: begin
                    ins_p1 <= ins_p1;
                    ins_p2 <= data;
                end
                default: begin
                    ins_p1 <= ins_p1;
                    ins_p2 <= ins_p2;
                end
            endcase
        end
    end

    // Field extraction: high 3 bits are the opcode, low 5 bits the register
    // address; the second source is passed through in full.
    assign ins = ins_p1[7:5];
    assign ad1 = ins_p1[4:0];
    assign ad2 = ins_p2[7:0];

endmodule
