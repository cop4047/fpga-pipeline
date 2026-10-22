# NOT WORKING: exec "&" did not start any Vivado process on Windows (no logs, no CPU). Use run_sweep_par.bat instead.
# Parallel Fmax sweep: one batch Vivado process per clock target, all at once.
# Same inputs as sweep.tcl (run build.tcl first so <outdir>/<top>_synth.dcp exists).
#
#   Tcl console:
#       cd C:/Users/alros/Documents/FPGA-Project/fpga-pipeline/syn
#       source sweep_par.tcl                        ;# default targets "6.4 5.5 5.0 4.5 4.0 3.5"
#       set PERIODS "6.0 5.0 4.0"; source sweep_par.tcl
#       set ASYNC_CORE 1; source sweep_par.tcl      ;# sweeps the network clock of the two-clock build
#
# Needs roughly 4-6 GB RAM per process.  Each process writes sweep_<p>.log in the output folder.
# Written without a Vivado to test against: if launching fails, use the sequential sweep.tcl.
# The console stays busy until every run has finished (the Vivado window may look frozen).
set part "xc7k70tfbg676-2"
if {![info exists PERIODS]} { set PERIODS "6.4 5.5 5.0 4.5 4.0 3.5" }
if {![info exists TOP]} { set TOP mdp_itch_top }
if {![info exists ASYNC_CORE]} { set ASYNC_CORE 0 }
set outname [expr {$TOP eq "mdp_top" ? "out" : ($ASYNC_CORE ? "out_itch_async" : "out_itch")}]
set root [file normalize [file join [file dirname [info script]] ..]]
set out  [file join $root syn $outname]
set dcp  [file join $out ${TOP}_synth.dcp]
set one  [file join $root syn sweep_one.tcl]
if {![file exists $dcp]} { error "no $dcp: run build.tcl first" }

catch {close_design}
set viv [file join $::env(XILINX_VIVADO) bin vivado.bat]
if {![file exists $viv]} { set viv [auto_execok vivado] }
puts "launching [llength $PERIODS] runs with $viv"

foreach p $PERIODS {
    file delete -force [file join $out sweep_$p.csv]
    exec $viv -mode batch -nojournal -log [file join $out sweep_$p.log] \
         -source $one -tclargs $part $p $dcp $out &
}

set t0 [clock seconds]
while {1} {
    set done 0
    foreach p $PERIODS { if {[file exists [file join $out sweep_$p.csv]]} { incr done } }
    puts "[expr {[clock seconds]-$t0}] s: $done of [llength $PERIODS] finished"
    if {$done == [llength $PERIODS]} break
    if {[clock seconds] - $t0 > 10800} { puts "gave up after 3 h; see the sweep_*.log files"; break }
    after 30000
}

set csv [open [file join $out sweep.csv] w]
puts $csv "part,period_ns,fmax_target_mhz,wns_ns,tns_ns,met"
foreach p $PERIODS {
    set f [file join $out sweep_$p.csv]
    if {[file exists $f]} { set fh [open $f]; puts -nonewline $csv [read $fh]; close $fh }
}
close $csv
puts "sweep written to [file join $out sweep.csv]"
