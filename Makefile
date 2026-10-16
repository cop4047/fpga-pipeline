# Top-level convenience targets.
#   make sim        cocotb regression (Icarus)               ~12 s
#   make lint       Verilator -Wall lint of the RTL
#   make mutation   inject bugs one at a time, check the TB catches them  ~2 min
#   make yosys      open-source synthesis (resource ESTIMATE, not Vivado) ~25 s
#   make vivado     OOC synth+impl, reports in syn/out   (needs Vivado on PATH)
#   make sweep      Fmax sweep                           (needs Vivado, after make vivado)
#   make clean
PART   ?= xc7k70tfbg676-2
PERIOD ?= 6.4

.PHONY: sim lint mutation yosys vivado sweep clean

sim:
	$(MAKE) -C tb

lint:
	verilator --lint-only -Wall -Wno-DECLFILENAME --top-module mdp_top rtl/*.sv

mutation:
	python3 tb/mutation_test.py

yosys:
	mkdir -p syn/out
	yosys -s syn/yosys.ys || yowasp-yosys -s syn/yosys.ys

vivado:
	vivado -mode batch -source syn/build.tcl -tclargs $(PART) $(PERIOD)

sweep:
	vivado -mode batch -source syn/sweep.tcl -tclargs $(PART) "6.4 5.0 4.0 3.3"

clean:
	rm -rf tb/sim_build tb/results.xml tb/__pycache__ model/__pycache__ syn/out
