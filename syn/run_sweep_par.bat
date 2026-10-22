@echo off
rem Parallel Fmax sweep for the ITCH design: one minimised Vivado window per clock target.
rem   run_sweep_par.bat <run-name>      e.g.  run_sweep_par.bat 02-bram-order-table
rem Use the SAME name you gave build.tcl (set RUN <run-name>).  With no name it uses syn\out_itch.
rem Needs about 4-6 GB RAM per run; edit the list on the "for" line to run fewer.
rem Results: syn\runs\<run-name>\sweep_<period>.csv, sweep_<period>.log, worst_paths_<period>ns.rpt
set VIV=C:/AMDDesignTools/2026.1/Vivado/bin/vivado.bat
set ROOT=C:/Users/alros/Documents/FPGA-Project/fpga-pipeline
if "%~1"=="" (set OUT=%ROOT%/syn/out_itch) else (set OUT=%ROOT%/syn/runs/%~1)
if not exist "%OUT%/mdp_itch_top_synth.dcp" (
  echo No synthesised design in %OUT%.  Run build.tcl first with:  set RUN %~1
  pause
  exit /b 1
)
for %%P in (5.0 4.5 4.0 3.5) do start "sweep %%P" /min cmd /c ""%VIV%" -mode batch -nojournal -log "%OUT%/sweep_%%P.log" -source "%ROOT%/syn/sweep_one.tcl" -tclargs xc7k70tfbg676-2 %%P "%OUT%/mdp_itch_top_synth.dcp" "%OUT%""
echo Started. Check %OUT% for sweep_*.log; a sweep_*.csv appears when each run finishes.
pause
