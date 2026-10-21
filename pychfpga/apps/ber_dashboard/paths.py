"""User data directory: saved plots, crate lists, previous inputs. Set BER_DASHBOARD_DIR to override."""
import os

DATA_DIR = os.environ.get('BER_DASHBOARD_DIR', os.path.expanduser('~/ber_dashboard'))
PLOT_DIR = os.path.join(DATA_DIR, 'plots')
for d in (PLOT_DIR, os.path.join(DATA_DIR, 'prev_data', 'crates')):
    os.makedirs(d, exist_ok=True)
