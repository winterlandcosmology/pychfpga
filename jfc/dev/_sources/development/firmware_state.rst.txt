-----------------
chFPGA state tree
-----------------

``chfpga`` firmware modules use a shared ``State`` tree to expose durable,
semantic configuration to host-side consumers. The tree supports dotted access,
subtree conversion with ``as_dict()``, and snapshot-based change checks with
``ack()`` and ``has_changed()``.

Construction pattern
====================

Parent modules create state branches and pass them to child modules. Child
modules store the received branch on ``self.state`` and declare or update their
own values there.

For example, the root firmware object creates top-level branches::

    self.GPIO = gpio.GPIO(..., state=self.state.add("gpio"))
    self.chan = chan.ChanArray(..., state=self.state.add("chan"))

Intermediate containers continue the same pattern::

    self.FFT = fft.FFT(..., state=self.state.add("fft"))
    self.FUNCGEN = funcgen.FUNCGEN(..., state=self.state.add("funcgen"))

Leaf modules should require the state branch provided by their parent::

    def __init__(self, *, router, router_port, state):
        super().__init__(router=router, router_port=router_port)
        self.state = state

Register values
===============

MMI ``BitField`` assignments are not automatically tracked. Modules should
explicitly mirror stable software-visible configuration into ``self.state``
after hardware writes succeed. The ``FUNCGEN`` module is the pilot example:
``set_data_source()`` and ``set_output_shifting()`` update both hardware
registers and the corresponding state fields.

Change tracking
===============

Consumers keep independent baselines by acknowledging a node or selected
attributes with a context token::

    state.chan[3].funcgen.ack(context=receiver)
    changed = state.chan[3].funcgen.has_changed(context=receiver, ack=True)
    gain_changed = state.chan[3].scaler.has_changed("gain", context=receiver)

``attrs=None`` means all keys on that state node. A string or integer checks one
attribute or index. A sequence checks a subset. ``None`` is also a valid context
token for callers that only need one default baseline.
