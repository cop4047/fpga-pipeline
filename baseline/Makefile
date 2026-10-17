# C++ CPU baseline.  Run in WSL/Linux:  make check   then   make bench PIN=2
CXX      ?= g++
CXXFLAGS ?= -std=c++20 -O3 -Wall -Wextra
PIN      ?= -1
PASSES   ?= 50

cpu_baseline: cpu_baseline.cpp
	$(CXX) $(CXXFLAGS) -o $@ $<

vectors/frames.bin vectors/expected.txt: gen_vectors.py ../model/mdp_model.py
	python3 gen_vectors.py

check: cpu_baseline vectors/frames.bin
	./cpu_baseline --check vectors

bench: cpu_baseline vectors/frames.bin
	./cpu_baseline --bench vectors --pin $(PIN) --passes $(PASSES)

cpu_itch50: cpu_itch50.cpp
	$(CXX) $(CXXFLAGS) -o $@ $<

vectors_itch/frames.bin vectors_itch/expected.txt: gen_itch_vectors.py ../model/itch50.py ../model/mdp_model.py
	python3 gen_itch_vectors.py

# ITCH 5.0 / MoldUDP64 version
itch-check: cpu_itch50 vectors_itch/frames.bin
	./cpu_itch50 --check vectors_itch

itch-bench: cpu_itch50 vectors_itch/frames.bin
	./cpu_itch50 --bench vectors_itch --pin $(PIN) --passes $(PASSES)

clean:
	rm -rf cpu_baseline cpu_itch50 vectors vectors_itch

.PHONY: check bench itch-check itch-bench clean
