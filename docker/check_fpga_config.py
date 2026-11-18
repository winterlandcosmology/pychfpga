#!/usr/bin/env python3
"""Check an fpga_master configuration for the Docker deployment before any hardware access.

Usage: check_fpga_config.py <config_path> <config_name>

The ADC delay tables must be saved on the /data mount: without
``fpga.adc_delay_params.delay_table_folder``, set_adc_delays() saves them in the
package folder inside the container, where they are lost with it.
"""

import logging
import os
import sys
import time
from types import SimpleNamespace

from wtl.config import load_yaml_config

from pychfpga.fpga_master import FPGAMaster


LOGGER = logging.getLogger("pychfpga.docker.check_fpga_config")
DATA_MOUNT = "/data"  # compose mounts PYCHFPGA_DATA_DIR here


def delay_table_folder_problem(conf):
    """Return why the delay tables of `conf` would not land on /data, or None if they would."""
    folder = conf.get("fpga", {}).get("adc_delay_params", {}).get("delay_table_folder")
    if not folder:
        return (
            "fpga.adc_delay_params.delay_table_folder is not set, so the ADC delay "
            "tables would be saved inside the container and lost with it. Set it on "
            f"the {DATA_MOUNT} mount, e.g. '{{data_folder}}/adc_delay_tables' as in "
            "config/docker_example.yaml.")

    # Expand the folder exactly as FPGAMaster.start() will, with the fields it sets.
    start_time = time.time()
    fields = SimpleNamespace(
        start_time=start_time,
        run_isotime=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start_time)),
        run_localtime=time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(start_time)),
        corr_name=conf["corr_name"],
        data_folder=None,
        run_name=None,
        run_folder=None)
    fields.data_folder = FPGAMaster.expand_path(fields, conf["data_folder"])
    fields.run_name = FPGAMaster.expand_path(fields, conf["run_name"])
    fields.run_folder = FPGAMaster.expand_path(fields, conf["run_folder"])
    resolved = os.path.normpath(FPGAMaster.expand_path(fields, folder))

    if resolved != DATA_MOUNT and not resolved.startswith(DATA_MOUNT + "/"):
        return (
            f"fpga.adc_delay_params.delay_table_folder '{folder}' resolves to "
            f"'{resolved}', outside the {DATA_MOUNT} mount, so the ADC delay tables "
            "would be lost with the container. Point it (or data_folder) under "
            f"{DATA_MOUNT}, e.g. '{{data_folder}}/adc_delay_tables' with "
            f"data_folder '{DATA_MOUNT}' as in config/docker_example.yaml.")
    return None


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        LOGGER.error("Usage: check_fpga_config.py <config_path> <config_name>")
        return 2

    conf = load_yaml_config(f"{args[0]}:{args[1]}")
    problem = delay_table_folder_problem(conf)
    if problem:
        LOGGER.error("%s", problem)
        return 1
    LOGGER.info("Configuration check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
