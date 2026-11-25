# Crate 012, slot 8 (SN0531): corr16 in Docker

Runs one IceBoard of crate 012 in corr16 with two containers: `raw_acq`, then
`fpga_master`. Data goes to `/storage/crate012/corr16_docker`.

| File | Purpose |
|---|---|
| `crate012_corr16_docker.yaml` | fpga_master configuration |
| `crate012_corr16_docker.env` | Docker settings: image, data folder, config |
| `docker-compose.yaml` | the two services (shared by every Docker deployment) |

Run every command from the repository folder:

```bash
cd /home/ops/pychfpga
```

## Once per host

Create the data folder (the containers run as root, so the files they write
there are owned by root):

```bash
mkdir -p /storage/crate012/corr16_docker/watchdog
```

Raise the UDP receive buffers, or raw_acq drops packets (see the README):

```bash
sudo sysctl -w net.core.rmem_max=268435456 net.core.rmem_default=268435456 net.ipv4.udp_rmem_min=268435456
printf 'net.core.rmem_max = 268435456\nnet.core.rmem_default = 268435456\nnet.ipv4.udp_rmem_min = 268435456\n' | sudo tee /etc/sysctl.d/90-pychfpga-udp.conf
```

## Run

1. Build the image from the current code (about 3 minutes; quick when nothing
   changed):

   ```bash
   PYCHFPGA_ENV_FILE=crate012_corr16_docker.env ./docker_build
   ```

2. Make sure the temperature watchdog is running. It powers the crate off if
   an FPGA reaches 85 C. The site cooling can cut out.

   ```bash
   pgrep -af '[c]rate_watchdog'
   ```

   If that prints nothing, start it:

   ```bash
   setsid nohup .venv/bin/python -u fw_test_results/crate012_20261016/tools/crate_watchdog.py \
       bp16 12:0 /storage/crate012/corr16_docker/watchdog/temps.csv \
       > /storage/crate012/corr16_docker/watchdog/watchdog.log 2>&1 < /dev/null &
   ```

3. Power the crate off (this waits the 8 s off time):

   ```bash
   python3 deployment_scripts/ps_power_off.py
   ```

4. Power it on:

   ```bash
   python3 deployment_scripts/ps_power_on.py
   ```

5. Wait 3 minutes for the boards to boot:

   ```bash
   sleep 180
   ```

6. Start raw_acq and check that it shows `healthy`:

   ```bash
   docker compose --env-file crate012_corr16_docker.env up -d raw_acq
   docker compose --env-file crate012_corr16_docker.env ps
   ```

7. Start fpga_master:

   ```bash
   docker compose --env-file crate012_corr16_docker.env up -d fpga_master
   ```

8. Watch it come up. It is ready when `ps` shows fpga_master `healthy`
   (this can take up to 10 minutes):

   ```bash
   docker compose --env-file crate012_corr16_docker.env ps
   docker compose --env-file crate012_corr16_docker.env logs -f fpga_master   # Ctrl-C stops watching only
   ```

   Correlator data appears in `/storage/crate012/corr16_docker/<run>/corr/`,
   and `/storage/crate012/corr16_docker/current` points to the latest run.

## Stop

First close the correlator file. `docker compose down` kills the containers
without letting them close it, which leaves the file being written unreadable.

```bash
curl -fsS http://127.0.0.1:54322/stop-corr-hdf5; echo
docker compose --env-file crate012_corr16_docker.env down
python3 deployment_scripts/ps_power_off.py
```

`curl` should print `"stopped correlator hdf5 writing to disk."`, and the
`.lock` file next to the last data file disappears.

## If something goes wrong

- **fpga_master stays `starting` or `unhealthy`:** read the reason with
  `docker compose --env-file crate012_corr16_docker.env logs fpga_master`.
  A failed start does not retry by itself, because each attempt reprograms
  the FPGA.
- **The boards were not ready yet:** wait a minute, then try again:
  `docker compose --env-file crate012_corr16_docker.env restart fpga_master`.
- **The crate turned itself off:** the watchdog cut the power because an FPGA
  reached 85 C. Its log is `/storage/crate012/corr16_docker/watchdog/watchdog.log`,
  or the log of the watchdog that `pgrep` found.

