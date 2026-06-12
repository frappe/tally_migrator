# Tally Migrator - test shortcuts.
#
# Two tiers (see tally_migrator/tests/conftest.py):
#   test-pure   pure, no-Frappe tests under a plain interpreter (fast, no bench).
#   test-bench  the FULL suite on a real site - the only tier that exercises the
#               importers, streaming parser, IDOR guard and opening-balance paths.
#               CI runs this; run it before pushing anything that touches them.
#
# Overridable: make test-bench SITE=mysite BENCH=/path/to/frappe-bench

SITE  ?= dev.localhost
BENCH ?= $(HOME)/frappe-bench

.PHONY: test test-pure test-bench

test: test-bench

test-pure:
	python3 -m tally_migrator.tests.run_pure

test-bench:
	cd $(BENCH) && bench --site $(SITE) run-tests --app tally_migrator
