------------
Installation
------------

Clone the repository
====================

.. code-block:: bash

    git lfs install          # once per user
    git clone https://github.com/winterlandcosmology/pychfpga.git
    cd pychfpga
    git lfs pull

Check out the release or branch that was validated for your platform and configuration, for
example:

.. code-block:: bash

    git checkout 2.2.0+crs_ct        # release tag
    git checkout develop/crs_ct      # or a development branch

Tag and branch names are described in :doc:`../development/workflow`.

If a bitstream or ARM firmware file is only a few hundred bytes, git-lfs did not download it. Run
``git lfs pull``.

Install with uv
===============

.. code-block:: bash

    uv sync                    # creates .venv and installs pychfpga in editable mode
    uv run fpga_master --help

Install with venv and pip
=========================

.. code-block:: bash

    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -e .

Optional extras
===============

.. code-block:: bash

    pip install -e ".[qc]"              # hardware QC and firmware tests
    pip install -e ".[ber_dashboard]"   # CRS BER test dashboard (PyQt6)
    pip install -e ".[docs]"            # Sphinx

With uv, use ``uv sync --extra qc`` and so on.

For interactive work:

.. code-block:: bash

    pip install ipython
    pip install jupyter ipykernel   # ipykernel makes this environment available to Jupyter

Install as a dependency of an application
=========================================

Always pin the platform and configuration. Use the git tag (or branch) in the URL:

.. code-block:: bash

    uv add "pychfpga @ git+https://github.com/winterlandcosmology/pychfpga@2.2.0+crs_ct"
    pip install "pychfpga @ git+https://github.com/winterlandcosmology/pychfpga@2.2.0+crs_ct"

Do not pin a bare version number such as ``pychfpga==2.2.0``: it matches every platform's release
of that version. See :doc:`../development/workflow` for details. git-lfs must be installed on the
installing machine.

Verify the installation
=======================

.. code-block:: bash

    python -c "import pychfpga.fpga_array"
    fpga_master --help

Build the documentation
=======================

.. code-block:: bash

    make docs     # output in docs/build/html
