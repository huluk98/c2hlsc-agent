#!/usr/bin/env bash
set -u

missing_required=0

check_required() {
    if command -v "$1" >/dev/null 2>&1; then
        printf 'FOUND    %-14s %s\n' "$1" "$(command -v "$1")"
    else
        printf 'MISSING  %-14s required for %s\n' "$1" "$2" >&2
        missing_required=1
    fi
}

check_optional() {
    if command -v "$1" >/dev/null 2>&1; then
        printf 'FOUND    %-14s %s\n' "$1" "$(command -v "$1")"
    else
        printf 'OPTIONAL %-14s not found (%s)\n' "$1" "$2"
    fi
}

check_required iverilog "SystemVerilog compilation"
check_required vvp      "running the compiled simulation"
check_required python3  "validating the WaveDrom JSON"
check_required verilator "strict RTL lint in make verify"
check_required yosys     "generic RTL synthesis in make verify"
check_required node      "checking study-page JavaScript in make docs"
check_optional gtkwave  "make wave cannot open the VCD without it"
check_optional wavedrom-cli "make docs will use HTML instead"
check_optional wavedrom     "make docs will use HTML instead"

if [[ "$missing_required" -ne 0 ]]; then
    printf 'Environment check failed: install the missing required command(s).\n' >&2
    exit 1
fi

printf 'Environment check passed.\n'
