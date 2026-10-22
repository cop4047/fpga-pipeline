# Fmax sweep: implement the synthesized design at several clock targets and
# record worst setup slack, so the README table can show "WNS at 6.4 / 5 / 4 / 3.3 ns".
#
#   vivado -mode batch -source syn/sweep.tcl -tclargs <part> "6.4 5.0 4.0 3.3"
#
# Run syn/build.tcl first (it writes <outdir>/<top>_synth.dcp).  Same TOP convention as
# build.tcl: default mdp_itch_top (results in syn/out_itch/sweep.csv); `set TOP mdp_top`
# first for the older design (syn/out/sweep.csv).

set part    [expr {[info exists argv] && [llength $argv] > 0 ? [lindex $argv 0] : "xc7k70tfbg676-2"}]
set periods [expr {[info exists argv] && [llength $argv] > 1 ? [lindex $argv 1] : "6.4 5.5 5.0 4.5 4.0"}]

if {![info exists TOP]} { set TOP mdp_itch_top }
if {![info exists ASYNC_CORE]} { set ASYNC_CORE 0 }       ;# same switch as build.tcl; sweeps the network clock only
set outname [expr {$TOP eq "mdp_top" ? "out" : ($ASYNC_CORE ? "out_itch_async" : "out_itch")}]
set root [file normalize [file join [file dirname [info script]] ..]]
set out  [file join $root syn $outname]
set dcp  [file join $out ${TOP}_synth.dcp]

set csv [open [file join $out sweep.csv] w]
puts $csv "part,period_ns,fmax_target_mhz,wns_ns,tns_ns,met"

catch {close_design}   ;# build.tcl leaves its design open in an interactive console

set n [llength $periods]
set i 0
foreach p $periods {
    incr i
    puts "=============== SWEEP $i of $n : period $p ns ==============="
    open_checkpoint $dcp
    create_clock -name clk -period $p [get_ports clk]
    opt_design
    place_design
    phys_opt_design
    route_design
    set path [get_timing_paths -max_paths 1 -setup]
    set wns  [get_property SLACK $path]
    set tns  [get_property TNS [current_design]]
    if {$tns eq ""} { set tns 0.0 }
    puts $csv [format "%s,%s,%.1f,%s,%s,%s" $part $p [expr {1000.0 / $p}] $wns $tns [expr {$wns >= 0 ? "yes" : "no"}]]
    flush $csv
    puts "SWEEP RESULT period=$p ns  WNS=$wns ns  (met: [expr {$wns >= 0 ? {yes} : {no}}])"
    close_design
}
close $csv
puts "sweep written to [file join $out sweep.csv]"