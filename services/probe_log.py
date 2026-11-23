"""Record connections that never became a session: port scans and service probes.

A port scanner (nmap's default connect scan) opens a TCP connection and closes it
without sending anything; service detection (nmap -sV) sends data that is not the
service's protocol. Neither produces a login or a request, so without this helper
the honeypot would keep no trace of them.
"""

import time


class CountingSocket:
    """Wrap an accepted socket and count the bytes the peer sent."""

    def __init__(self, sock):
        self._sock = sock
        self.received = 0
        self.started = time.monotonic()

    def recv(self, size, *flags):
        data = self._sock.recv(size, *flags)
        self.received += len(data)
        return data

    def recv_into(self, buffer, *args):
        count = self._sock.recv_into(buffer, *args)
        self.received += count
        return count

    def __getattr__(self, name):
        return getattr(self._sock, name)


def probe(service, port, peer, received, started):
    """Metadata and outcome for a connection that produced no session or login."""
    outcome = 'no_data' if not received else 'invalid'
    return outcome, {'service': service, 'port': port, 'peer': str(peer)[:64],
                     'bytes_received': int(received),
                     'duration_ms': int((time.monotonic() - started) * 1000)}
