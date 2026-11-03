Server-based operation
======================

``fpga_master`` is a stand-alone server that initializes and operates
IceBoards from a YAML configuration file. It exposes a web server that
accepts REST commands over HTTP and publishes metrics for Prometheus /
Grafana.

``raw_acq`` is a companion server run in parallel with ``fpga_master``. It
receives, analyzes, and stores raw ADC, FFT, or *firmware* correlator data
that the IceBoards capture periodically and send on the FPGA SFP+ 1G
Ethernet link. It does **not** process real-time post-corner-turn (not yet
correlated) data on the QSFP+ links; those streams go to high-performance
correlator nodes.

You need a YAML configuration file that lists which boards to include and
how to set them up.

Operation
---------

From the ``pychfpga`` folder, in the right Python environment::

    source my_env/bin/activate
    cd pychfpga

Start ``raw_acq`` (raw ADC/FFT/firmware-correlator capture and Prometheus
metrics)::

    ./raw_acq.py

Start ``fpga_master`` (replace the config path and section)::

    ./fpga_master.py path_to_config_file:jfc.drao

Docker
------

A Docker image is available with system and Python dependencies already
installed. Building the image requires an SSH identity that can read this
repository and the other ``wtl`` repositories.

Set ``DOCKER_BUILDKIT=1`` before the build. If the correct identity is
already in ``ssh-agent``::

    export DOCKER_BUILDKIT=1
    export BUILDKIT_PROGRESS=plain
    docker build -f Dockerfile -t chimefrb/pychfpga:latest --ssh github_ssh_key=$SSH_AUTH_SOCK .

Alternatively, pass a private key path::

    docker build -f Dockerfile -t chimefrb/pychfpga:latest --ssh github_ssh_key=/path/to/id_rsa .

See :doc:`ssh_keys` for loading the SSH key into the agent.
