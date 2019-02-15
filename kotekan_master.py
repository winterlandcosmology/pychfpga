#!/usr/bin/env python

"""
RESTful Server & Client Module for KotekanMaster to initialize,
manage and operate the CHIME GPU Backend.
"""

# Standard Imports
import os
import time
import json
import hashlib
import requests
import numpy as np

# Custom Imports
import log
from pychfpga import NameSpace, load_yaml_config
from kotekan import KotekanAsyncRESTClient
from rest import AsyncRESTClient, AsyncRESTServer, endpoint
from rest import coroutine, coroutine_return, sleep

# Import Slack Client
from slack import SlackClient

# REST Server/Client
from rest import RunSyncWrapper, run_client

# If a node is desynced for this many watchdog cycles, it will get restarted.
DESYNC_LIMIT = 2


# KotekanMaster Class
class KotekanMaster(object):
    """
    KotekanMaster class to manage individual Kotekan Objects which interact
    with the CHIME GPU nodes over RESTful API.
    """

    # Logging setup when no config file is provided.
    DEFAULT_LOGGING = {
        "handlers": {
            "stderr": {"class": "logging.StreamHandler", "level": "INFO"}
        },
        "loggers": {"": {"handlers": ["stderr"]}},
    }

    def __init__(self):
        # Default Logger
        log.setup_logging(self.DEFAULT_LOGGING)
        self.log = log.get_logger(self)
        self.log.info("%r : Creating KotekanMaster Class Instance", self)

        # Logging Parameters -- Absolute path name to this module
        self.program = os.path.realpath(__file__)

        # Tracked KotekanMaster Parameters
        # Valid state parameters: 'on', 'off', 'stopping'
        self.state = "off"
        self.start_time = None
        self.startup_config = None
        self.current_config = None

        # KotekanAsyncRESTClient instances which are managed by KotekanMaster
        # Format {{node_name: KotekanMasterObj}}
        self.nodes = {}
        self.receiver_nodes = {}
        # KotekanAsyncRESTClient instances blacklisted
        self.blacklist_nodes = []
        # Connection error list contains of tuples with the format:
        # (time.ctime(), {"node_name": "HTTPError"})
        self.connection_error_nodes = []

        # Watchdog parameters
        self.watchdog_enabled = False
        self.watchdog_interval = 240
        self.watchdog_cycles = 0

        self.update_moratorium = True
        # Watchdog Statistics Format
        # { node_name }
        self.watchdog_stats = {}

        # GPS Parameters
        self.gps_status = {}
        self.gps_server = "http://carillon.chime:54321/get-frame0-time"
        self.gps_time = {}

        # Synchronization Parameters
        self.kotekan_versions = {}
        # True all nodes in the array are running the same config
        self.array_sync = False
        # True if nodes are running the same config as the one
        # tracked by KotekanMaster
        self.km_sync = False
        # Restart List
        self.out_of_sync_cycles = 0
        self.out_of_sync_nodes = []
        self.node_md5sums = None
        self.unique_md5sums = {}

        # Startup Parameters
        self.isotime = None
        self.localtime = None
        self.run_name = None
        self.run_folder = None
        self.current_folder = None
        self.logging_handlers = None

        # FRB Update Times
        self.frb_gains_dir_update_time = "Never"
        self.ew_spacing_update_time = "Never"
        self.ns_extent_update_time = "Never"

        # Pulsar Update Times
        self.pulsar_update_time = "Never"
        self.pulsar_gains_dirs_update_time = "Never"

        # Cosmology Status Paramters

        # Calibration Broker Parameters
        self.cal_broker_gain_tag = None
        self.cal_broker_gain_update_time = None
        self.correlator_bad_inputs = None
        self.cylinder_bad_inputs = None
        self.bad_inputs_tag = None
        self.bat_inputs_update_time = None

        # Revision Control Logging
        self.git_version = "2018.12"
        self.log.info("%s : Program : %s", self, self.program)
        self.log.info("%s : Git Ver : %s", self, self.git_version)
        self.slack = SlackClient(
            SLACK_TOKEN_NAME="SLACK_API_TOKEN", module_name="KotekanMaster"
        )
        self.pulsar_slack = SlackClient(
            SLACK_TOKEN_NAME="PULSAR_SLACK_API_TOKEN",
            module_name="KotekanMaster",
        )

    # Helper Methods
    #   NOTE: These methods are not coroutines!
    def set_config(self, config):
        """
        Convert list or dict to an object with attributes
        """
        self.current_config = NameSpace(config)

    @coroutine
    def _get_gps_time(self, slack_broadcast=True, update_config=False):
        """
        Get gps time for chime master to sync the kotekan nodes

        Parameters
        ----------
            slack_broadcast: boolean
                Post updates to slack
            update_config : boolean
                Update the local copy of kotekan config, which is posted to all
                nodes and is used for checksum validation.
                NOTE: update_config should only be called during start and
                restart process only.
        Returns
        -------
            result : string
                PASSED -- All good!
                FAILED -- All not good!
        """
        self.log.info("%s : Retreiving GPS Time ...", self)
        if slack_broadcast:
            self.slack.info(
                msg_title="Retreiving GPS Time ...",
                msg=self.gps_server,
                as_inline_code=True,
            )
        try:
            gps_request = requests.get(self.gps_server)
            # Check if the request worked out.
            gps_request.raise_for_status()
            # Change the GPS Time in KotekanMaster Status
            self.gps_time = gps_request.json()
            # Append GPS time to the current kotekan config.
            if update_config:
                self.current_config.common_config.gps_time = self.gps_time
        except requests.exceptions.RequestException as error:
            msg = str(error)
            self.slack.error(
                msg_title="Unable to retreive GPS Time",
                msg=msg,
                as_inline_code=True,
            )
            self.log.error("%s : Unable to retreive GPS Time: %s", self, error)
            coroutine_return(result="FAILED")

        # Check for the corner case when gps_server is booting up and
        # returns an empty dict
        if self.gps_time == {}:
            msg = ("gps_server: (%s) returned empty dict.", self.gps_server)
            self.log.error(msg)
            self.slack.error(
                msg_title="Unable to retreive GPS Time",
                msg=msg,
                as_inline_code=True,
            )
            coroutine_return(result="FAILED")
        else:
            self.log.info("%s : successfully retrieved gps_time", self)
            if slack_broadcast:
                self.slack.info(
                    msg_title="Successfully retrieved GPS Time",
                    msg=json.dumps(self.gps_time),
                    as_inline_code=True,
                )
        coroutine_return(result="PASSED")

    # KotekanMaster Methods
    @coroutine
    def start_kotekan_master(self, config):
        """
        Start KotekanMaster and create a KotekanAsyncRESTClient for each node
        specified in the config file.
        """
        # Start KotekanMaster if the current state is off.
        if self.state == "off":
            self.log.info("%s : KotekanMaster server starting ...", self)
            self.slack.info(msg_title="KotekanMaster Startup Initiated")
            self.start_time = time.time()
            self.isotime = time.strftime(
                "%Y%m%dT%H%M%SZ", time.gmtime(self.start_time)
            )
            self.localtime = time.strftime(
                "%Y/%m/%d %H:%M:%S", time.localtime(self.start_time)
            )
            self.log.info("%s : Start Time : %s", self, self.localtime)

            # The startup_config is never changed throughout the operation of
            # KotekanMaster. All dynamic updates to the configuration are
            # applied against a local copy called current_config
            self.startup_config = NameSpace(config)
            self.current_config = self.startup_config

            # Setup logging paths
            self.run_name = self.startup_config.run_name % dict(
                isotime=self.isotime,
                localtime=self.localtime,
                corr_name=self.startup_config.corr_name,
            )
            self.log.info("%s : Run Name : %s", self, self.run_name)
            run_args = dict(
                isotime=self.isotime,
                corr_name=self.startup_config.corr_name,
                run_name=self.run_name,
            )
            self.run_folder = self.startup_config.run_folder % run_args
            self.run_folder = os.path.expanduser(self.run_folder)
            self.log.info("%s : Run Folder : %s", self, self.run_folder)
            self.current_folder = os.path.expanduser(
                self.startup_config.current_folder % run_args
            )
            self.log.info("%s : Current Folder : %s", self, self.current_folder)

            # Create Run Folders for Logging
            try:
                os.makedirs(self.run_folder)
                self.log.info("%s : Run Folder: %s", self, self.run_folder)
                self.slack.info(
                    msg_title="Run Folder",
                    msg=json.dumps(self.run_folder),
                    as_inline_code=True,
                )
            except OSError:
                errmsg = "Could not create directory '%s'!" % self.run_folder
                self.log.critical(errmsg)
                raise RuntimeError(errmsg)

            # Make a symlink to the run folder
            if hasattr(os, "symlink"):
                try:
                    os.remove(self.current_folder)
                except OSError as e:
                    msg = (
                        "%r : Could not remove current symlink '%s'.\
                         The error is \n%s"
                        % (self, self.current_folder, e)
                    )
                    self.log.warn(msg)
                try:
                    os.symlink(self.run_folder, self.current_folder)
                except OSError as e:
                    msg = (
                        "%r : Could not create a symlink '%s' to the run\
                          folder '%s'. The error is:\n%s"
                        % (self, self.current_folder, self.run_folder, e)
                    )
                    self.log.warning(msg)

            # Setting up logging handlers
            self.logging_handlers = log.setup_logging(
                self.startup_config.logging.dict_config,
                self.startup_config.logging.log_levels,
                base_package_name=(
                    self.startup_config.logging.base_package_name
                ),
                actual_package_name=__name__.rpartition(".")[0],
                script_name=self.startup_config.logging.script_name,
                run_folder=self.run_folder,
            )
            self.log.info("%s: Logging Configured.", self)

            # Get gps_time from the self.gps_server
            self.gps_status = yield self._get_gps_time(
                slack_broadcast=True, update_config=True
            )

            # Create a client for each node.
            self.log.info(
                "%s : Creating Kotekan & Receiver Node Clients ...", self
            )
            yield self._create_node_clients()
            self.log.info("%s : Kotekan Clients Created.", self)
            self.slack.info(
                msg_title="Kotekan Nodes",
                msg=json.dumps(self.nodes.keys()),
                as_inline_code=True,
            )
            self.slack.info(
                msg_title="Receiver Nodes",
                msg=json.dumps(self.receiver_nodes.keys()),
                as_inline_code=True,
            )

            # Toggle KotekanMaster State
            self.state = "on"
            self.log.info("%s : KotekanMaster State : %s", self, self.state)

            # Check if we can connect to Kotekan running on the nodes.
            self.log.info("%s : Checking node connection status ", self)
            status = yield self.kotekan_status()
            no_connection_nodes = yield self.check_node_connection(status)
            self.log.warning(
                "%s : Connection Error Nodes: %s", self, no_connection_nodes
            )

            # Check if all kotekan instances are running the same binary.
            self.log.info("%s : Checking kotekan binary versions", self)
            # Get the current running versions
            running_versions = yield self.kotekan_version()
            for node_name, version in running_versions.items():
                if node_name not in no_connection_nodes.keys():
                    self.kotekan_versions[node_name] = version.get(
                        "git_commit_hash"
                    )
            # Find all unique kotekan versions
            unique_versions = {}
            arr_versions = np.asarray(self.kotekan_versions.values())
            for version in set(arr_versions):
                unique_versions[version] = len(
                    arr_versions[arr_versions == version]
                )

            self.slack.info(
                msg_title="Kotekan Versions Discovered",
                msg=str(unique_versions),
                as_inline_code=True,
            )
            # Remove None field from unique_versions
            unique_versions.pop(None, None)
            if len(unique_versions) != 1:
                self.slack.error(
                    msg_title="Kotekan Version Check: FAILED",
                    msg=str(unique_versions),
                    as_inline_code=True,
                )
            else:
                self.slack.info(
                    msg_title="Kotekan Version Check: PASSED",
                    msg=str(unique_versions),
                )

            self.log.info("KotekanMaster Startup Complete.")
            self.slack.info(msg_title="Update Moratorium Enabled")
            self.slack.info(msg_title="KotekanMaster Startup Complete.")
            coroutine_return(result="KotekanMaster Startup Complete")

        # If already running, do nothing.
        if self.state == "on":
            self.log.info("%s : KotekanMaster State : %s", self, self.state)
            coroutine_return(result="KotekanMaster Already Running!")

    @coroutine
    def _create_node_clients(self):
        """
        Create KotekanAsyncRESTClient instances for each GPU node listed in the
        startup_config

        This routine is only called at startup and is not accessible as an
        endpoint.
        """
        self.nodes = {}
        # This actually points to current_config.servers.default_server.nodes
        # in the kotekan_config.yaml
        nodes = self.current_config.nodes or {}
        for node_name, node_params in nodes.items():
            self.nodes[node_name] = KotekanAsyncRESTClient(
                name=node_name, heartbeat_period=5000, **node_params
            )
        self.log.info("%s : Created kotekan clients.", self)

        # Create clients for receiver nodes
        self.receiver_nodes = {}
        receiver_nodes = self.current_config.receiver_nodes or {}
        for node_name, node_params in receiver_nodes.items():
            self.receiver_nodes[node_name] = KotekanAsyncRESTClient(
                name=node_name, heartbeat_period=5000, **node_params
            )
        self.log.info("%s : Created receiver clients.", self)
        coroutine_return(result="PASSED")

    @coroutine
    def stop_kotekan_master(self):
        """
        Stop KotekanMaster and release node clients.
        """
        if self.state == "on":
            self.state = "stopping"
            self.log.info("%s : KotekanMaster State : %s", self, self.state)
            if self.nodes is not None:
                self.nodes = None
            self.current_config = None
            reap_cached_sockets()
            # self.log.stop_logging(self.logging_handlers)

        self.state = "off"
        self.log.info("%s : KotekanMaster State : %s", self, self.state)
        self.slack.info(msg_title="KotekanMaster Stopped.")
        coroutine_return("KotekanMaster Server Stopped.")

    @coroutine
    def status_kotekan_master(self):
        """
        Current status of services KotekanMaster Status
        """
        _config = self.current_config.common_config
        result = {
            "state": self.state,
            "current_config": _config.as_dict(),
            "nodes": {
                "kotekan_nodes": self.nodes.keys(),
                "unique_md5sums": self.unique_md5sums,
                "node_md5sums": self.node_md5sums,
                "receiver_nodes": self.receiver_nodes.keys(),
                "kotekan_version": self.kotekan_versions,
            },
            "blacklist_nodes": self.blacklist_nodes,
            "connection_error_nodes": self.connection_error_nodes,
            "watchdog_status": {
                "watchdog_enabled": self.watchdog_enabled,
                "watchdog_interval": self.watchdog_interval,
                "watchdog_cycles": self.watchdog_cycles,
                "watchdog_stats": self.watchdog_stats,
                "update_moratorium": self.update_moratorium,
                "array_sync": self.array_sync,
                "km_sync": self.km_sync,
            },
            "gps_status": {
                "gps_server": self.gps_server,
                "gps_status": self.gps_status,
                "gps_time": self.gps_time,
            },
            "calibration_broker_status": {
                "calibration_tag": self.cal_broker_gain_tag,
                "calibration_start_time": self.cal_broker_gain_update_time,
                "correlator_bad_inputs": self.correlator_bad_inputs,
                "cylinder_bad_inputs": self.cylinder_bad_inputs,
                "bad_inputs_tag": self.bad_inputs_tag,
                "bad_inputs_update_time": self.bat_inputs_update_time,
            },
            "frb_status": {
                "gains_dir": _config.frb_gain.frb_gain_dir,
                "gains_dir_update_time": self.frb_gains_dir_update_time,
                "ew_spacing": _config.gpu.gpu_0.ew_spacing,
                "ew_spacing_update_time": self.ew_spacing_update_time,
                "ns_extent": _config.gpu.gpu_0.northmost_beam,
                "ns_extent_update_time": self.ns_extent_update_time,
            },
            "pulsar_status": {
                "gain_dirs": _config.pulsar_gain.pulsar_gain_dir,
                "gain_update_time": self.pulsar_gains_dirs_update_time,
                "last_beam_update_time": self.pulsar_update_time,
                "ra": _config.gpu.gpu_0.source_ra,
                "dec": _config.gpu.gpu_0.source_dec,
                "scaling": _config.gpu.gpu_0.psr_scaling,
            },
            "cosmology_status": {
                "rfi_zeroing": str(_config.rfi_masking.toggle.rfi_zeroing),
                "pulsar_gating": str(_config.updatable_config.gating),
            },
            "git_version": self.git_version,
            "start_time": time.strftime(
                "%Y/%m/%d %H:%M:%S", time.localtime(self.start_time)
            ),
        }
        coroutine_return(result)

    @coroutine
    def check_node_connection(self, node_status):
        _connection_error = {}
        # Check for which nodes we cannot connect
        for node_name, status in node_status.items():
            if "running" not in status:
                _connection_error[node_name] = status

        if len(_connection_error) != 0:
            # Update the self.connection_error_nodes parameter with the new
            # nodes that were found not to be working.
            self.connection_error_nodes.append(
                (time.ctime(), _connection_error)
            )
            self.slack.warning(
                msg_title="Connection Error Nodes:",
                msg=json.dumps(_connection_error),
                as_inline_code=True,
            )
        coroutine_return(result=_connection_error)

    @coroutine
    def whitelist_node(self, node_list):
        """
        Whitelist Node
        """
        self.log.info("%s : Whitelist Nodes Executed : %s", self, node_list)
        whitelisted_nodes = []
        for node_name in node_list:
            # Find a node_config or use defaults.
            try:
                node_config = self.current_config.nodes.as_dict()[node_name]
                self.log.info("%s : Found config for node %s", self, node_name)
            except Exception:
                node_config = {"hostname": node_name, "port": 12048}
                self.log.info(
                    "%s : Cannot find config for node %s, using  defaults",
                    self,
                    node_name,
                )
            # Start a Kotekan Client for the node.
            if node_name not in self.nodes.keys():
                self.nodes[node_name] = KotekanAsyncRESTClient(
                    name=node_name, **node_config
                )
                self.log.info("%s : Whitelisted Node: %s", self, node_name)
            # Remove the node from the blacklist
            if node_name in self.blacklist_nodes:
                self.blacklist_nodes.remove(node_name)
                self.log.info(
                    "%s : Removed node %s from Blacklist", self, node_name
                )
            # Keep track of newly whiteliested nodes.
            whitelisted_nodes.append(node_name)
        self.slack.info(
            msg_title="Whitelisted Nodes",
            msg=json.dumps(whitelisted_nodes),
            as_inline_code=True,
        )
        coroutine_return(whitelisted_nodes)

    @coroutine
    def blacklist_node(self, node_list):
        """
        Blacklist Node
        """
        new_blacklist_nodes = []
        self.log.info("%s : Blacklist Nodes Executed : %s", self, node_list)
        for node_name, kotekan in self.nodes.items():
            if node_name in node_list:
                if node_name not in self.blacklist_nodes:
                    self.log.info("%s : Blacklisted Node : %s", self, node_name)
                    yield kotekan.kill()
                    self.blacklist_nodes.append(node_name)
                    self.nodes.pop(node_name)
                    new_blacklist_nodes.append(node_name)
        if len(new_blacklist_nodes) != 0:
            self.slack.info(
                msg_title="Blacklisted Nodes",
                msg=json.dumps(new_blacklist_nodes),
                as_inline_code=True,
            )
        coroutine_return(self.blacklist_nodes)

    # KotekanMaster Watchdog Methods
    @coroutine
    def start_watchdog(self):
        """
        Start Watchdog
        """
        self.watchdog_enabled = True
        self.log.info("%s : KotekanMaster Watchdog Enabled", self)
        self.log.info(
            "%s : KotekanMaster Watchdog Interval : %s seconds",
            self,
            self.watchdog_interval,
        )
        msg = "watchdog interval: {} seconds".format(self.watchdog_interval)
        self.slack.info(msg_title="KotekanMaster Watchdog Enabled", msg=msg)
        coroutine_return(result="KotekanMaster Watchdog Enabled")

    @coroutine
    def stop_watchdog(self):
        """
        Stop Watchdog
        """
        self.watchdog_enabled = False
        self.watchdog_cycles = 0
        self.update_moratorium = True
        self.log.info("%s : KotekanMaster Watchdog Disabled", self)
        self.slack.warning(msg_title="KotekanMaster Watchdog Disabled")
        self.slack.info(msg_title="Update Moratorium Enabled")
        coroutine_return("KotekanMaster Watchdog Disabled")

    @coroutine
    def update_watchdog_stats(self, restart_list):
        """
        Statistics for KotekanMaster Watchdog

        Parameters
        ----------
            restart_list : dict-type

        Returns
        -------
            watchdog_stats : dict-type
                {node_name: number_of_restarts}
        """
        restart_time = time.ctime()
        for node in restart_list:
            if node not in self.watchdog_stats.keys():
                self.watchdog_stats.update({node: []})
            self.watchdog_stats[node].append(restart_time)
        coroutine_return(self.watchdog_stats)

    # KotekanMaster Validation Routines
    @coroutine
    def validate_gps(self):
        """
        Validate GPS Time
        """
        previous_gps_time = self.gps_time
        # Assign GPS status to a placeholder, so that we never return a future
        # when we access self.gps_status
        new_gps_status = yield self._get_gps_time(
            slack_broadcast=False, update_config=False
        )
        self.gps_status = new_gps_status
        try:

            self.log.info("GPS Validation Status")
            self.log.info(
                "Current frame0_ctime: {0}".format(
                    self.gps_time["frame0_ctime"]
                )
            )
            self.log.info(
                "Previous frame0_ctime: {0}".format(
                    previous_gps_time["frame0_ctime"]
                )
            )
            if (
                self.gps_time["frame0_ctime"]
                != previous_gps_time["frame0_ctime"]
            ):
                # frame0_ctime changed! Raise Exception
                raise Exception("frame0_ctime mismatch")
        except Exception as err:
            # Set gps_status to FAILED!
            self.gps_status["result"] = "FAILED"
            # Log / slack things.
            msg = str(err)
            self.log.error("%s : GPS Validation Error: %s", self, msg)
            self.slack.error(
                msg_title="GPS Validation Error", msg=msg, as_inline_code=True
            )
            # Kill kotekan on all nodes immediately
            kill_status = yield self.kotekan_master.kill_kotekan()
            self.log.error(kill_status)
            coroutine_return(result="FAILED")

        # If everything is good, return PASSED
        coroutine_return(result="PASSED")

    @coroutine
    def validate_checksum(self):
        """
        Validate Configuration Checksum
        """
        # Declare state variables
        result = {}
        array_md5_sync = False
        kotekan_master_md5_sync = False
        unique_md5sums = {}
        try:
            self.log.debug("%s : Validating Checksums", self)
            # Get node md5's
            node_md5sums = yield self.kotekan_config_md5sum()
            self.node_md5sums = node_md5sums
            self.log.debug(node_md5sums)

            # Calculate the md5 for kotekanmaster config
            dynamic_config = json.dumps(
                self.current_config.common_config.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
            _md5 = hashlib.md5()
            _md5.update(dynamic_config)
            kotekan_master_md5sum = _md5.hexdigest()
            self.log.debug(kotekan_master_md5sum)
        except Exception as md5_error:
            self.log.error(md5_error)
            coroutine_return(error="Unable to get md5sums")

        # Get unique md5sums
        try:
            self.log.debug("Sanitizing node md5sums")
            # Check all the md5sums and keep track how many times we see it.
            for node in node_md5sums.keys():
                md5sum = node_md5sums.get(node).get("md5sum")
                if md5sum not in unique_md5sums:
                    unique_md5sums[md5sum] = 1
                else:
                    unique_md5sums[md5sum] += 1

                # Inc. desync counter for this node if different from local conf
                if md5sum != kotekan_master_md5sum:
                    try:
                        result[node]['desynced'] = result[node]['desynced'] + 1
                    except KeyError:
                        result[node]['desynced'] = 1

            self.log.info(unique_md5sums)
            # Track unique_md5sums
            self.unique_md5sums = unique_md5sums

            # Remove None's from md5sums returned by dead nodes.
            try:
                unique_md5sums.pop(None)
            except Exception as e:
                self.log.debug(e)

        except Exception as e:
            self.log.error(e)
            pass

        if len(unique_md5sums.keys()) == 1:
            array_md5_sync = True
        else:
            array_md5_sync = False

        for md5sum in unique_md5sums.keys():
            if md5sum == kotekan_master_md5sum:
                kotekan_master_md5_sync = True

        # Update counter about the array being out of sync.
        # If the array has been in out of sync for two watchdog cycles.
        # start incrementing the global counter
        if (not array_md5_sync) and (not self.array_sync):
            self.out_of_sync_cycles += 1
            msg = "array out of sync for {} watchdog cycles".format(
                self.out_of_sync_cycles
            )
            if self.out_of_sync_cycles <= 5:
                self.log.warning("%s : %s", self, msg)
                self.slack.warning(
                    msg_title="Checksum validation failed.", msg=msg
                )
            elif self.out_of_sync_cycles > 5:
                self.log.critical("%s : %s", self, msg)
                self.slack.critical(
                    msg_title="Checksum validation failed.", msg=msg
                )
        else:
            self.out_of_sync_cycles = 0

        if array_md5_sync and (not self.array_sync):
            self.log.info("%s : Checksum validation passed.", self)
            self.log.debug(unique_md5sums)
            self.slack.info(
                msg_title="Checksum validation passed",
                msg=json.dumps(unique_md5sums),
                as_inline_code=True,
            )
        if not array_md5_sync:
            self.log.warning("%s : Checksum validation failed.", self)
            self.log.warning("%s : array_md5: %s", self, unique_md5sums)
            self.log.warning(
                "%s : kotekan master md5: %s", self, kotekan_master_md5sum
            )
            self.slack.warning(msg_title="Checksum validation failed")
            self.slack.warning(
                msg_title="array md5",
                msg=json.dumps(unique_md5sums),
                as_inline_code=True,
            )
            self.slack.warning(
                msg_title="kotekan master md5",
                msg=json.dumps(kotekan_master_md5sum),
                as_inline_code=True,
            )

        # Update globals regarding array sync status
        self.array_sync = array_md5_sync
        self.km_sync = kotekan_master_md5_sync
        result["array_sync"] = array_md5_sync
        result["km_sync"] = kotekan_master_md5_sync
        coroutine_return(result)

    # Kotekan Methods
    #   These methods interact with the kotekan rest server.
    #   See https://github.com/kotekan/kotekan for more information.

    # Operation Based Endpoints
    @coroutine
    def start_kotekan(self):
        """
        Start the kotekan process on all nodes.
        """
        self.log.info("%s : Starting kotekan...", self)
        self.slack.info(
            msg_title="start-kotekan",
            msg=json.dumps(self.nodes.keys()),
            as_inline_code=True,
        )
        result = yield {
            node_name: kotekan.start(
                config=self.current_config.common_config.as_dict()
            )
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    @coroutine
    def restart_kotekan(self, node_status):
        """
        Start specific kotekan nodes from the node_status which are not running

        NOTE: This method is not visible as a RESTful endpoint.

        Parameters
        ----------
        Input:
            node_status : dict-type
                {'node_name' : {'running' : boolean }}
        Returns:
            restart_list : list-type
                {'node_1', 'node_2', ... 'node_N'}
        """
        restart_list = []
        for node_name, status in node_status.items():
            if status.get("running") is False \
                    or status.get("desynced", 0) >= DESYNC_LIMIT:
                restart_list.append(node_name)
        if len(restart_list) != 0:
            self.log.info("Watchdog Restart List: %s", restart_list)
            self.slack.info(
                msg_title="Watchdog Restart List",
                msg=json.dumps(restart_list),
                as_inline_code=True,
            )
        # Start the nodes
        for node_name, kotekan in self.nodes.items():
            if node_name in restart_list:
                self.log.info("Restarting kotekan on %s", node_name)
                kotekan.start(
                    config=self.current_config.common_config.as_dict()
                )
        coroutine_return(restart_list)

    @coroutine
    def restart_cluster(self):
        """
        Restart kotekan on the entire node cluster
        """
        self.log.critical("Attempting to restarting the entire GPU Cluster")
        self.slack.critical(
            msg_title="Cluster Restart Statsu",
            msg="Attempting to restart the entire GPU Cluster",
        )
        # First make sure everything is killed!
        kill_status = yield self.kill_kotekan()
        self.log.critical(kill_status)
        self.slack.critical(
            msg_title="Cluster Restart", msg="All kotekan instances killed."
        )
        self.log.warning("Sleeping while the kill-kotekan permeates")
        self.slack.warning(
            msg_title="Cluster Restart",
            msg="Sleeping 30s while the kill-kotekan permeates",
        )
        yield sleep(30)
        try:
            new_gps_status = yield self._get_gps_time(
                slack_broadcast=True, update_config=True
            )
            self.gps_status = new_gps_status
            # Check for the corner case when gps returns an empty dict
            if self.gps_status["result"] == "FAILED":
                raise Exception(
                    "Restart Error: restart-cluster failed due to GPS Error"
                )
            # Successful GPS Acquisition
            self.slack.info(
                msg_title="Cluster Restart",
                msg=json.dumps(self.gps_time),
                as_inline_code=True,
            )
        except Exception as err:
            self.log.error(err)
            self.log.error("%s : Unable to restart cluster.", self)
            self.slack.error(
                msg_title="Cluster Restart",
                msg="GPS Error: Unable to execute restart-cluster",
            )
            coroutine_return(result="FAILED")

        if self.gps_status["result"] == "PASSED":
            self.log.info("Restarting kotekan on the entire cluster.")
            self.slack.info(
                msg_title="Restart Cluster", msg="Re-starting Kotekan..."
            )
            start_status = yield self.start_kotekan()
            self.log.debug(start_status)
            coroutine_return(result="PASSED")
        else:
            self.slack.error(msg_title="Restart Cluster", msg="Restart FAILED")
            coroutine_return(result="FAILED")

    @coroutine
    def stop_kotekan(self):
        """
        Stop the kotekan process on all nodes.
        """
        self.slack.info(
            msg_title="stop-kotekan (used /kill)",
            msg=json.dumps(self.nodes.keys()),
            as_inline_code=True,
        )
        result = yield {
            node_name: kotekan.kill()
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    @coroutine
    def kill_kotekan(self):
        """
        Kill the kotekan process on all nodes.
        """
        self.slack.info(
            msg_title="kill-kotekan",
            msg=json.dumps(self.nodes.keys()),
            as_inline_code=True,
        )
        result = yield {
            node_name: kotekan.kill()
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    @coroutine
    def kotekan_status(self):
        """
        GET status of the kotekan process from all nodes currently
        managed by kotekan_master.

        Returns: dict
        {"node_name" : {"running": Boolean}}
        """
        result = yield {
            node_name: kotekan.status()
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    @coroutine
    def kotekan_version(self):
        """
        GET Kotekan Version.

        Parameters
        ----------
            None

        Returns
        -------
            kotekan_version : dict-type
            { "node_name" : {"available_processes": ["p1","p2", ... "pN"],
                             "branch": "master",
                             "cmake_build_settings" : "BUILD_OPTIONS",
                             "git_commit_hash": "b1ce8aecf",
                             "kotekan_version": "2.3"
                            }
            }
        """
        result = yield {
            node_name: kotekan.version()
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    @coroutine
    def kotekan_running_config(self):
        """
        GET current kotekan configuration.
        """
        result = yield {
            node_name: kotekan.running_config()
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    @coroutine
    def kotekan_config_md5sum(self):
        """
        GET md5sum of current running configuration.

        Returns
            config_md5sum : dict-types
                {node_name:md5sum ...}
        """
        result = yield {
            node_name: kotekan.config_md5sum()
            for node_name, kotekan in self.nodes.items()
        }
        self.log.debug(result)
        coroutine_return(result)

    @coroutine
    def baseband(
        self,
        event_id,
        file_path,
        start_unix_seconds,
        start_unix_nano,
        duration_nano,
        dm,
        dm_error,
    ):
        """Submit a baseband dump request

        Parameters
        ----------
            event_id : integer
                Unique id number of the event

            file_path : string
                The path relative to the archiver root where the baseband files
                should be saved. E.g., "yyyy/mm/dd/<evt_id>/baseband/raw".

            start_unix_seconds : int
                Whole part of the start time of the dump (seconds since Unix
                epoch) at the reference frequency channel

            start_unix_nano : int
                Fractional part of the start time of the dump (seconds since
                Unix epoch) at the reference frequency channel, scaled to
                nanoseconds.

            duration_nano : int
                Duration of the dump in seconds at the reference frequency
                channel, in nanoseconds.

            dm : number
                Estimated dispersion measure

            dm_error : number
                Uncertainty of dispersion measure

        Returns
        -------
            <node_name> : dict
                Result of baseband request federation to kotekan nodes

        """
        msg = "%s: [%s.%s, %s] (dm=%s+/-%s)" % (
            event_id,
            start_unix_seconds,
            start_unix_nano,
            duration_nano,
            dm,
            dm_error,
        )
        self.log.debug("%s: dispatching baseband dump request %s", self, msg)

        result = yield {
            node_name: kotekan.baseband(
                event_id,
                file_path,
                start_unix_seconds,
                start_unix_nano,
                duration_nano,
                dm,
                dm_error,
            )
            for node_name, kotekan in self.nodes.items()
        }
        self.log.info("%s: baseband dump request dispatched", self)
        self.log.debug(result)

        self.slack.info(
            msg_title="Baseband %s requested" % event_id,
            msg=msg,
            as_inline_code=True,
        )
        coroutine_return(result)

    @coroutine
    def baseband_status(self, event_id):
        """GET baseband status for an event

        Parameters
        ----------
            event_id : integer
                Unique id number of the event

        Returns
        -------
            <node_name> : dict
                Result of baseband status federation to kotekan nodes

        """
        result = yield {
            node_name: kotekan.baseband_status(event_id)
            for node_name, kotekan in self.nodes.items()
        }
        self.log.info("%s: baseband status request dispatched", self)
        self.log.debug(result)
        coroutine_return(result)

    # Calibration Broker Endpoints
    @coroutine
    def update_bad_inputs(
        self, tag, start_time, correlator_bad_inputs, cylinder_bad_inputs
    ):
        """
        Update Calibration Broker provided Bad Input Feed Information

        Paramters
        ---------
            tag : string
                < update type >_< ISO8601 timestamp >_< source >
                e.g. flaginput_20180818T182255.390387Z_raw
            start_time : time.ctime type
                UTC after which the calibration paramter should be applied
            correlator_bad_input : list
                List of bad inputs in correlator schema
            cylinder_bad_inputs : list
                List of bad inputs in cylinder schema

        Returns
        -------
            kotekan_result : string
                Result of bad input federation to kotekan nodes
            receiver_result : string
                Result of bad input federation to receiver nodes

        """
        if not self.update_moratorium:
            # Update KotekanMaster state machine
            self.bad_inputs_tag = tag
            self.bat_inputs_update_time = start_time
            self.correlator_bad_inputs = correlator_bad_inputs
            self.cylinder_bad_inputs = cylinder_bad_inputs
            # Update local copy of kotekan configuration
            self.current_config.common_config.gpu.gpu_0.bad_inputs = (
                correlator_bad_inputs
            )
            self.current_config.common_config.gpu.gpu_1.bad_inputs = (
                correlator_bad_inputs
            )
            self.current_config.common_config.gpu.gpu_2.bad_inputs = (
                correlator_bad_inputs
            )
            self.current_config.common_config.gpu.gpu_3.bad_inputs = (
                correlator_bad_inputs
            )
            # Federate bad inputs to kotekan nodes
            kotekan_result = yield {
                node_name: kotekan.update_bad_inputs(
                    start_time=start_time,
                    tag=tag,
                    bad_inputs=correlator_bad_inputs,
                    update_destination="cluster",
                )
                for node_name, kotekan in self.nodes.items()
            }
            # Federate bad inputs to receiver nodes
            receiver_result = yield {
                receiver_node: receiver.update_bad_inputs(
                    start_time=start_time,
                    tag=tag,
                    bad_inputs=cylinder_bad_inputs,
                    update_destination="receiver",
                )
                for receiver_node, receiver in self.receiver_nodes.items()
            }
            # Create the result response
            result = {
                "kotekan_result": str(kotekan_result),
                "receiver_result": str(receiver_result),
            }
            # Log Things
            self.log.info("%s : Parameter bad_inputs updated.", self)
            self.log.debug(result)
            return_msg = "{} update-bad-inputs tag registered.".format(tag)
            self.slack.info(
                msg_title="update-bad-inputs", msg=tag, as_inline_code=True
            )
            coroutine_return(return_msg)
        else:
            result = "Kotekan updates moratorium, endpoint ignored."
            self.slack.info(
                msg_title='update-bad-inputs',
                msg=result,
                as_inline_code=True
            )
            coroutine_return(result)

    @coroutine
    def update_gain(self, start_time, tag):
        """
        Update Calibration Broker provided gain calibration information.

        Paramters
        ---------
            tag : string
                < update type >_< ISO8601 timestamp >_< source >
                e.g. gain_20180528T113502.178792Z_cyga
            start_time : time.time + delay
                UTC after which the calibration paramter should be applied
        Returns
        -------
            result : dict-type
                {
                    kotekan_result:
                        kotekan_response,
                    receiver_result:
                        receiver_response
                }
            where:
                kotekan_response : string
                    Result of gain federation to kotekan nodes
            receiver_result : string
                Result of bad input federation to receiver nodes

        """
        if not self.update_moratorium:
            # Update KotekanMaster Status Parameters
            self.cal_broker_gain_tag = tag
            self.cal_broker_gain_update_time = start_time

            # Federate calibration directory to kotekan nodes
            # Currently not implemented
            kotekan_result = {
                "Gain federation to kotekan nodes is currently not implemented"
            }

            # Federate calibration directory to receiver nodes
            receiver_result = yield {
                receiver_node: receiver.update_gain(
                    start_time=start_time,
                    tag=tag,
                    update_destination="receiver",
                )
                for receiver_node, receiver in self.receiver_nodes.items()
            }
            # Assemble the result from all federation destinations.
            result = {
                "kotekan_result": kotekan_result,
                "receiver_result": receiver_result,
            }

            # Log things
            self.log.info("%s : calibration tag updated to: %s", self, tag)
            self.log.debug(result)
            return_msg = "{} update-gain tag registered.".format(tag)
            self.slack.info(
                msg_title="update-gain",
                msg=tag,
                as_inline_code=True
            )
            coroutine_return(return_msg)
        else:
            result = "Kotekan updates moratorium, endpoint ignored."
            self.slack.info(
                "update-gain",
                msg=result,
                as_inline_code=True
            )
            coroutine_return(result)

    # Parameter POST Based Endpoints
    @coroutine
    def update_gain_dir(self, gain_dir):
        """
        POST the new gain directory for the beamformingKernel on all nodes
        currently managed by kotekan_master.
        """
        # # Update local configuration to reflect gain_dir changes.
        # self.gains_dir_update_time = time.strftime(
        #     "%Y/%m/%d %H:%M:%S", time.localtime()
        # )
        # self.current_config.common_config.gpu.gpu_0.gain_dir = gain_dir
        # self.current_config.common_config.gpu.gpu_1.gain_dir = gain_dir
        # self.current_config.common_config.gpu.gpu_2.gain_dir = gain_dir
        # self.current_config.common_config.gpu.gpu_3.gain_dir = gain_dir
        # result = yield {
        #     node_name: kotekan.update_gain_dir(gain_dir)
        #     for node_name, kotekan in self.nodes.items()
        # }
        # self.log.info(
        #     "%s : Parameter gains_dir updated to: %s", self, gain_dir
        # )
        # self.slack.info(
        #     msg_title="update-gain-dir", msg=gain_dir, as_inline_code=True
        # )
        result = (
            "Endpoint Depracted. Please use frb-gain-dir or pulsar_gain_dirs"
        )
        coroutine_return(result)

    @coroutine
    def update_frb_gain_dir(self, frb_gain_dir):
        """
        POST the new gain directory for the hsaBeamformKernel on all nodes.
        """
        self.frb_gains_dir_update_time = time.strftime(
            "%Y/%m/%d %H:%M:%S", time.localtime()
        )

        # Update the local copy of frb_gain_dir in config
        self.current_config.common_config.frb_gain.frb_gain_dir = frb_gain_dir

        # Update frb_gain_dir for all nodes
        result = yield {
            node_name: kotekan.update_frb_gain_dir(frb_gain_dir)
            for node_name, kotekan in self.nodes.items()
        }
        self.log.info(
            "%s : Parameter frb_gain_dir updated to: %s", self, frb_gain_dir
        )
        self.slack.info(
            msg_title="update-frb-gain-dir",
            msg=frb_gain_dir,
            as_inline_code=True,
        )
        coroutine_return(result)

    @coroutine
    def update_pulsar_gain_dirs(self, pulsar_gain_dirs):
        """
        POST the new gain directory list to hsaPulsarUpdatePhase kernel
        """
        self.pulsar_gains_dirs_update_time = time.strftime(
            "%Y/%m/%d %H:%M:%S", time.localtime()
        )
        self.log.info(
            "pulsar_gain_dirs type: {}".format(type(pulsar_gain_dirs))
        )
        self.log.info("pulsar_gain_dirs val:{}".format(pulsar_gain_dirs))
        self.log.info("Updating the local copy of the pulsar_gain_dirs")
        self.current_config.common_config.pulsar_gain.pulsar_gain_dir = (
            pulsar_gain_dirs
        )
        self.log.info("Updating the pulsar_gain_dirs for all nodes")
        result = yield {
            node_name: kotekan.update_pulsar_gain_dirs(pulsar_gain_dirs)
            for node_name, kotekan in self.nodes.items()
        }
        self.log.info(
            "%s : Parameter pulsar_gain_dirs updated to: %s",
            self,
            pulsar_gain_dirs,
        )
        self.slack.info(
            msg_title="update-pulsar-gain-dirs",
            msg=str(pulsar_gain_dirs),
            as_inline_code=True,
        )
        coroutine_return(result)

    @coroutine
    def update_beam_offset(self, beam_offset):
        """
        POST the new beam_offset for frbNetworkProcess on all nodes currently
        managed by kotekan_master.
        """
        self.current_config.common_config.frb.buffer_read.beam_offset = (
            beam_offset
        )
        result = yield {
            node_name: kotekan.update_beam_offset(beam_offset)
            for node_name, kotekan in self.nodes.items()
        }
        self.log.info(
            "%s : Parameter beam_offset updated to : %s", self, beam_offset
        )
        self.slack.info(
            msg_title="update-beam-offset",
            msg=json.dumps(beam_offset),
            as_inline_code=True,
        )
        coroutine_return(result)

    @coroutine
    def update_north_south_beam(self, northmost_beam):
        """
        Update CHIME/FRB North-South Beam
        """
        northmost_beam = float(northmost_beam)
        self.current_config.common_config.gpu.gpu_0.northmost_beam = (
            northmost_beam
        )
        self.current_config.common_config.gpu.gpu_1.northmost_beam = (
            northmost_beam
        )
        self.current_config.common_config.gpu.gpu_2.northmost_beam = (
            northmost_beam
        )
        self.current_config.common_config.gpu.gpu_3.northmost_beam = (
            northmost_beam
        )
        result = yield {
            node_name: kotekan.update_north_south_beam(northmost_beam)
            for node_name, kotekan in self.nodes.items()
        }
        self.log.info(
            "%s : Parameter northmost_beam updated to : %s",
            self,
            northmost_beam,
        )
        self.slack.info(
            msg_title="updated-north-south-beam",
            msg=json.dumps(northmost_beam),
            as_inline_code=True,
        )
        coroutine_return(result)

    @coroutine
    def update_east_west_beam(self, east_west_id, east_west_beam):
        """
        Update CHIME/FRB East-West Beam
        """
        east_west_id = int(east_west_id)
        east_west_beam = float(east_west_beam)
        try:
            # Update the KotekanMaster Dynamic Config
            self.current_config.common_config.gpu.gpu_0.ew_spacing[
                east_west_id
            ] = east_west_beam
            self.current_config.common_config.gpu.gpu_1.ew_spacing[
                east_west_id
            ] = east_west_beam
            self.current_config.common_config.gpu.gpu_2.ew_spacing[
                east_west_id
            ] = east_west_beam
            self.current_config.common_config.gpu.gpu_3.ew_spacing[
                east_west_id
            ] = east_west_beam

            msg = "east_west_id: {}, east_west_beam: {}".format(
                east_west_id, east_west_beam
            )
            self.log.info(msg)
            self.slack.info(
                msg_title="update-east-west-beam", msg=msg, as_inline_code=True
            )
            result = yield {
                node_name: kotekan.update_east_west_beam(
                    east_west_id, east_west_beam
                )
                for node_name, kotekan in self.nodes.items()
            }
            coroutine_return(result)
        except Exception as e:
            coroutine_return(str(e))

    @coroutine
    def update_pulsar_gating(
        self,
        enabled,
        pulsar_name,
        pulse_width,
        segment,
        dm,
        rot_freq,
        t_ref,
        phase_ref,
        coeff,
    ):
        """
        Update the Pulsar Gating Parameters
        """
        # Update the local configuration file
        config = self.current_config.common_config.updatable_config.gating
        try:
            config.psr0_config.enabled = enabled
            config.psr0_config.pulsar_name = pulsar_name
            config.psr0_config.pulse_width = pulse_width
            config.psr0_config.segment = segment
            config.psr0_config.rot_freq = rot_freq
            config.psr0_config.phase_ref = phase_ref
            config.psr0_config.t_ref = t_ref
            config.psr0_config.dm = dm
            config.psr0_config.coeff = coeff
        except Exception as e:
            coroutine_return(str(e))

        # Execute Endpoint
        result = yield {
            node_name: kotekan.update_pulsar_gating(
                enabled,
                pulsar_name,
                pulse_width,
                segment,
                dm,
                rot_freq,
                t_ref,
                phase_ref,
                coeff,
            )
            for node_name, kotekan in self.nodes.items()
        }
        self.slack.info(
            msg_title="update-pulsar-gating",
            msg=str(self.current_config.common_config.updatable_config.gating),
            as_inline_code=True,
        )
        coroutine_return(result)

    @coroutine
    def update_pulsar_pointing(self, beam, ra, dec, scaling):
        """
        Update CHIME/PSR Beam Pointing
        """
        if not self.update_moratorium:
            beam = int(beam)
            ra = float(ra)
            dec = float(dec)
            scaling = int(scaling)
            _config = self.current_config.common_config.gpu
            try:
                # Update KotekanMaster Dynamic Config
                # Update ra
                _config.gpu_0.source_ra[beam] = ra
                _config.gpu_1.source_ra[beam] = ra
                _config.gpu_2.source_ra[beam] = ra
                _config.gpu_3.source_ra[beam] = ra
                # Update dec
                _config.gpu_0.source_dec[beam] = dec
                _config.gpu_1.source_dec[beam] = dec
                _config.gpu_2.source_dec[beam] = dec
                _config.gpu_3.source_dec[beam] = dec
                # Update scaling
                _config.gpu_0.psr_scaling[beam] = scaling
                _config.gpu_1.psr_scaling[beam] = scaling
                _config.gpu_2.psr_scaling[beam] = scaling
                _config.gpu_3.psr_scaling[beam] = scaling

                msg = "beam: {}, ra: {}, dec: {}, scaling: {}".format(
                    beam, ra, dec, scaling
                )
                self.pulsar_slack.info(
                    msg_title="update-pulsar-pointing",
                    msg=msg,
                    as_inline_code=True
                )
                self.log.info("%s : Pulsar Parameters Updated", self)
                self.log.info(msg)
                result = yield {
                    node_name: kotekan.update_pulsar_pointing(
                        beam, ra, dec, scaling
                    )
                    for node_name, kotekan in self.nodes.items()
                }
                coroutine_return(result)
            except Exception as e:
                coroutine_return(str(e))
        else:
            result = "Kotekan updates moratorium, endpoint ignored."
            self.pulsar_slack.info(
                "update-pulsar-pointing",
                msg=result,
                as_inline_code=True
            )
            coroutine_return(result)

    @coroutine
    def toggle_rfi_zeroing(self, rfi_zeroing):
        """
        Toggle RFI Zeroing Kernels
        """
        # Update local state in the configuration
        self.current_config.common_config.rfi_masking.toggle.rfi_zeroing = (
            rfi_zeroing
        )

        #  Send the command to all nodes
        result = yield {
            node_name: kotekan.toggle_rfi_zeroing(rfi_zeroing)
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)

    # Node Methods
    #   These methods interact the node hardware and have no access to the
    #   kotekan process endpoints.
    @coroutine
    def ping_nodes(self):
        """
        Ping all kotekan clients currently managed by KotekanMaster
        """
        result = yield {
            node_name: kotekan.ping()
            for node_name, kotekan in self.nodes.items()
        }
        coroutine_return(result)


# KotekanMaster Asynchronous RESTful Server
class KotekanMasterAsyncRESTServer(AsyncRESTServer):
    """
    Asynchronous RESTful Server for KotekanMaster

    Wraps KotekanMaster into a Async Restful Server which can recieve HTTP GET,
    POST and PUT requests and call the appropriate KotekanMaster class
    functions to perform the required task.
    """

    # Defaul parameters
    KOTEKAN_MASTER_HOSTNAME = "localhost"
    DEFAULT_PORT = 54323

    def __init__(self, address=KOTEKAN_MASTER_HOSTNAME, port=DEFAULT_PORT):
        """
        KotekanMaster Server Initialization
        """
        super(KotekanMasterAsyncRESTServer, self).__init__(
            address=address,
            port=port,
            heartbeat_string="KMs",
            heartbeat_period=60000,
        )
        self.log.info(
            "KotekanMasterAsyncRESTServer: %s:%s", str(address), str(port)
        )
        self.current_config = None
        self.kotekan_master = KotekanMaster()
        self.log = self.kotekan_master.log
        # Start the watchdog loop.
        # NOTE: This routine only starts the loop, and not the watchdog actions
        # by default. You need to execute `start-watchdog` endpoint for that.
        self._watchdog()

    # KotekanMaster RESTful Endpoints
    #   API to interact with KotekanMaster.
    #   These endpoints do not interact with kotekan process or the nodes.
    @coroutine
    @endpoint("start-kotekan-master")
    def start_kotekan_master(self, handler, **config):
        """
        Initialization KotekanMaster
        """
        result = yield self.kotekan_master.start_kotekan_master(config)
        coroutine_return(result)

    @coroutine
    @endpoint("stop-kotekan-master")
    def stop_kotekan_master(self, handler):
        """
        Stop KotekanMaster
        """
        result = yield self.kotekan_master.stop_kotekan_master()
        coroutine_return(result)

    @coroutine
    @endpoint("start-watchdog")
    def start_watchdog(self, handler):
        """
        Start Kotekan Watchdog
        """
        result = yield self.kotekan_master.start_watchdog()
        coroutine_return(result)

    @coroutine
    def _watchdog(self):
        """
        KotekanMaster Watchdog Loop

        NOTE: This loop runs prepetually. To enable and disable the watchdog,
        execute the start-watchdog and stop-watchdog endpoints.
        """
        while True:
            # Check if the watchdog is currently enabled.
            if self.kotekan_master.watchdog_enabled:
                self.log.info("%s : Watching...0.0", self)
                try:
                    self.kotekan_master.watchdog_cycles += 1
                    # Run this portion of the watchdog only if the GPS status
                    # is PASSED (Good)
                    if self.kotekan_master.gps_status["result"] == "PASSED":
                        # Run get status from each node
                        self.log.info("%s : WD : Geting Node Status", self)
                        node_status = (
                            yield self.kotekan_master.kotekan_status()
                        )
                        # Report nodes which have connection issues
                        no_connection_nodes = (
                            yield self.kotekan_master.check_node_connection(
                                node_status
                            )
                        )
                        self.log.warning(
                            "%s : Node Connection Issues: %s",
                            self,
                            no_connection_nodes,
                        )

                        # Validate Configuration Checksums
                        self.log.info("%s : WD: Validating Checksums", self)
                        checksum_validate, desync_status = (
                            yield self.kotekan_master.validate_checksum()
                        )
                        node_status.update(desync_status)

                        self.log.info("{0}".format(checksum_validate))
                        # Execute restarts for nodes with running==false or too
                        # high desynced counter
                        self.log.info("%s : WD: Geting Restart List", self)
                        restart_list = (
                            yield self.kotekan_master.restart_kotekan(
                                node_status
                            )
                        )
                        yield sleep(10)
                        # Update watchdog statistics
                        self.log.info("%s : WD: Updating Watchdog Stats", self)
                        watchdog_stats = (
                            yield self.kotekan_master.update_watchdog_stats(
                                restart_list
                            )
                        )
                        yield sleep(10)
                        # Log Kotekan Master Statistics
                        self.log.debug(
                            "%s : KotekanMaster Watchdog Stats", self
                        )
                        self.log.debug("%s : %s", self, watchdog_stats)
                        # Validate GPS Clock Status
                        self.log.info("%s : Validating GPS Time", self)
                        gps_validate = yield self.kotekan_master.validate_gps()
                        self.log.info(
                            "%s : GPS Status: %s", self, gps_validate
                        )
                    else:
                        self.log.error(
                            "Unable to execute watchdog loop due to gps error"
                        )

                    # Execute this part of the loop only when validate_gps has
                    # FAILED, causing the KotekanMaster to shutdown the entire
                    # cluster.
                    if gps_validate["result"] != "PASSED":
                        restart_cluster_status = (
                            yield self.kotekan_master.restart_cluster()
                        )
                        self.log.info(
                            "%s : Restart Cluster Status %s",
                            self,
                            restart_cluster_status,
                        )
                except Exception as watchdog_error:
                    self.log.error(watchdog_error)

                # Sleep Watchdog
                sleep_interval = self.kotekan_master.watchdog_interval

                if self.kotekan_master.update_moratorium:
                    sleep_interval = self.kotekan_master.watchdog_interval/2
                    if self.kotekan_master.watchdog_cycles > 2:
                        self.log.info("Update Moratorium Lifted")
                        self.kotekan_master.update_moratorium = False

                self.log.info(
                    'Watchdog sleeping for {}s Zzz..o.0'.format(sleep_interval)
                )
                yield sleep(sleep_interval)

            if not self.kotekan_master.watchdog_enabled:
                # If watchdog is not enabled, still sleep so that we dont
                # overtake compute cycles.
                yield sleep(5)

    @coroutine
    @endpoint("stop-watchdog")
    def stop_watchdog(self, handler):
        """
        Stop Kotekan Watchdog
        """
        result = yield self.kotekan_master.stop_watchdog()
        coroutine_return(result)

    @coroutine
    @endpoint("blacklist-node")
    def blacklist_node(self, handler, node_list):
        """
        Blacklist a node[s] from being actively managed by KotekanMaster

        curl
        -d '{"node_list":["csDg5", "csDg6"]}'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/blacklist-node
        """
        result = yield self.kotekan_master.blacklist_node(node_list)
        coroutine_return(result)

    @coroutine
    @endpoint("whitelist-node")
    def whitelist_node(self, handler, node_list):
        """
        Whitelist a node to bring it under the control of KotekanMaster

        Parameters
        ----------
        curl
            -d '{"node_list":["csDg5", "csDg6"]}'
            -X POST
            -H "Content-Type: application/json"
            http://localhost:54323/whitelist-node
        """
        result = yield self.kotekan_master.whitelist_node(node_list)
        coroutine_return(result)

    @coroutine
    @endpoint("status-kotekan-master")
    def status_kotekan_master(self, handler):
        """
        Get the current status of KotekanMaster
        """
        result = yield self.kotekan_master.status_kotekan_master()
        self.log.info("%s : Sending KotekanMaster Status", self)
        coroutine_return(result)

    # KotekanMaster Validation Routines
    #   These routines run on the entire node cluster in order to deduce
    #   consensus in the array.
    @coroutine
    @endpoint("validate-checksum")
    def validate_checksum(self, handler):
        """
        Validate the md5checksum for all running kotekan configs against the
        md5checksum of the self.current_config
        """
        result = yield self.kotekan_master.validate_checksum()
        coroutine_return(result)

    # Kotekan RESTful Endpoints
    #   These endpoints interact with the kotekan process.
    #   There are two types of Kotekan RESTful endpoints
    #       Parameter Endpoints: Dynamically tracked which have a corresponding
    #                            config paramter
    #       Operation Endpoints: Not tracked, and no corresponding config
    #                            parameter, e.g. start_baseband_dump

    # Operation Endpoints
    @coroutine
    @endpoint("start-kotekan")
    def start_kotekan(self, handler):
        """
        Start kotekan process on all nodes with current_config
        """
        result = yield self.kotekan_master.start_kotekan()
        watchdog = yield self.kotekan_master.start_watchdog()
        self.log.debug(watchdog)
        coroutine_return(result)

    @coroutine
    @endpoint("stop-kotekan")
    def stop_kotekan(self, handler):
        """
        Stop the kotekan process on all nodes.
        """
        yield self.kotekan_master.stop_watchdog()
        result = yield self.kotekan_master.stop_kotekan()
        coroutine_return(result)

    @coroutine
    @endpoint("kill-kotekan")
    def kill_kotekan(self, handler):
        """
        Kill the kotekan process on all nodes.
        """
        yield self.kotekan_master.stop_watchdog()
        result = yield self.kotekan_master.kill_kotekan()
        coroutine_return(result)

    @coroutine
    @endpoint("restart-cluster")
    def restart_kotekan(self, handler):
        """
        Restart kotekan process on the entire cluster
        and also reacquire the gps time.
        """
        stop = yield self.kotekan_master.stop_watchdog()
        restart = yield self.kotekan_master.restart_cluster()
        start = yield self.kotekan_master.start_watchdog()
        self.log.debug(stop, restart, start)
        coroutine_return(result="restart-cluster executed")

    @coroutine
    @endpoint("kotekan-status")
    def kotekan_status(self, handler):
        """
        GET status of all kotekan from all nodes.
        """
        result = yield self.kotekan_master.kotekan_status()
        coroutine_return(result)

    @coroutine
    @endpoint("kotekan-version")
    def kotekan_version(self, handler):
        """
        GET version of kotekan process from all nodes.
        """
        result = yield self.kotekan_master.kotekan_version()
        coroutine_return(result)

    # Returns all the unique running configurations found in the array.
    @coroutine
    @endpoint("kotekan-running-config")
    def kotekan_running_config(self, handler):
        """
        Queries the current running configuration from the kotekan process.
        """
        result = yield self.kotekan_master.kotekan_running_config()
        coroutine_return(result)

    # Returns all the unique config checksums found in the array.
    @coroutine
    @endpoint("kotekan-config-md5sum")
    def kotekan_config_md5sum(self, handler):
        """
        Queries for the MD5 hash of the running configuration.
        """
        result = yield self.kotekan_master.kotekan_config_md5sum()
        coroutine_return(result)

    @coroutine
    @endpoint("baseband")
    def baseband(
        self,
        handler,
        event_id,
        file_path,
        start_unix_seconds,
        start_unix_nano,
        duration_nano,
        dm,
        dm_error,
    ):
        """
        POST to submit a baseband dump request.

        curl
        -d
            '{
                "event_id": 238,
                "file_path": "yyyy/mm/dd/<evt_id>/baseband/raw",
                "start_unix_seconds": -1,
                "start_unix_nano": -1,
                "duration_nano": 400000000,
                "dm": 100,
                "dm_error": 12
            }'
        -H "Content-Type: application/json"
        http://KOTEKAN-MASTER-NODE:KOTEKAN_MASTER-PORT/baseband
        """
        # file_path is cleaned *not* to include the trailing slash
        if file_path[-1] == "/":
            file_path = file_path[:-1]
        result = yield self.kotekan_master.baseband(
            event_id,
            file_path,
            start_unix_seconds,
            start_unix_nano,
            duration_nano,
            dm,
            dm_error,
        )
        coroutine_return(result)

    @coroutine
    @endpoint(r"baseband/\d+")
    def baseband_status(self, handler):
        """
        Queries for the status of a baseband dump request.
        """
        event_id = handler.request.path.split("/")[-1]
        result = yield self.kotekan_master.baseband_status(int(event_id))

        statuses = set()
        for node, node_status in result.items():
            if "RuntimeError" in node_status:
                statuses.add("fail")
            else:
                for readout_status in node_status:
                    statuses.add(readout_status["status"])

        if len(statuses) == 1:
            status = statuses.pop()
        elif statuses == set(["done", "error"]):
            status = "done"
        else:
            status = "inprogress"

        if status == "fail":
            status = "error"

        coroutine_return({"status": status, "nodes": result})

    @coroutine
    @endpoint("toggle-rfi-zeroing")
    def toggle_rfi_zeroing(self, handler, rfi_zeroing):
        """
        POST to update the state of rfi_zeroing
        curl
        -d
            '{"rfi_zeroing": false|true}'
        -X POST
        -H "Content-Type: application/json"
        http://KOTEKAN-MASTER-NODE:KOTEKAN_MASTER-PORT/toggle-rfi-zeroing
        """
        result = yield self.kotekan_master.toggle_rfi_zeroing(rfi_zeroing)
        coroutine_return(result)

    # Calibration Broker Endpoints
    @coroutine
    @endpoint("update-gain")
    def update_gain(self, handler, start_time, tag):
        """
        POST to update and federate the calibration broker provided
        gain information

        curl
        -d
            '{
                "start_time": time.time() + delay,
                "tag": "gain_20180528T113502.178792Z_cyga"
            }'
        -X POST
        -H "Content-Type: application/json"
        http://KOTEKAN-MASTER-NODE:KOTEKAN_MASTER-PORT/update-gain
        """
        result = yield self.kotekan_master.update_gain(start_time, tag)
        coroutine_return(result)

    @coroutine
    @endpoint("update-pulsar-gain-dirs")
    def update_pulsar_gain_dirs(self, handler, pulsar_gain_dirs):
        """
        POST to update and federate the calibration broker provided gain
        information to the pulsar gpu kernels

        curl
            -d '{
                "pulsar_gain_dirs":[
                "path0","path1","path2","path3","path4",
                "path5","path6","path7","path8","path9"
                ]
            }'
        -X POST
        -H "Content-Type: application/json"
        http://KOTEKAN-MASTER-NODE:KOTEKAN_MASTER-PORT/update-pulsar-gain
        """
        result = yield self.kotekan_master.update_pulsar_gain_dirs(
            pulsar_gain_dirs
        )
        coroutine_return(result)

    @coroutine
    @endpoint("update-frb-gain-dir")
    def update_frb_gain_dir(self, handler, frb_gain_dir):
        """
        POST to update the frb_gain_dir parameter.

        curl
        -d '{"frb_gain_dir": "dir"}'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-frb-gain-dir
        """
        result = yield self.kotekan_master.update_frb_gain_dir(frb_gain_dir)
        coroutine_return(result)

    @coroutine
    @endpoint("update-bad-inputs")
    def update_bad_inputs(
        self,
        handler,
        tag,
        start_time,
        correlator_bad_inputs,
        cylinder_bad_inputs,
    ):
        """
        POST to update and federate the bad inputs

        curl
        -d
        '{ "tag": <HASH>,
           "start_time": <C_TIME>,
           "correlator_bad_inputs": <ARRAY>,
           "cylinder_bad_inputs": <ARRAY> }'
        -X POST
        -H "Content-Type: application/json"
        http://KOTEKAN-MASTER:PORT/update-bad-inputs
        """
        result = yield self.kotekan_master.update_bad_inputs(
            tag, start_time, correlator_bad_inputs, cylinder_bad_inputs
        )
        coroutine_return(result)

    # Parameter Endpoints.
    @coroutine
    @endpoint("update-gain-dir")
    def update_gain_dir(self, handler, gain_dir):
        """
        POST to update the gain_dir parameter.

        curl
        -d '{"gain_dir": "dir"}'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-gain-dir
        """
        # result = yield self.kotekan_master.update_gain_dir(gain_dir)
        result = "Endpoint Depracted: Use frb-gain-dir or pulsar-gain-dirs"
        coroutine_return(result)

    @coroutine
    @endpoint("update-beam-offset")
    def update_beam_offset(self, handler, beam_offset):
        """
        POST to update the beam_offset parameter.

        curl
        -d '{"beam_offset": <int> }'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-beam-offset
        """
        result = yield self.kotekan_master.update_beam_offset(beam_offset)
        coroutine_return(result)

    @coroutine
    @endpoint("update-north-south-beam")
    def update_north_south_beam(self, handler, northmost_beam):
        """
        POST to update CHIME/FRB northmost_beam parameter

        curl
        -d '{"northmost_beam": float }'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-north-south-beam
        """
        result = yield self.kotekan_master.update_north_south_beam(
            northmost_beam
        )
        coroutine_return(result)

    @coroutine
    @endpoint("update-east-west-beam")
    def update_east_west_beam(self, handler, east_west_id, east_west_beam):
        """
        POST to update CHIME/FRB East-West Beam

        curl
        -d '{"east_west_id": 0|1|2|3, "east_west_beam": 0.2 }'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-east-west-beam
        """
        result = yield self.kotekan_master.update_east_west_beam(
            east_west_id, east_west_beam
        )
        coroutine_return(result)

    @coroutine
    @endpoint("update-pulsar-gating")
    def update_pulsar_gating(
        self,
        handler,
        enabled,
        pulsar_name,
        pulse_width,
        segment,
        dm,
        rot_freq,
        t_ref,
        phase_ref,
        coeff,
    ):
        """
        POST to update CHIME/Cosmology pulsar gating parameter
        curl
        -d '{
            "enabled" : "false",
            "pulsar_name": "B1929",
            "pulse_width": 0.014,
            "rot_freq": 4.41466731644,
            "phase_ref": 4542317506.850324938073754,
            "t_ref": 58431.77083333330000058936,
            "segment": 18000.0,
            "dm": 3.18321,
            "coeff": '[[
               3.428779943504219e-10,
               0.0011253963075329334,
               -1.1565642124857199e-07,
               1.1347089844281842e-10,
               1.1882399863116445e-13,
               -1.0867680647236115e-16,
               -7.594309559679319e-20,
               5.042548967792808e-23,
               2.745973613190195e-26,
               -2.279050323988331e-29,
               -1.1771713330451292e-32,
               3.948826407949732e-35,
             ],]'
        }'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-pulsar-gating
        """
        self.log.debug(
            "\nEnabled:{}\nName:{}\nWidth:{}\nSegment:{}\nDM:{}\nRot:{}\nT:{}\nPhase:{}\nCoeff:{}\n".format(
                enabled,
                pulsar_name,
                pulse_width,
                segment,
                dm,
                rot_freq,
                t_ref,
                phase_ref,
                coeff,
            )
        )
        result = yield self.kotekan_master.update_pulsar_gating(
            enabled,
            pulsar_name,
            pulse_width,
            segment,
            dm,
            rot_freq,
            t_ref,
            phase_ref,
            coeff,
        )
        coroutine_return(result)

    @coroutine
    @endpoint("update-pulsar-pointing")
    def update_pulsar_pointing(self, handler, beam, ra, dec, scaling):
        """
        POST to update CHIME/PSR pulsar beam pointing.
        curl
        -d '{"beam": 0|1|2|3,
             "ra": 0.0,
             "dec": 0.0,
             "scaling": 48 }'
        -X POST
        -H "Content-Type: application/json"
        http://localhost:54323/update-pulsar-pointing

        """
        result = yield self.kotekan_master.update_pulsar_pointing(
            beam, ra, dec, scaling
        )
        coroutine_return(result)

    # Node RESTful Endpoints
    #   These endpoints interact with the hardware in the GPU SeaCans.
    @coroutine
    @endpoint("ping-nodes")
    def ping_nodes(self, handler):
        """
        Ping kotekan nodes.
        """
        self.log.info("%r: Pinging kotekan nodes...", self)
        result = self.kotekan_master.ping_nodes()
        coroutine_return(result)

    @coroutine
    @endpoint("boot-nodes")
    def boot_nodes(self, handler):
        """
        Boot kotekan nodes
        """
        result = "Not Implemented."
        coroutine_return(result)

    @coroutine
    @endpoint("shutdown-nodes")
    def shutdown_nodes(self, handler):
        """
        Shutdown kotekan nodes
        """
        result = "Not Implemented."
        coroutine_return(result)

    @coroutine
    @endpoint("reboot-nodes")
    def reboot_nodes(self, handler):
        """
        Reboot kotekan nodes
        """
        result = "Not Implemented."
        coroutine_return(result)


# KotekanMaster Asynchronous RESTful Client
class KotekanMasterAsyncRESTClient(AsyncRESTClient):
    """Async Restful Server for Kotekan Master
    """

    # Default Port for KotekanMaster as assigned in bao.phas wikipage.
    # TODO: Add a link to the wikipedia page.
    DEFAULT_PORT = KotekanMasterAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname="localhost", port=DEFAULT_PORT):
        """
        KotekanMaster Client Initialization
        """
        super(KotekanMasterAsyncRESTClient, self).__init__(
            hostname=hostname,
            port=port,
            server_class=KotekanMasterAsyncRESTServer,
            heartbeat_string="KMc",
            heartbeat_period=10000,
            connection_timeout=6,
            request_timeout=6,
        )

    # Kotekan Master Routines
    # These routines are not executed on any kotekan node but are rather
    # specific to the current state of KotekanMaster
    @coroutine
    def start(self, config):
        """
        Start a KotekanMaster process with the specified configuration.

        Parameters:
            config (str or dict): If a string, the configuration is
            loaded from the specified configuration file and name.
            if a dict, it is passed directly to the server.
        """
        self.log.info(
            "%s:Starting KotekanMasterServer at %s:%i with config:%r",
            self,
            self.hostname,
            self.port,
            config,
        )
        if isinstance(config, str):
            config = load_yaml_config(config)
        result = self.post("start-kotekan-master", **config)
        coroutine_return(result)

    @coroutine
    def stop(self):
        """
        Stop kotekan_master from managing any nodes.
        """
        self.log.info(
            "%s: Stoping KotekanMasterServer at %s:%i",
            self,
            self.hostname,
            self.port,
        )
        result = yield self.get("stop-kotekan-master")
        coroutine_return(result)

    @coroutine
    def status(self):
        """
        Current status of the kotekan_master.
        """
        result = yield self.get("status-kotekan-master")
        coroutine_return(result)

    @coroutine
    def start_watchdog(self):
        """
        Start KotekanMaster Watchdog
        """
        result = yield self.post("start-watchdog")
        coroutine_return(result)

    @coroutine
    def stop_watchdog(self):
        """
        Stop KotekanMaster Watchdog
        """
        result = yield self.get("stop-watchdog")
        coroutine_return(result)

    @coroutine
    def blacklist_node(self, node_dict):
        """
        Blacklist node[s] from being managed by KotekanMaster
        """
        result = yield self.post("blacklist-node", node_dict)
        coroutine_return(result)

    @coroutine
    def whitelist_node(self, node_list):
        """
        Whitelist nodes[s]
        """
        result = yield self.post("whitelist-node", node_list)
        coroutine_return(result)

    # Kotekan Routines
    # Routine endpoints which interact with kotekan nodes.
    @coroutine
    def start_kotekan(self):
        """
        Start kotekan on all nodes with the current configuration.
        """
        result = yield self.get("start-kotekan")
        coroutine_return(result)

    @coroutine
    def restart_cluster(self):
        """
        Restart the entire cluster by reacquiring GPS Time
        """
        result = yield self.get("restart-cluster")
        coroutine_return(result)

    @coroutine
    def stop_kotekan(self):
        """
        Stop kotekan on all nodes.
        """
        result = yield self.get("stop-kotekan")
        coroutine_return(result)

    @coroutine
    def kill_kotekan(self):
        """
        Kill kotekan process on all nodes.
        """
        result = yield self.get("kill-kotekan")
        coroutine_return(result)

    @coroutine
    def kotekan_status(self):
        """
        Get status of kotekan from all nodes.
        """
        result = yield self.get("kotekan-status")
        coroutine_return(result)

    @coroutine
    def kotekan_version(self):
        """
        Kotekan Version
        """
        result = yield self.get("kotekan-version")
        coroutine_return(result)

    @coroutine
    def kotekan_running_config(self):
        """
        Current running kotekan configuration.
        """
        result = yield self.get("kotekan-running-config")
        coroutine_return(result)

    @coroutine
    def kotekan_config_md5sum(self):
        """
        Returns an MD5 hash of the config file
        """
        result = yield self.get("kotekan-config-md5sum")
        coroutine_return(result)

    @coroutine
    def baseband(
        self,
        event_id,
        file_path,
        start_unix_seconds,
        start_unix_nano,
        duration_nano,
        dm,
        dm_error,
    ):
        """
        Update and federate baseband dump requests.
        """
        result = yield self.post(
            "baseband",
            event_id,
            file_path,
            start_unix_seconds,
            start_unix_nano,
            duration_nano,
            dm,
            dm_error,
        )
        coroutine_return(result)

    @coroutine
    def baseband_status(self, event_id):
        """
        Returns the status of a baseband dump for `event_id`
        """
        result = yield self.get("baseband/{}".format(event_id))
        coroutine_return(result)

    @coroutine
    def toggle_rfi_zeroing(self, rfi_zeroing):
        """
        Update the state of the rfi-zeroing kernel
        """
        result = yield self.post("toggle-rfi-zeroing", rfi_zeroing)
        coroutine_return(result)

    # Calibration Broker Endpoints
    @coroutine
    def update_gain(self, start_time, tag):
        """
        Update and federate gain calibration information sent from
        the calibration broker.
        """
        result = yield self.post("update-gain", start_time, tag)
        coroutine_return(result)

    @coroutine
    def update_bad_inputs(
        self, tag, start_time, correlator_bad_inputs, cylinder_bad_inputs
    ):
        """
        Update and federate bad inputs changes.
        """
        result = yield self.post(
            "update-bad-inputs",
            tag,
            start_time,
            correlator_bad_inputs,
            cylinder_bad_inputs,
        )
        coroutine_return(result)

    # Parameter Endpoints
    # All parameter endpoints have a corresponding value in the kotekan config.
    @coroutine
    def update_gain_dir(self, gain_dir):
        """
        Update CHIME/FRB Gains directory on all nodes.
        """
        result = yield self.post("update-gain-dir", gain_dir)
        coroutine_return(result)

    @coroutine
    def update_frb_gain_dir(self, frb_gain_dir):
        result = yield self.post("update-frb-gain-dir", frb_gain_dir)
        coroutine_return(result)

    @coroutine
    def update_pulsar_gain_dirs(self, pulsar_gain_dirs):
        result = yield self.post("update-pulsar-gain-dirs", pulsar_gain_dirs)
        coroutine_return(result)

    @coroutine
    def update_east_west_beam(self, east_west_id, east_west_beam):
        """
        Update CHIME/FRB East West Beam Spacing
        """
        result = yield self.post(
            "update-east-west-beam", east_west_id, east_west_beam
        )
        coroutine_return(result)

    @coroutine
    def update_north_south_beam(self, northmost_beam):
        """
        Update CHIME/FRB North South Beam Spacing
        """
        result = yield self.post("update-north-south-beam", northmost_beam)
        coroutine_return(result)

    @coroutine
    def update_beam_offset(self, beam_offset):
        """
        Update CHIME/FRB Beam Offset on all nodes.
        """
        result = yield self.post("update-beam-offset", beam_offset)
        coroutine_return(result)

    @coroutine
    def update_pulsar_gating(
        self,
        enabled,
        pulsar_name,
        pulse_width,
        segment,
        dm,
        rot_freq,
        t_ref,
        phase_ref,
        coeff,
    ):
        """
        """
        result = yield self.post(
            "update-pulsar-gating",
            enabled,
            pulsar_name,
            pulse_width,
            segment,
            dm,
            rot_freq,
            t_ref,
            phase_ref,
            coeff,
        )
        coroutine_return(result)

    @coroutine
    def update_pulsar_pointing(self, beam, ra, dec, scaling):
        """
        Update CHIME/PSR Beam Pointing
        """
        result = yield self.post(
            "update-pulsar-pointing", beam, ra, dec, scaling
        )
        coroutine_return(result)

    # Node Routines
    @coroutine
    def ping_nodes(self):
        result = yield self.get("ping-nodes")
        coroutine_return(result)

    @coroutine
    def boot_nodes(self):
        result = yield self.get("boot-nodes")
        coroutine_return(result)

    @coroutine
    def shutdown_nodes(self):
        result = yield self.get("shutdown-nodes")
        coroutine_return(result)

    @coroutine
    def reboot_nodes(self):
        result = yield self.get("restart-nodes")
        coroutine_return(result)


# Cleanup Opened Sockets
def reap_cached_sockets():
    import __main__

    logger = log.get_logger(__name__, "reap_cached_sockets()")
    if hasattr(__main__, "__opened_sockets__"):
        for port, socket in __main__.__opened_sockets__.items():
            logger.debug("closing cached socket on port %d" % port)
            socket.close()
        del __main__.__opened_sockets__


# Command Line Interface to operate KotekanMaster Server
def main():
    """CLI for operating Kotekan Master
    """
    # TODO: Add Configuration Path Here
    # TODO: Add click based CLI similar to kotekan_master
    config = "config.yaml"
    log.setup_basic_logging("INFO")
    client, server = run_client(
        config,
        KotekanMasterAsyncRESTServer,
        KotekanMasterAsyncRESTClient,
        object_name="KotekanMaster",
        server_config_path="kotekan_master.servers",
    )
    # TODO: This needs to be fixed.
    kotekan_master = None
    if server:
        kotekan_master = RunSyncWrapper(server.kotekan_master)
    return client, server, kotekan_master


# Command Line Instantiation of KotekanMaster
if __name__ == "__main__":
    """
    Kotekan Master Command Line Instantiation
    """
    CLIENT, SERVER, KM = main()
