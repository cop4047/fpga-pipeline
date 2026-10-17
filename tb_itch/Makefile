# cocotb testbench for the ITCH 5.0 / MoldUDP64 framer (Icarus Verilog)
#   make                       run the framer tests
#   make TOP=pipeline          run the full-pipeline tests (framer + book + order tx)
#   make ITCH_SEEDS=1,2,3,4,5,6 ITCH_FRAMES=4000    longer random run
SIM                ?= icarus
SIM_BUILD          ?= sim_build_$(TOP)
TOPLEVEL_LANG       = verilog
TOP                ?= frame
ifeq ($(TOP),pipeline)
COCOTB_TOPLEVEL     = mdp_itch_top
COCOTB_TEST_MODULES = test_pipeline
else
COCOTB_TOPLEVEL     = mdp_itch_frame
COCOTB_TEST_MODULES = test_framer
endif

RTL_DIR = $(abspath $(CURDIR)/../rtl)
VERILOG_SOURCES = $(RTL_DIR)/mdp_itch_unit.sv $(RTL_DIR)/mdp_itch_frame.sv
ifeq ($(TOP),pipeline)
VERILOG_SOURCES += $(RTL_DIR)/mdp_fifo.sv $(RTL_DIR)/mdp_itch_book.sv $(RTL_DIR)/mdp_order_tx.sv $(RTL_DIR)/mdp_itch_top.sv
endif

ifeq ($(SIM),icarus)
COMPILE_ARGS += -g2012
endif

export PYTHONPATH := $(CURDIR)/../model:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
