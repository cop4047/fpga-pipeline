# Board constraints TEMPLATE -- fill in from your board's schematic / vendor XDC
# once you have chosen hardware (see README, "Phase 5: hardware").
# This repo's mdp_top is the *core* (AXI-Stream in/out, one clock).  On a board
# it sits behind a 10G MAC/PCS (e.g. Alex Forencich's verilog-ethernet) whose
# transceiver clock domain crosses into this core's clock (use the async FIFO
# from your PTP project for that crossing).
#
# create_clock -name clk_156 -period 6.400 [get_ports clk_156_p]
# set_property PACKAGE_PIN <pin> [get_ports clk_156_p]
# set_property IOSTANDARD LVDS   [get_ports clk_156_p]
#
# SFP+ transceiver pins/refclk: take them from the vendor's reference design.
# set_clock_groups -asynchronous -group [get_clocks clk_156] -group [get_clocks <gt_txoutclk>]
