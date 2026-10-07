"""A browser's parallel static-asset connections fit before accept resumes."""
import socket
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler

from codey.app.server import CodeyHTTPServer


def test_desktop_accept_queue_preserves_parallel_boot_asset_connections():
    server = CodeyHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        # No handler is accepted yet, representing a short scheduling pause
        # while the browser opens its boot assets and API connections.
        with ExitStack() as sockets:
            for _ in range(16):
                sockets.enter_context(socket.create_connection(server.server_address, timeout=0.3))
    finally:
        server.server_close()
