# Top-level shortcuts for the pychfpga repo.

.PHONY: docs docs-clean help

help:
	@echo "Targets:"
	@echo "  make docs       Build HTML documentation (output: docs/build/html)"
	@echo "  make docs-clean Remove Sphinx build products and autosummaries"

docs:
	$(MAKE) -C docs html

docs-clean:
	$(MAKE) -C docs clean
