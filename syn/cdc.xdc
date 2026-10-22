# Clock-domain-crossing constraints for the two-clock build (ASYNC_CORE=1).
#   UNTESTED: written without access to Vivado, together with syn/build.tcl.  After running it,
#   open report_cdc and report_clock_interaction and fix whatever the tool says.
#
# What crosses, and why this is safe:
#   * Gray-coded FIFO pointers, through mdp_sync2 (2 flops, ASYNC_REG): each crossing is a single
#     bit changing per source clock, so it may be received late but never wrong.
#   * FIFO data, written in one clock and read asynchronously in the other from LUTRAM: a word is
#     only read after its write pointer has crossed (>= 2 destination cycles after the write).
#   * cfg_* (quasi-static configuration) and the statistics counters: written/read while idle.
# The right constraint for such paths is a bounded delay, not a false path, so that the router
# cannot stretch a Gray pointer bit until two bits of one pointer arrive a cycle apart.

set_clock_groups -asynchronous -group [get_clocks clk] -group [get_clocks clk_core]

# Gray pointers: bound the skew between source flop and first synchroniser flop
set_max_delay -datapath_only -from [get_cells -hier -filter {NAME =~ *u_msg_fifo/wgray_reg*}]  -to [get_cells -hier -filter {NAME =~ *u_msg_fifo/u_w2r/s1_reg*}] [expr {min([get_property PERIOD [get_clocks clk]], [get_property PERIOD [get_clocks clk_core]])}]
set_max_delay -datapath_only -from [get_cells -hier -filter {NAME =~ *u_msg_fifo/rgray_r_reg*}] -to [get_cells -hier -filter {NAME =~ *u_msg_fifo/u_r2w/s1_reg*}] [expr {min([get_property PERIOD [get_clocks clk]], [get_property PERIOD [get_clocks clk_core]])}]
set_max_delay -datapath_only -from [get_cells -hier -filter {NAME =~ *u_ord_fifo/wgray_reg*}]  -to [get_cells -hier -filter {NAME =~ *u_ord_fifo/u_w2r/s1_reg*}] [expr {min([get_property PERIOD [get_clocks clk]], [get_property PERIOD [get_clocks clk_core]])}]
set_max_delay -datapath_only -from [get_cells -hier -filter {NAME =~ *u_ord_fifo/rgray_r_reg*}] -to [get_cells -hier -filter {NAME =~ *u_ord_fifo/u_r2w/s1_reg*}] [expr {min([get_property PERIOD [get_clocks clk]], [get_property PERIOD [get_clocks clk_core]])}]
