IVERILOG ?= iverilog
VVP      ?= vvp
GTKWAVE  ?= gtkwave
PYTHON   ?= python3

BUILD_DIR := build
SIMULATOR := $(BUILD_DIR)/simv
RTL       := rtl/one_cycle_delayed_adder.sv
TB        := tb/tb_one_cycle_delayed_adder.sv
WAVEJSON  := docs/expected_waveform.json
WAVEHTML  := docs/expected_waveform.html
WAVESVG   := docs/expected_waveform.svg

.PHONY: all sim wave docs check-env serve clean

all: sim docs

check-env:
	@./scripts/check_environment.sh

$(BUILD_DIR):
	@mkdir -p $(BUILD_DIR)

$(SIMULATOR): $(RTL) $(TB) | $(BUILD_DIR)
	@command -v $(IVERILOG) >/dev/null 2>&1 || { echo "ERROR: iverilog is required for make sim." >&2; exit 1; }
	$(IVERILOG) -g2012 -Wall -s tb_one_cycle_delayed_adder -o $@ $(RTL) $(TB)

sim: $(SIMULATOR)
	@command -v $(VVP) >/dev/null 2>&1 || { echo "ERROR: vvp is required for make sim." >&2; exit 1; }
	$(VVP) $(SIMULATOR)
	@test -s $(BUILD_DIR)/wave.vcd || { echo "ERROR: simulation did not produce a non-empty VCD." >&2; exit 1; }
	@test -s $(BUILD_DIR)/cycle_trace.csv || { echo "ERROR: simulation did not produce a non-empty CSV trace." >&2; exit 1; }

wave: sim
	@command -v $(GTKWAVE) >/dev/null 2>&1 || { echo "ERROR: GTKWave is not installed; install it, then rerun make wave." >&2; exit 1; }
	$(GTKWAVE) $(BUILD_DIR)/wave.vcd docs/one_cycle_delayed_adder.gtkw

docs:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "ERROR: python3 is required to validate the WaveDrom JSON." >&2; exit 1; }
	@$(PYTHON) -m json.tool $(WAVEJSON) >/dev/null
	@if command -v wavedrom-cli >/dev/null 2>&1; then \
		wavedrom-cli -i $(WAVEJSON) -s $(WAVESVG); \
	elif command -v wavedrom >/dev/null 2>&1; then \
		wavedrom -i $(WAVEJSON) -s $(WAVESVG); \
	else \
		echo "WaveDrom CLI not found: WaveJSON validated; open $(WAVEHTML) in a browser to render it."; \
	fi

serve: all
	@printf 'RTL study page: http://127.0.0.1:4173/docs/study.html\n'
	$(PYTHON) -m http.server 4173 --bind 127.0.0.1

clean:
	rm -f $(SIMULATOR) $(BUILD_DIR)/wave.vcd $(BUILD_DIR)/cycle_trace.csv $(WAVESVG)
