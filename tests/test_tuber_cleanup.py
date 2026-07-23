import asyncio

from pychfpga.hardware.ice.icecore.tuber import TuberObject


class LoopBoundSession:
    def __init__(self, loop):
        self.loop = loop
        self.closed = False

    async def close(self):
        assert asyncio.get_running_loop() is self.loop
        self.closed = True


def test_tuber_closes_sessions_on_their_owning_loops(monkeypatch):
    loops = [asyncio.new_event_loop(), asyncio.new_event_loop()]
    sessions = [LoopBoundSession(loop) for loop in loops]
    monkeypatch.setattr(
        TuberObject,
        "_tuber_client_sessions",
        dict(zip(loops, sessions)),
    )

    try:
        TuberObject._tuber_close_client_sessions()
    finally:
        for loop in loops:
            loop.close()

    assert all(session.closed for session in sessions)
    assert TuberObject._tuber_client_sessions == {}
