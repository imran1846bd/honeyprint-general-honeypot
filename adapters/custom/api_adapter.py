"""Observe an explicitly selected external HTTP API through an owned local bridge."""
import http.client
from urllib.parse import urlsplit
from pathlib import Path
from adapters.base_adapter import AdapterError
from adapters.custom.process_adapter import CustomProcessAdapter
from core.profile_loader import load_profile


def parse_api_url(value):
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Custom API requires an HTTP(S) base URL without credentials, query or fragment')
    if any(c.isspace() for c in value):
        raise ValueError('Custom API URL contains whitespace')
    url.port  # Validate the optional port.
    return url


class CustomAPIAdapter(CustomProcessAdapter):
    owned_platform_resources = False

    def __init__(self, *, api_url, health_path='/', runtime_dir=None):
        super().__init__(entry=Path(__file__), health_path=health_path, runtime_dir=runtime_dir)
        self.url = parse_api_url(api_url)

    def capabilities(self):
        return {'deployment': ['attach'], 'simulators': ['custom_api'], 'services': ['web_port'],
                'stages': [], 'recipes': {'attach': 'custom_api_attach'}}

    def validate(self, cfg, context):
        if not self.health_path.startswith('/') or len(self.health_path) > 256 or any(c in self.health_path for c in '\r\n'):
            raise AdapterError('Invalid custom API health path')
        load_profile(cfg['printer_profile'])

    def _start_backend(self):
        # The remote service is never launched, reconfigured or stopped here.
        pass

    def _connection(self, timeout):
        factory = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        return factory(self.url.hostname, self.url.port, timeout=timeout)

    def _target_path(self, path):
        return self.url.path.rstrip('/') + path

    def health(self, instance):
        if instance is not self or self.server is None or self.failed:
            return {'ready': False}
        connection = self._connection(2)
        try:
            connection.request('GET', self._target_path(self.health_path))
            response = connection.getresponse()
            return {'ready': 200 <= response.status < 400}
        except (OSError, http.client.HTTPException):
            return {'ready': False}
        finally:
            connection.close()
