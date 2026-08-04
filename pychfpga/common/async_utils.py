# Standard Python packages
import asyncio


def run_async(awaitable):
    """
    Run an awaitable from synchronous code when no event loop is running.

    If a loop is already running (e.g. Jupyter), raises RuntimeError — await
    the coroutine from async code instead. Native asyncio does not allow
    nested run_until_complete / asyncio.run on a running loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError(
        "Event loop is already running; await the coroutine instead of "
        "calling run_async() / async_to_sync wrappers"
    )


def async_to_sync(fn):
    """
    Wraps an async coroutine function into a function that can be called
    synchronously (without the await statement) when no loop is running.
    """
    def sync_fn(*args, **kwargs):
        return run_async(fn(*args, **kwargs))
    return sync_fn
