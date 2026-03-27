import asyncio
import signal

from aiohttp import web

from pychfpga.monitoring_tools.common import resolve_access_host


async def handle_index(request):
    return web.Response(
        text=request.app['index_html'],
        content_type='text/html',
        headers={'Cache-Control': 'no-store'},
    )


async def handle_latest(request):
    board_key = request.query.get('board')
    return web.json_response(
        request.app['state'].serialize(board_key=board_key),
        headers={'Cache-Control': 'no-store'},
    )


def build_app(state, index_html):
    app = web.Application()
    app['state'] = state
    app['index_html'] = index_html
    app.add_routes([
        web.get('/', handle_index),
        web.get('/api/latest', handle_latest),
    ])
    return app


class MonitorServer:
    def __init__(self, state, index_html, host, port):
        self.state = state
        self.index_html = index_html
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.site = None
        self.url = None
        self.actual_port = None

    async def start(self):
        if self.runner is not None:
            return self.url

        self.app = build_app(self.state, self.index_html)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host=self.host, port=self.port)
        await self.site.start()

        server = self.site._server
        actual_port = self.port
        if server is not None and server.sockets:
            actual_port = server.sockets[0].getsockname()[1]

        access_host = resolve_access_host(self.host)
        self.actual_port = actual_port
        self.url = f'http://{access_host}:{actual_port}/'
        return self.url

    async def stop(self):
        if self.runner is None:
            return

        try:
            await self.runner.cleanup()
        finally:
            self.app = None
            self.runner = None
            self.site = None
            self.url = None
            self.actual_port = None


async def wait_for_shutdown_signal():
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals = []

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
            registered_signals.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    try:
        await shutdown_event.wait()
    finally:
        for sig in registered_signals:
            loop.remove_signal_handler(sig)


async def run_monitor_server(state, index_html, host, port, console_print, service_name='Monitor'):
    server = MonitorServer(state=state, index_html=index_html, host=host, port=port)

    try:
        url = await server.start()
        console_print(f'{service_name} available at {url}')
        console_print('Press Ctrl+C to stop the monitor server.')
        await wait_for_shutdown_signal()
    finally:
        await server.stop()
