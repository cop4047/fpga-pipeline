# One point of the Fmax sweep, run by sweep_par.tcl in its own batch Vivado process.
#   vivado -mode batch -source sweep_one.tcl -tclargs <part> <period_ns> <synth.dcp> <outdir>
# Writes <outdir>/sweep_<period>.csv (one line) and <outdir>/worst_paths_<period>ns.rpt.
lassign $argv part p dcp out
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
report_timing -max_paths 5 -sort_by slack -path_type full -file [file join $out worst_paths_${p}ns.rpt]
set fh [open [file join $out sweep_$p.csv.tmp] w]
puts $fh [format "%s,%s,%.1f,%s,%s,%s" $part $p [expr {1000.0 / $p}] $wns $tns [expr {$wns >= 0 ? "yes" : "no"}]]
close $fh
file rename -force [file join $out sweep_$p.csv.tmp] [file join $out sweep_$p.csv]
puts "SWEEP RESULT period=$p ns  WNS=$wns ns"
