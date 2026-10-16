# cocotb testbench for mdp_top (Icarus Verilog by default; SIM=verilator also works)
#   make            run all tests
#   make WAVES=1    also dump a waveform (sim_build/*.fst / *.vcd)
#   make MDP_SEEDS=1,2,3,4,5,6,7,8 MDP_FRAMES=3000   longer random run
SIM                ?= icarus
TOPLEVEL_LANG       = verilog
COCOTB_TOPLEVEL     = mdp_top
COCOTB_TEST_MODULES = test_mdpipe

RTL_DIR = $(abspath $(CURDIR)/../rtl)
VERILOG_SOURCES = $(RTL_DIR)/mdp_fifo.sv $(RTL_DIR)/mdp_hdr_parse.sv \
                  $(RTL_DIR)/mdp_msg_asm.sv $(RTL_DIR)/mdp_book.sv \
                  $(RTL_DIR)/mdp_order_tx.sv $(RTL_DIR)/mdp_top.sv

ifeq ($(SIM),icarus)
COMPILE_ARGS += -g2012
endif
ifeq ($(SIM),verilator)
EXTRA_ARGS += --trace --trace-structs -Wno-fatal
endif

export PYTHONPATH := $(CURDIR)/../model:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
