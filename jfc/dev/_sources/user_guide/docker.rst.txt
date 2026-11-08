------
Docker
------

The Docker image contains ``pychfpga`` and its dependencies, the FPGA bitstreams and the CRS ARM
firmware. It does not contain the IceBoard SD card images. It works for IceBoard and CRS arrays.

Build an image
==============

From a checkout where ``git lfs pull`` has been run:

.. code-block:: bash

    docker build --target deploy -t pychfpga:local .

or, using the compose file:

.. code-block:: bash

    PYCHFPGA_IMAGE=pychfpga:local docker compose build

The build fails if a bitstream or firmware file is still a git-lfs pointer.

Run the servers
===============

Boards are discovered with broadcast (CRS) and multicast (IceBoard mDNS), which do not cross
Docker port mapping. Always use host networking:

.. code-block:: bash

    docker run --rm --network host \
        -v /path/to/config.yaml:/config/fpga_config.yaml:ro \
        -v /path/to/data:/data \
        pychfpga:local fpga_master /config/fpga_config.yaml:my_array

With ``docker compose``, the ``fpga_master`` service in ``docker-compose.yaml`` already uses host
networking. Set these variables before ``docker compose up fpga_master``:

``PYCHFPGA_IMAGE``
    Image to run, e.g. ``wtlpychfpga/pychfpga:2.2.0-crs_ct``.

``PYCHFPGA_CONFIG_FILE``
    Host path of the YAML configuration.

``PYCHFPGA_CONFIG_NAME``
    Configuration section to use.

``PYCHFPGA_DATA_FOLDER``
    Host folder mounted as ``/data``.

The available commands are ``fpga_master``, ``raw_acq``, ``crs_discovery``, ``pychfpga_ps`` and
``pychfpga_gps``.

Upgrade CRS U-Boot from the container
=====================================

The CRS U-Boot image and its upgrade tool are in ``/opt/pychfpga/crs_arm_firmware``:

.. code-block:: bash

    docker run --rm -it --network host pychfpga:local \
        python /opt/pychfpga/crs_arm_firmware/fwupgrade.py --help

See :doc:`../platforms/crs/index` for the target syntax.

Release images (maintainers)
============================

Release images are pushed to Docker Hub as ``wtlpychfpga/pychfpga:<tag>`` by the
``docker_build`` script at the root of the repository. Docker tags cannot contain ``+``, so
release ``2.2.0+crs_ct`` is pushed as ``2.2.0-crs_ct``.

The stored registry write token has been revoked. Log in manually before building:

.. code-block:: bash

    docker login -u wtlpychfpga
    ./docker_build

``docker_build`` only runs on a clean working tree whose ``HEAD`` is a release tag, and refuses
to push if you are not logged in.
