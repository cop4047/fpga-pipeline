# Out-of-context synthesis + implementation of mdp_top with reports.
#
#   vivado -mode batch -source syn/build.tcl -tclargs <part> <clk_period_ns>
#   e.g.   vivado -mode batch -source syn/build.tcl -tclargs xc7k70tfbg676-2 6.4
#
# Defaults: xc7k70tfbg676-2 (a Kintex-7 that the free Vivado edition supports),
# 6.4 ns (156.25 MHz, the 10G / 64-bit datapath clock).
# NOTE: written without access to Vivado -- run it once, fix anything version
# specific, and keep the working version in git.
#
# Outputs go to syn/out/: timing_summary.rpt, worst_paths.rpt,
# utilization.rpt, utilization_flat.rpt, methodology.rpt, mdp_top_routed.dcp.

set part   [expr {[info exists argv] && [llength $argv] > 0 ? [lindex $argv 0] : "xc7k70tfbg676-2"}]
set period [expr {[info exists argv] && [llength $argv] > 1 ? [lindex $argv 1] : 6.4}]

set root [file normalize [file join [file dirname [info script]] ..]]
set out  [file join $root syn out]
file mkdir $out

set rtl [list mdp_fifo mdp_hdr_parse mdp_msg_asm mdp_book mdp_order_tx mdp_top]
foreach m $rtl { read_verilog -sv [file join $root rtl ${m}.sv] }

# clock constraint generated from the requested period
set xdc [file join $out clk.xdc]
set fh [open $xdc w]
puts $fh "create_clock -name clk -period $period \[get_ports clk\]"
close $fh
read_xdc $xdc

synth_design -top mdp_top -part $part -mode out_of_context -flatten_hierarchy rebuilt
write_checkpoint -force [file join $out mdp_top_synth.dcp]
report_utilization -file [file join $out utilization_synth.rpt]

opt_design
place_design
phys_opt_design
route_design

report_timing_summary -max_paths 10 -file [file join $out timing_summary.rpt]
report_timing -max_paths 5 -sort_by slack -path_type full -file [file join $out worst_paths.rpt]
report_utilization -hierarchical -file [file join $out utilization.rpt]
report_utilization -file [file join $out utilization_flat.rpt]
report_methodology -file [file join $out methodology.rpt]
write_checkpoint -force [file join $out mdp_top_routed.dcp]

set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
puts "RESULT part=$part period_ns=$period WNS_ns=$wns"
