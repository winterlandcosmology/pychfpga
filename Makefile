# Top-level shortcuts for the pychfpga repo.
# Docs and lint run through uv (https://docs.astral.sh/uv/).

UV ?= uv

.PHONY: docs docs-clean lint help

help:
	@echo "Targets:"
	@echo "  make docs       Build HTML documentation (docs/pyproject.toml + uv; output: docs/build/html)"
	@echo "  make docs-clean Remove Sphinx build products and autosummaries"
	@echo "  make lint       Run pylint (errors only) via uv"

docs:
	$(MAKE) -C docs html

docs-clean:
	$(MAKE) -C docs clean

lint:
	$(UV) run --with pylint pylint pychfpga/ --errors-only
