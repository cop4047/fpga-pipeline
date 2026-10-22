# Top-level convenience targets.
#   make sim        cocotb regression (Icarus)               ~12 s
#   make lint       Verilator -Wall lint of the RTL
#   make mutation   inject bugs one at a time, check the TB catches them  ~2 min
#   make yosys      open-source synthesis (resource ESTIMATE, not Vivado) ~25 s
#   make vivado     OOC synth+impl, reports in syn/out   (needs Vivado on PATH)
#   make sweep      Fmax sweep                           (needs Vivado, after make vivado)
#   make itch-sim       ITCH 5.0 framer cocotb regression (Icarus)      ~30 s
#   make itch-pipe      full ITCH 5.0 pipeline cocotb regression         ~40 s
#   make itch-lint      Verilator -Wall lint of the whole ITCH design
#   make itch-mutation  inject bugs into the framer, check the TB catches them  ~3 min
#   make itch-book-mutation  same for the book (50 bugs, whole-pipeline tests)  ~12 min
#   make itch-yosys     open-source resource ESTIMATE of the ITCH design
#   make ptp-sim        hardware clock + async FIFO unit tests, then the whole pipeline with one clock
#                       and with two unrelated clocks (CDC-aware simulation model)          ~6 min
#   make ptp-lint       Verilator -Wall lint, both configurations
#   make ptp-mutation   inject bugs into the clock, FIFO, timestamp path and CDC wiring      ~40 min
#   make clean
PART   ?= xc7k70tfbg676-2
PERIOD ?= 6.4

.PHONY: sim lint mutation yosys vivado sweep clean itch-sim itch-pipe itch-lint itch-mutation itch-book-mutation itch-yosys \
        ptp-sim ptp-lint ptp-mutation

sim:
	$(MAKE) -C tb

lint:
	verilator --lint-only -Wall -Wno-DECLFILENAME --top-module mdp_top rtl/*.sv

mutation:
	python3 tb/mutation_test.py

itch-sim:
	$(MAKE) -C tb_itch

itch-pipe:
	$(MAKE) -C tb_itch TOP=pipeline

ITCH_RTL = rtl/mdp_fifo.sv rtl/mdp_sync2.sv rtl/mdp_async_fifo.sv rtl/mdp_rst_sync.sv rtl/mdp_ptp_rtc.sv rtl/mdp_lat_stats.sv \
           rtl/mdp_itch_unit.sv rtl/mdp_itch_frame.sv rtl/mdp_itch_book.sv rtl/mdp_order_tx.sv rtl/mdp_itch_top.sv

itch-lint: ptp-lint

ptp-lint:
	verilator --lint-only -Wall -GASYNC_CORE=0 --top-module mdp_itch_top $(ITCH_RTL)
	verilator --lint-only -Wall -GASYNC_CORE=1 --top-module mdp_itch_top $(ITCH_RTL)

ptp-sim:
	$(MAKE) -C tb_itch TOP=rtc SIM_BUILD=sim_build_rtc
	$(MAKE) -C tb_itch TOP=fifo SIM_BUILD=sim_build_fifo
	$(MAKE) -C tb_itch TOP=pipeline ASYNC=0 SIM_BUILD=sim_build_pipeline
	$(MAKE) -C tb_itch TOP=pipeline ASYNC=1 SIM_BUILD=sim_build_pipeline_async

ptp-mutation:
	python3 tb_itch/mutation_test.py --suite rtc
	python3 tb_itch/mutation_test.py --suite fifo
	python3 tb_itch/mutation_test.py --suite ts-framer
	python3 tb_itch/mutation_test.py --suite ts-pipe
	python3 tb_itch/mutation_test.py --suite cdc-pipe

itch-mutation:
	python3 tb_itch/mutation_test.py

itch-book-mutation:
	python3 tb_itch/mutation_test.py --suite book

itch-yosys:
	mkdir -p syn/out_itch
	yosys -s syn/yosys_itch.ys || yowasp-yosys -s syn/yosys_itch.ys
	yosys -s syn/yosys_itch_async.ys || yowasp-yosys -s syn/yosys_itch_async.ys

yosys:
	mkdir -p syn/out
	yosys -s syn/yosys.ys || yowasp-yosys -s syn/yosys.ys

vivado:
	vivado -mode batch -source syn/build.tcl -tclargs $(PART) $(PERIOD)

sweep:
	vivado -mode batch -source syn/sweep.tcl -tclargs $(PART) "6.4 5.0 4.0 3.3"

clean:
	rm -rf tb/sim_build tb/results.xml tb/__pycache__ model/__pycache__ syn/out tb_itch/sim_build* tb_itch/results.xml tb_itch/results_pipeline.json tb_itch/__pycache__
