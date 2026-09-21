"""Array-wide methods for Ultrascale corner-turn (UCT) firmware."""

from pychfpga.common.array_methods import ArrayMethods


class UCTArrayMethods(ArrayMethods):
    """Array-wide methods for UCT (CRS / ZCU111) arrays."""

    def get_frequency_map(self, format='l:cscb'):
        """Stub: UCT has no CROSSBAR walk. Real CRS fmap is a separate issue."""
        return {}

    def reset_crossbar_stats(self):
        """No-op: UCT has no Ice CROSSBAR / CROSSBAR2 / CROSSBAR3."""
        return

    def reset_bp_shuffle_stats(self):
        """No-op: UCT has no BP_SHUFFLE."""
        return
