"""Run only the pure (no-Frappe) unit tests under a plain interpreter.

A naive ``python3 -m unittest discover`` greets a new contributor with import
errors, because four modules import ``frappe`` (the importers, the streaming
parser, the IDOR guard, the opening-balance idempotency) and cannot load outside
``bench``. This runner loads every ``test_*.py`` and silently skips the
frappe-tier ones, so ``make test-pure`` is green out of the box. Run the full
suite - including those four - with ``make test-bench``.

Usage (from the repo root):
    python3 -m tally_migrator.tests.run_pure
"""
import importlib
import pathlib
import sys
import unittest

_HERE = pathlib.Path(__file__).parent


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    skipped: list[str] = []
    for path in sorted(_HERE.glob("test_*.py")):
        module = f"tally_migrator.tests.{path.stem}"
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            # frappe/erpnext absent under a bare interpreter is expected - these are
            # the frappe-tier modules; defer them to `make test-bench`.
            if exc.name in ("frappe", "erpnext"):
                skipped.append(path.stem)
                continue
            raise
        suite.addTests(loader.loadTestsFromModule(mod))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if skipped:
        print(
            f"\nSkipped {len(skipped)} frappe-tier module(s) - run them with "
            f"`make test-bench`: {', '.join(skipped)}"
        )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
