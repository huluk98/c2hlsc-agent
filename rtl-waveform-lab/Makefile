IVERILOG ?= iverilog
VVP      ?= vvp
GTKWAVE  ?= gtkwave
PYTHON   ?= python3
VERILATOR ?= verilator
YOSYS     ?= yosys

BUILD_DIR := build
SIMULATOR := $(BUILD_DIR)/simv
EXHAUSTIVE_SIMULATOR := $(BUILD_DIR)/simv-exhaustive
RTL       ?= rtl/one_cycle_delayed_adder.sv
TB        := tb/tb_one_cycle_delayed_adder.sv
EXHAUSTIVE_TB := tb/tb_one_cycle_delayed_adder_exhaustive.sv
WAVEJSON  := docs/expected_waveform.json
WAVEHTML  := docs/expected_waveform.html
WAVESVG   := docs/expected_waveform.svg

.PHONY: all sim exhaustive lint synth mutations docs verify wave check-env serve clean

all: sim docs

check-env:
	@./scripts/check_environment.sh

$(BUILD_DIR):
	@mkdir -p $(BUILD_DIR)

$(SIMULATOR): $(RTL) $(TB) | $(BUILD_DIR)
	@command -v $(IVERILOG) >/dev/null 2>&1 || { echo "ERROR: iverilog is required for make sim." >&2; exit 1; }
	$(IVERILOG) -g2012 -Wall -s tb_one_cycle_delayed_adder -o $@ $(RTL) $(TB)

$(EXHAUSTIVE_SIMULATOR): $(RTL) $(EXHAUSTIVE_TB) | $(BUILD_DIR)
	@command -v $(IVERILOG) >/dev/null 2>&1 || { echo "ERROR: iverilog is required for make exhaustive." >&2; exit 1; }
	$(IVERILOG) -g2012 -Wall -s tb_one_cycle_delayed_adder_exhaustive -o $@ $(RTL) $(EXHAUSTIVE_TB)

sim: $(SIMULATOR)
	@command -v $(VVP) >/dev/null 2>&1 || { echo "ERROR: vvp is required for make sim." >&2; exit 1; }
	$(VVP) $(SIMULATOR)
	@test -s $(BUILD_DIR)/wave.vcd || { echo "ERROR: simulation did not produce a non-empty VCD." >&2; exit 1; }
	@test -s $(BUILD_DIR)/cycle_trace.csv || { echo "ERROR: simulation did not produce a non-empty CSV trace." >&2; exit 1; }

exhaustive: $(EXHAUSTIVE_SIMULATOR)
	@command -v $(VVP) >/dev/null 2>&1 || { echo "ERROR: vvp is required for make exhaustive." >&2; exit 1; }
	$(VVP) $(EXHAUSTIVE_SIMULATOR)

lint: | $(BUILD_DIR)
	@command -v $(VERILATOR) >/dev/null 2>&1 || { echo "ERROR: verilator is required for make lint." >&2; exit 1; }
	$(VERILATOR) --lint-only -Wall --top-module one_cycle_delayed_adder $(RTL)

synth: | $(BUILD_DIR)
	@command -v $(YOSYS) >/dev/null 2>&1 || { echo "ERROR: yosys is required for make synth." >&2; exit 1; }
	$(YOSYS) -l $(BUILD_DIR)/synth.log -p 'read_verilog -sv $(RTL); hierarchy -check -top one_cycle_delayed_adder; proc; opt; check -assert; stat; write_json $(BUILD_DIR)/synth.json'
	@test -s $(BUILD_DIR)/synth.log || { echo "ERROR: synthesis did not produce build/synth.log." >&2; exit 1; }
	@test -s $(BUILD_DIR)/synth.json || { echo "ERROR: synthesis did not produce build/synth.json." >&2; exit 1; }

mutations:
	@./scripts/run_mutation_tests.sh

wave: sim
	@command -v $(GTKWAVE) >/dev/null 2>&1 || { echo "ERROR: GTKWave is not installed; install it, then rerun make wave." >&2; exit 1; }
	$(GTKWAVE) $(BUILD_DIR)/wave.vcd docs/one_cycle_delayed_adder.gtkw

docs: sim
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "ERROR: python3 is required to validate the WaveDrom JSON." >&2; exit 1; }
	@$(PYTHON) ./scripts/check_study_data.py
	@if command -v wavedrom-cli >/dev/null 2>&1; then \
		wavedrom-cli -i $(WAVEJSON) -s $(WAVESVG); \
	elif command -v wavedrom >/dev/null 2>&1; then \
		wavedrom -i $(WAVEJSON) -s $(WAVESVG); \
	else \
		echo "WaveDrom CLI not found: WaveJSON validated; open $(WAVEHTML) in a browser to render it."; \
	fi

verify: check-env lint sim exhaustive synth mutations docs
	@printf 'PASS: complete local RTL evidence gate succeeded.\n'

serve: all
	@printf 'RTL study page: http://127.0.0.1:4173/docs/study.html\n'
	$(PYTHON) -m http.server 4173 --bind 127.0.0.1

clean:
	rm -f $(SIMULATOR) $(EXHAUSTIVE_SIMULATOR) $(BUILD_DIR)/wave.vcd $(BUILD_DIR)/cycle_trace.csv $(BUILD_DIR)/synth.log $(BUILD_DIR)/synth.json $(BUILD_DIR)/mutation-*.log $(WAVESVG)
