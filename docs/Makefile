# Minimal makefile for Sphinx documentation
#

# You can set these variables from the command line.
SPHINXOPTS    =
UV            ?= uv
SPHINXBUILD   ?= $(UV) run sphinx-build
SPHINXPROJ    = pychfpga
SOURCEDIR     = source
BUILDDIR      = build

# User-friendly check for uv (creates/syncs .venv from pyproject.toml via uv run)
ifeq ($(shell which $(UV) >/dev/null 2>&1; echo $$?), 1)
$(error The '$(UV)' command was not found. Install uv (https://docs.astral.sh/uv/) or set UV to its full path.)
endif

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile

clean:
	rm -rf  source/_autosummaries
	@$(SPHINXBUILD) -M clean "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# Catch-all target: route all unknown targets to Sphinx using the new
# "make mode" option.  $(O) is meant as a shortcut for $(SPHINXOPTS).
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)