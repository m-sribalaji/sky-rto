# rto_client - the guts of checkin_core.py, split up so nobody has to
# scroll through a 1300-line file to find the DNS-sniffing code next to
# the update logic next to the Teams notification wiring. checkin_core.py
# at the top of client/ just re-exports the three functions the compiled
# agent binaries actually call.
