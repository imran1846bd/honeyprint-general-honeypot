"""HTTP servers that stay quiet when a client drops its connection.

Port scanners (nmap) connect and reset the connection before sending a request.
Python's socketserver then prints a full traceback for every such connection,
which looks like a crash although the service keeps running. The connection is
already recorded as evidence, so these errors are dropped; real errors still print.
"""

from http.server import ThreadingHTTPServer
import sys

DROPPED = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)


class QuietHandlingMixin:
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], DROPPED):
            return
        super().handle_error(request, client_address)


class QuietThreadingHTTPServer(QuietHandlingMixin, ThreadingHTTPServer):
    daemon_threads = True
