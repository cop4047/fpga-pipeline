# cocotb testbench for the ITCH 5.0 / MoldUDP64 framer (Icarus Verilog)
#   make                       run the framer tests
#   make TOP=pipeline          run the full-pipeline tests (framer + book + order tx)
#   make TOP=pipeline ASYNC=1 [CORE_PERIOD_PS=4000 CORE_PHASE_PS=1370]
#                              the same tests with an unrelated core clock and Gray-coded async FIFOs
#   make TOP=fifo              mdp_async_fifo unit tests (two independent clocks, CDC-aware sim model)
#   make TOP=rtc               mdp_ptp_rtc unit tests (cycle-exact against a Python model)
#   make ITCH_SEEDS=1,2,3,4,5,6 ITCH_FRAMES=4000    longer random run
SIM                ?= icarus
SIM_BUILD          ?= sim_build_$(TOP)
TOPLEVEL_LANG       = verilog
TOP                ?= frame
ASYNC              ?= 0
CORE_PERIOD_PS     ?= 4000
CORE_PHASE_PS      ?= 1370
ifeq ($(TOP),pipeline)
COCOTB_TOPLEVEL     = mdp_itch_top
COCOTB_TEST_MODULES = test_pipeline
else ifeq ($(TOP),fifo)
COCOTB_TOPLEVEL     = mdp_async_fifo
COCOTB_TEST_MODULES = test_async_fifo
else ifeq ($(TOP),rtc)
COCOTB_TOPLEVEL     = mdp_ptp_rtc
COCOTB_TEST_MODULES = test_rtc
else
COCOTB_TOPLEVEL     = mdp_itch_frame
COCOTB_TEST_MODULES = test_framer
endif
export ASYNC
export CORE_PERIOD_PS
export CORE_PHASE_PS

RTL_DIR = $(abspath $(CURDIR)/../rtl)
VERILOG_SOURCES = $(RTL_DIR)/mdp_itch_unit.sv $(RTL_DIR)/mdp_itch_frame.sv
ifeq ($(TOP),pipeline)
VERILOG_SOURCES += $(RTL_DIR)/mdp_fifo.sv $(RTL_DIR)/mdp_itch_book.sv $(RTL_DIR)/mdp_order_tx.sv \
                   $(RTL_DIR)/mdp_sync2.sv $(RTL_DIR)/mdp_async_fifo.sv $(RTL_DIR)/mdp_rst_sync.sv \
                   $(RTL_DIR)/mdp_ptp_rtc.sv $(RTL_DIR)/mdp_lat_stats.sv $(RTL_DIR)/mdp_itch_top.sv
endif
ifeq ($(TOP),fifo)
VERILOG_SOURCES = $(RTL_DIR)/mdp_sync2.sv $(RTL_DIR)/mdp_async_fifo.sv
endif
ifeq ($(TOP),rtc)
VERILOG_SOURCES = $(RTL_DIR)/mdp_ptp_rtc.sv
endif

ifeq ($(SIM),icarus)
COMPILE_ARGS += -g2012
ifeq ($(TOP),pipeline)
COMPILE_ARGS += -Pmdp_itch_top.ASYNC_CORE=$(ASYNC)
endif
# CDC-aware synchroniser model: bits changing inside the sampling aperture resolve at random
ifneq ($(filter 1,$(ASYNC)),)
COMPILE_ARGS += -DMDP_CDC_SIM
endif
ifeq ($(TOP),fifo)
COMPILE_ARGS += -DMDP_CDC_SIM -Pmdp_async_fifo.W=16 -Pmdp_async_fifo.AW=3
endif
endif

export PYTHONPATH := $(CURDIR)/../model:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
