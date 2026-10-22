"""Array-wide signal-integrity methods for the CGE (100G) link and its QSFP28 optical module.

Registered by :class:`~pychfpga.fpga_firmware.chfpga.ct_engine.cge.CGE`, so on a CRS array::

    ca.set_fpga_tx(*ca.nearest_tx_codes(950, 0.01, 0))   # FPGA TX swing / pre-cursor / post-cursor
    ca.read_optical()                                     # optical module SI settings and latched flags
    ca.set_optical(adaptive=False, tx_eq=5)               # manual optical module settings (volatile)
    ca.retrain_optical()                                  # module TX input equalizer adapts again
    ca.restore_si_defaults()

Every method takes ``serials=`` (e.g. ``['0224']``) to act on some boards only. FPGA TX drive and RX LPM need a
bitstream with the CGE TX drive control bytes; on older firmware the CGE methods refuse to write.
"""

import time

from pychfpga.common.array_methods import ArrayMethods


class CGEArrayMethods(ArrayMethods):
    """Array-wide FPGA TX / RX equalizer and QSFP optical module signal-integrity controls."""

    RPC_TRIES = 3              # FPGA control-register access over TCPipe can time out; the writes are idempotent
    RPC_RETRY_DELAY = 0.1      # s

    def _boards(self, serials):
        return [ib for ib in self.ib if getattr(ib, 'GPU', None) is not None and (serials is None or ib.serial in serials)]

    def _with_module(self, serials, log=print):
        """Boards whose QSFP cage has a module (ModPrsL). An EEPROM access to an empty cage times out (~10 s per try)."""
        out = []
        for ib in self._boards(serials):
            if ib.i2c1_qsfp.is_present():
                out.append(ib)
            else:
                log(f'  SN{ib.serial}: no QSFP module in the cage; skipped')
        return out

    def _rpc(self, fn):
        """Run fn(), retrying on OSError (TimeoutError included)."""
        for attempt in range(self.RPC_TRIES):
            try:
                return fn()
            except OSError as e:
                if attempt == self.RPC_TRIES - 1:
                    raise
                self.logger.warning(f'CGE SI: control access failed ({e}); retry {attempt + 1} of {self.RPC_TRIES - 1}')
                time.sleep(self.RPC_RETRY_DELAY)

    # ------------------------------------------------------------------ FPGA TX / RX

    @staticmethod
    def nearest_tx_codes(swing_mv=390, pre_db=0.01, post_db=0.0):
        """(swing, pre, post) GTY codes nearest to the requested mV / dB values (UG578 tables in CGE)."""
        from .cge import CGE
        def near(table, x, n):
            return min(range(n), key=lambda c: abs(table[c] - x))
        return near(CGE.TXDIFF_MV, swing_mv, 32), near(CGE.TXPRE_DB, pre_db, 21), near(CGE.TXPOST_DB, post_db, 32)

    def set_fpga_tx(self, swing=0, pre=0, post=0, lanes=range(4), serials=None):
        """Set FPGA TX swing / pre-cursor / post-cursor codes (0-31) on `lanes` of each board. Returns the read-back."""
        for ib in self._boards(serials):
            for lane in lanes:
                self._rpc(lambda: ib.GPU.set_tx_drive(lane, swing=swing, pre=pre, post=post))
        from .cge import CGE
        print(f'FPGA TX set: {CGE.TXDIFF_MV[swing]} mV, pre {CGE.TXPRE_DB[pre]} dB, post {CGE.TXPOST_DB[post]} dB (codes {swing}, {pre}, {post})')
        return self.get_fpga_tx(serials=serials)

    def get_fpga_tx(self, serials=None, verbose=True):
        """{serial: [(swing, pre, post) codes of lanes 0-3]}."""
        out = {}
        for ib in self._boards(serials):
            out[ib.serial] = [self._rpc(lambda: ib.GPU.get_tx_drive(lane)) for lane in range(4)]
            if verbose:
                print(f'  SN{ib.serial} FPGA TX codes lanes 0-3: {out[ib.serial]}')
        return out

    def set_fpga_rx_lpm(self, enable, serials=None):
        """FPGA receiver equalizer on all 4 lanes: LPM (True) or DFE (False). Affects only what the CRS receives."""
        for ib in self._boards(serials):
            self._rpc(lambda: ib.GPU.set_rx_lpm(enable))
        print('FPGA RX equalizer:', 'LPM' if enable else 'DFE')

    # ------------------------------------------------------------------ optical module

    def read_optical(self, serials=None, verbose=True):
        """Optical module identity, SI capabilities, page-03h settings and latched TX/RX flags (reading clears them)."""
        out = {}
        for ib in self._boards(serials):
            q = ib.i2c1_qsfp
            if not q.is_present():
                out[ib.serial] = dict(present=False)
                if verbose:
                    print(f'  SN{ib.serial}: no QSFP module in the cage')
                continue
            s = dict(present=True, vendor=bytes(q.read(148, 16)).decode(errors='replace').strip(),
                     part=bytes(q.read(168, 16)).decode(errors='replace').strip(), **q.get_si_capabilities())
            if s['paged']:
                s.update(tx_eq_db=q.get_tx_input_eq(), rx_emphasis_db=q.get_rx_output_emphasis(),
                         rx_amplitude=q.get_rx_output_amplitude(), tx_adaptive_eq=q.get_tx_adaptive_eq())
            s['flags'] = q.get_signal_flags()
            out[ib.serial] = s
            if verbose:
                f = s['flags']
                print(f'  SN{ib.serial} {s["vendor"]} {s["part"]}: adaptive TX EQ capable {s["tx_adaptive_eq_capable"]}')
                if s['paged']:
                    print(f'    TX input EQ {s["tx_eq_db"]} dB (max {s["max_tx_eq_db"]}), adaptive ch1-4 {[int(a) for a in s["tx_adaptive_eq"]]}'
                          f' | RX emphasis {s["rx_emphasis_db"]} dB (max {s["max_rx_emphasis_db"]})'
                          f' | RX amplitude {s["rx_amplitude"]} (codes {s["rx_amplitude_codes"]})')
                print(f'    latched flags ch4..1: TX LOS {f["tx_los"]:04b} TX CDR LOL {f["tx_cdr_lol"]:04b} TX adaptive EQ fault '
                      f'{f["tx_adaptive_eq_fault"]:04b} | RX LOS {f["rx_los"]:04b} RX CDR LOL {f["rx_cdr_lol"]:04b}')
        return out

    def set_optical(self, tx_eq=None, rx_emph=None, rx_amp=None, adaptive=None, serials=None):
        """Set optical module SI settings; None leaves a setting unchanged. One value for all channels or a list for
        channels 1-4: tx_eq / rx_emph in dB, rx_amp as a code, adaptive True/False. Volatile. Returns read_optical()."""
        for ib in self._with_module(serials):
            q = ib.i2c1_qsfp
            if adaptive is not None:
                q.set_tx_adaptive_eq(adaptive)
            if tx_eq is not None:
                q.set_tx_input_eq(tx_eq)
            if rx_emph is not None:
                q.set_rx_output_emphasis(rx_emph)
            if rx_amp is not None:
                q.set_rx_output_amplitude(rx_amp)
        return self.read_optical(serials=serials)

    def retrain_optical(self, method='toggle', wait=1.0, serials=None):
        """Make the optical module TX input equalizer adapt again to the current FPGA signal.

        'toggle': adaptive enables off, then back (other settings kept; boards without adaptive EQ on are skipped).
        'reset': pulse ResetL (ALL module settings back to power-on values; the link drops briefly; waits >= 2 s).
        Prints the latched flags after the wait: TX CDR LOL set means the module input was acquired again."""
        bs = self._with_module(serials)
        for ib in bs:
            ib.i2c1_qsfp.get_signal_flags()                       # clear old latched flags
        for ib in bs:
            if method == 'toggle':
                if not ib.i2c1_qsfp.retrain_tx_adaptive_eq():
                    print(f'  SN{ib.serial}: adaptive TX EQ is off on all channels; nothing to retrain (use method="reset")')
            elif method == 'reset':
                ib.i2c1_qsfp.reset()
            else:
                raise ValueError(f'method must be "toggle" or "reset", not {method!r}')
        time.sleep(max(wait, 2.0) if method == 'reset' else wait)
        print(f'retrain ({method}) done; state after:')
        return self.read_optical(serials=serials)

    def restore_si_defaults(self, tx=(24, 0, 15), serials=None):
        """FPGA TX codes `tx` (default 24/0/15 = 950 mV, 0.01 dB, 3.99 dB: the chfpgalite reset value from 07c6572 on;
        older bitstreams reset to 24/0/0 or 0/0/0), FPGA RX DFE, optical module reset."""
        for ib in self._boards(serials):
            if self._rpc(ib.GPU.tx_drive_supported):
                for lane in range(4):
                    self._rpc(lambda: ib.GPU.set_tx_drive(lane, *tx))
                self._rpc(lambda: ib.GPU.set_rx_lpm(False))
            ib.i2c1_qsfp.reset()
        time.sleep(2)
        print(f'restored: FPGA TX codes {tuple(tx)}, FPGA RX DFE, optical modules reset to power-on settings')
