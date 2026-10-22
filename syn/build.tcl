# Out-of-context synthesis + implementation with reports.
#
#   Tcl console (what the author uses):
#       cd C:/Users/alros/Documents/FPGA-Project/fpga-pipeline/syn
#       source build.tcl                 ;# ITCH 5.0 design (mdp_itch_top), results in syn/out_itch
#       set TOP mdp_top; source build.tcl  ;# the older ITCH-lite design, results in syn/out
#       set ASYNC_CORE 1; source build.tcl ;# two-clock build: book on clk_core, Gray-coded async FIFOs,
#                                          ;# results in syn/out_itch_async (see syn/cdc.xdc; UNTESTED in Vivado)
#       set CORE_PERIOD 4.0                ;# core clock period for the two-clock build (default 4.0 ns)
#   Batch:
#       vivado -mode batch -source syn/build.tcl -tclargs <part> <clk_period_ns>
#   e.g.   vivado -mode batch -source syn/build.tcl -tclargs xc7k70tfbg676-2 6.4
#
# Defaults: xc7k70tfbg676-2 (a Kintex-7 that the free Vivado edition supports),
# 6.4 ns (156.25 MHz, the 10G / 64-bit datapath clock).
# NOTE: written without access to Vivado -- run it once, fix anything version
# specific, and keep the working version in git.
#
# Outputs go to syn/out/: timing_summary.rpt, worst_paths.rpt,
# utilization.rpt, utilization_flat.rpt, methodology.rpt, <top>_routed.dcp.

set part   [expr {[info exists argv] && [llength $argv] > 0 ? [lindex $argv 0] : "xc7k70tfbg676-2"}]
set period [expr {[info exists argv] && [llength $argv] > 1 ? [lindex $argv 1] : 6.4}]

# which design: set TOP before sourcing (default mdp_itch_top)
if {![info exists TOP]} { set TOP mdp_itch_top }
if {![info exists ASYNC_CORE]} { set ASYNC_CORE 0 }
if {![info exists CORE_PERIOD]} { set CORE_PERIOD 4.0 }
if {$TOP eq "mdp_top"} {
    set outname out
    set rtl [list mdp_fifo mdp_hdr_parse mdp_msg_asm mdp_book mdp_order_tx mdp_top]
} else {
    set outname [expr {$ASYNC_CORE ? "out_itch_async" : "out_itch"}]
    set rtl [list mdp_fifo mdp_sync2 mdp_async_fifo mdp_rst_sync mdp_ptp_rtc mdp_lat_stats \
                  mdp_itch_unit mdp_itch_frame mdp_itch_book mdp_order_tx mdp_itch_top]
}

set root [file normalize [file join [file dirname [info script]] ..]]
# Named run: `set RUN 02-bram-order-table` before sourcing puts everything in syn/runs/<RUN>/ so no run
# overwrites another.  Without RUN the old folders (out_itch, out_itch_async, out) are used.
if {[info exists RUN]} { set outname "runs/$RUN" }
set out  [file join $root syn $outname]
file mkdir $out
# which source version made these results
set commit "unknown"; set dirty "unknown"
catch { set commit [string trim [exec git -C $root rev-parse --short HEAD]] }
catch { if {[string trim [exec git -C $root status --porcelain -- rtl]] eq ""} { set dirty clean } else { set dirty "UNCOMMITTED CHANGES IN rtl/" } }
set fh [open [file join $out info.txt] w]
set runname $outname; if {[info exists RUN]} { set runname $RUN }
puts $fh "run:        $runname"
puts $fh "date:       [clock format [clock seconds] -format {%Y-%m-%d %H:%M}]"
puts $fh "git commit: $commit   (rtl: $dirty)"
puts $fh "top:        $TOP   ASYNC_CORE=$ASYNC_CORE   part=$part   period=$period ns   core_period=$CORE_PERIOD ns"
close $fh

foreach m $rtl { read_verilog -sv [file join $root rtl ${m}.sv] }

# clock constraint generated from the requested period
set xdc [file join $out clk.xdc]
set fh [open $xdc w]
puts $fh "create_clock -name clk -period $period \[get_ports clk\]"
if {$ASYNC_CORE} {
    puts $fh "create_clock -name clk_core -period $CORE_PERIOD \[get_ports clk_core\]"
}
close $fh
read_xdc $xdc
if {$ASYNC_CORE} { read_xdc [file join $root syn cdc.xdc] }

set generics [expr {$ASYNC_CORE ? "-generic ASYNC_CORE=1" : ""}]
synth_design -top $TOP -part $part -mode out_of_context -flatten_hierarchy rebuilt {*}$generics
write_checkpoint -force [file join $out ${TOP}_synth.dcp]
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
write_checkpoint -force [file join $out ${TOP}_routed.dcp]

set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
puts "RESULT top=$TOP part=$part period_ns=$period WNS_ns=$wns"
