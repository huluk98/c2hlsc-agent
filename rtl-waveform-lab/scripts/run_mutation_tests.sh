#!/usr/bin/env bash
set -u

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mutation_dir=$(mktemp -d /tmp/rtl-mutants.XXXXXX)
trap 'rm -rf -- "${mutation_dir:?}"' EXIT

mkdir -p "$repo_dir/build"

compile_and_expect_failure() {
    name=$1
    source=$2
    executable="$mutation_dir/$name-simv"
    log="$repo_dir/build/mutation-$name.log"

    if ! iverilog -g2012 -Wall \
        -s tb_one_cycle_delayed_adder_exhaustive \
        -o "$executable" "$source" \
        "$repo_dir/tb/tb_one_cycle_delayed_adder_exhaustive.sv"; then
        printf 'ERROR: mutation %s did not compile; the mutation harness is invalid.\n' "$name" >&2
        return 1
    fi

    if vvp "$executable" >"$log" 2>&1; then
        printf 'ERROR: mutation %s survived the black-box oracle. See %s\n' "$name" "$log" >&2
        return 1
    fi

    printf 'PASS: oracle rejected mutation %-18s (%s)\n' "$name" "$log"
}

cat >"$mutation_dir/truncated_add.sv" <<'SV'
module one_cycle_delayed_adder (
    input logic clk, rst, in_valid,
    input logic [7:0] a, b,
    output logic out_valid,
    output logic [8:0] sum
);
    logic pending_valid;
    logic [7:0] pending_sum;
    always_ff @(posedge clk) begin
        if (rst) begin
            pending_valid <= 1'b0;
            pending_sum <= 8'd0;
            out_valid <= 1'b0;
            sum <= 9'd0;
        end else begin
            out_valid <= pending_valid;
            if (pending_valid) sum <= {1'b0, pending_sum};
            pending_valid <= in_valid;
            if (in_valid) pending_sum <= a + b;
        end
    end
endmodule
SV

cat >"$mutation_dir/zero_latency.sv" <<'SV'
module one_cycle_delayed_adder (
    input logic clk, rst, in_valid,
    input logic [7:0] a, b,
    output logic out_valid,
    output logic [8:0] sum
);
    always_ff @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0;
            sum <= 9'd0;
        end else begin
            out_valid <= in_valid;
            if (in_valid) sum <= {1'b0, a} + {1'b0, b};
        end
    end
endmodule
SV

cat >"$mutation_dir/no_flush.sv" <<'SV'
module one_cycle_delayed_adder (
    input logic clk, rst, in_valid,
    input logic [7:0] a, b,
    output logic out_valid,
    output logic [8:0] sum
);
    logic pending_valid = 1'b0;
    logic [8:0] pending_sum = 9'd0;
    always_ff @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0;
            sum <= 9'd0;
        end else begin
            out_valid <= pending_valid;
            if (pending_valid) sum <= pending_sum;
            pending_valid <= in_valid;
            if (in_valid) pending_sum <= {1'b0, a} + {1'b0, b};
        end
    end
endmodule
SV

cat >"$mutation_dir/asynchronous_reset.sv" <<'SV'
module one_cycle_delayed_adder (
    input logic clk, rst, in_valid,
    input logic [7:0] a, b,
    output logic out_valid,
    output logic [8:0] sum
);
    logic pending_valid;
    logic [8:0] pending_sum;
    always_ff @(posedge clk or posedge rst) begin
        if (rst) begin
            pending_valid <= 1'b0;
            pending_sum <= 9'd0;
            out_valid <= 1'b0;
            sum <= 9'd0;
        end else begin
            out_valid <= pending_valid;
            if (pending_valid) sum <= pending_sum;
            pending_valid <= in_valid;
            if (in_valid) pending_sum <= {1'b0, a} + {1'b0, b};
        end
    end
endmodule
SV

cat >"$mutation_dir/reset_requires_invalid.sv" <<'SV'
module one_cycle_delayed_adder (
    input logic clk, rst, in_valid,
    input logic [7:0] a, b,
    output logic out_valid,
    output logic [8:0] sum
);
    logic pending_valid;
    logic [8:0] pending_sum;
    always_ff @(posedge clk) begin
        if (rst && !in_valid) begin
            pending_valid <= 1'b0;
            pending_sum <= 9'd0;
            out_valid <= 1'b0;
            sum <= 9'd0;
        end else begin
            out_valid <= pending_valid;
            if (pending_valid) sum <= pending_sum;
            pending_valid <= in_valid;
            if (in_valid) pending_sum <= {1'b0, a} + {1'b0, b};
        end
    end
endmodule
SV

cat >"$mutation_dir/wrong_operator.sv" <<'SV'
module one_cycle_delayed_adder (
    input logic clk, rst, in_valid,
    input logic [7:0] a, b,
    output logic out_valid,
    output logic [8:0] sum
);
    logic pending_valid;
    logic [8:0] pending_sum;
    always_ff @(posedge clk) begin
        if (rst) begin
            pending_valid <= 1'b0;
            pending_sum <= 9'd0;
            out_valid <= 1'b0;
            sum <= 9'd0;
        end else begin
            out_valid <= pending_valid;
            if (pending_valid) sum <= pending_sum;
            pending_valid <= in_valid;
            if (in_valid) pending_sum <= {1'b0, a} - {1'b0, b};
        end
    end
endmodule
SV

compile_and_expect_failure truncated_add "$mutation_dir/truncated_add.sv" || exit 1
compile_and_expect_failure zero_latency  "$mutation_dir/zero_latency.sv" || exit 1
compile_and_expect_failure no_flush      "$mutation_dir/no_flush.sv" || exit 1
compile_and_expect_failure asynchronous_reset "$mutation_dir/asynchronous_reset.sv" || exit 1
compile_and_expect_failure reset_requires_invalid "$mutation_dir/reset_requires_invalid.sv" || exit 1
compile_and_expect_failure wrong_operator "$mutation_dir/wrong_operator.sv" || exit 1

printf 'PASS: all six deliberate RTL faults were detected.\n'
