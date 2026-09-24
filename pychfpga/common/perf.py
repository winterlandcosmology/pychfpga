#!/usr/bin/python

"""Named wall-clock phase timing."""

import time


class Perf:
    """Named wall-clock phases, optionally nested.

    ``start(name)`` records a start time. By default it also stops the
    currently running sequential phase so successive ``start()`` calls form a
    chain of siblings at the same hierarchy level. Use ``stop_current=False``
    to time an overlapping phase.

    ``start(name, subtask=True)`` pushes a child of the current phase. The
    parent keeps running. If there is no current phase, the first subtask
    starts as a top-level phase (same as ``subtask=False``). ``stop()`` pops
    one level. Until ``stop()``, further ``start(..., subtask=True)`` calls
    stay under the current subtask.

    ``times`` maps ``path_tuple -> (start_time, stop_time, duration)``.
    ``stop_time`` and ``duration`` are ``None`` while the phase is running.
    Times are ``time.perf_counter()`` values. The display name is the last
    component of the path.
    """

    def __init__(self):
        self.times = {}
        self.descriptions = {}
        self._current = None

    def start(self, name, stop_current=True, description=None, subtask=False):
        """Start phase ``name``.

        Parameters:

            name (str): Phase name (last path component). Re-starting a
                finished path overwrites it.

            stop_current (bool): If True (default) and ``subtask`` is False,
                stop the current leaf and start ``name`` as a sibling (same
                parent). If False, start an overlapping timer without changing
                the current sequential phase.

            description (str): Optional text shown in :meth:`summary`.

            subtask (bool): If True, push ``name`` under the current phase.
                Does not stop the parent. With no current phase, starts
                ``name`` at the top level instead.
        """
        now = time.perf_counter()
        if subtask and self._current is not None:
            key = self._current + (name,)
        else:
            parent = self._current[:-1] if self._current is not None else ()
            if stop_current and self._current is not None:
                self.stop()
            key = parent + (name,)
        if key in self.times:
            start, stop, _ = self.times[key]
            if stop is None:
                raise RuntimeError(f"Perf phase {key[-1]!r} is already running")
        self.times[key] = (now, None, None)
        self.descriptions[key] = description or ''
        if subtask or stop_current:
            self._current = key
        return now

    def stop(self, name=None):
        """Stop phase ``name`` (or the current leaf if omitted).

        With no argument, pops one hierarchy level. Returns the duration in
        seconds.
        """
        if name is None:
            key = self._current
        else:
            key = name if isinstance(name, tuple) else self._resolve_key(name)
        if key is None:
            raise RuntimeError('Perf.stop() with no current phase')
        if key not in self.times:
            raise KeyError(f'Perf phase {name!r} was never started')
        start, stop, _ = self.times[key]
        if stop is not None:
            raise RuntimeError(f'Perf phase {key[-1]!r} is already stopped')
        now = time.perf_counter()
        duration = now - start
        self.times[key] = (start, now, duration)
        if self._current == key:
            parent = key[:-1]
            self._current = parent if parent else None
        return duration

    def _resolve_key(self, name):
        """Map a string or tuple to a ``times`` key."""
        if isinstance(name, tuple):
            if name not in self.times:
                raise KeyError(f'Perf phase {name!r} was never started')
            return name
        top = (name,)
        if top in self.times:
            return top
        matches = [key for key in self.times if key[-1] == name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f'Perf phase {name!r} was never started')
        raise KeyError(
            f'Perf phase {name!r} is ambiguous ({len(matches)} matches)')

    def get_delay(self, from_=None, to=None):
        """Elapsed seconds from one phase to another.

        Measured from the start of ``from_`` to the stop of ``to`` (or now
        if ``to`` is still running). ``from_=None`` is the first recorded
        phase; ``to=None`` is the last.

        ``from`` is a Python reserved word, so the argument is ``from_``.
        A string matches ``(name,)`` or a unique leaf name.

        Examples::

            perf.get_delay()
            perf.get_delay('ping', 'open platform')
            perf.get_delay(from_='hardware map')
            perf.get_delay(to='mezzanines')
        """
        if not self.times:
            raise RuntimeError('Perf.get_delay() with no recorded phases')
        names = list(self.times)
        if from_ is None:
            from_key = names[0]
        else:
            from_key = self._resolve_key(from_)
        if to is None:
            to_key = names[-1]
        else:
            to_key = self._resolve_key(to)
        from_start, _, _ = self.times[from_key]
        _, to_stop, _ = self.times[to_key]
        if to_stop is None:
            to_stop = time.perf_counter()
        return to_stop - from_start

    @staticmethod
    def _label(key):
        return '  ' * (len(key) - 1) + key[-1]

    def summary(self, stop_running=True):
        """Return a text table of phase start, stop, and duration.

        Times are seconds relative to the first ``start()``. If
        ``stop_running`` is True (default), still-running phases are stopped
        innermost-first so leftover subtasks do not leave parents running.

        A ``total`` row is the wall time from the earliest start to the
        latest stop. Nested phase names are indented by depth.
        """
        if stop_running:
            running = [
                key for key, (_, stop, _) in self.times.items()
                if stop is None]
            running.sort(key=len, reverse=True)
            for key in running:
                self.stop(key)
        if not self.times:
            return 'Perf: (no phases)'
        t0 = min(start for start, _, _ in self.times.values())
        labels = {key: self._label(key) for key in self.times}
        name_w = max(5, max(len(label) for label in labels.values()))
        desc_w = max(
            (len(self.descriptions.get(key, '')) for key in self.times),
            default=0)
        if desc_w:
            desc_w = max(desc_w, len('description'))
        header = f"{'phase':<{name_w}}  {'start':>8}  {'stop':>8}  {'duration':>8}"
        rule = f"{'-' * name_w}  {'-' * 8}  {'-' * 8}  {'-' * 8}"
        if desc_w:
            header += f"  {'description':<{desc_w}}"
            rule += f"  {'-' * desc_w}"
        lines = [header, rule]
        last_stop = t0
        for key, (start, stop, duration) in self.times.items():
            label = labels[key]
            desc = self.descriptions.get(key, '')
            if stop is None:
                row = (
                    f"{label:<{name_w}}  {start - t0:8.3f}  {'...':>8}  {'...':>8}")
            else:
                last_stop = max(last_stop, stop)
                row = (
                    f"{label:<{name_w}}  {start - t0:8.3f}  {stop - t0:8.3f}  "
                    f"{duration:8.3f}")
            if desc_w:
                row += f"  {desc:<{desc_w}}"
            lines.append(row)
        wall = last_stop - t0
        total = f"{'total':<{name_w}}  {0:8.3f}  {wall:8.3f}  {wall:8.3f}"
        if desc_w:
            total += f"  {'':<{desc_w}}"
        lines.append(rule)
        lines.append(total)
        return '\n'.join(lines)
