import json
import struct
import warnings
from functools import partial

import requests
import requests.exceptions
import six
import websocket

from .build import BuildApiMixin
from .container import ContainerApiMixin
from .daemon import DaemonApiMixin
from .exec_api import ExecApiMixin
from .image import ImageApiMixin
from .network import NetworkApiMixin
from .plugin import PluginApiMixin
from .secret import SecretApiMixin
from .service import ServiceApiMixin
from .swarm import SwarmApiMixin
from .volume import VolumeApiMixin
from .. import auth
from ..constants import (
    DEFAULT_TIMEOUT_SECONDS, DEFAULT_USER_AGENT, IS_WINDOWS_PLATFORM,
    DEFAULT_DOCKER_API_VERSION, STREAM_HEADER_SIZE_BYTES, DEFAULT_NUM_POOLS,
    MINIMUM_DOCKER_API_VERSION
)
from ..errors import (
    DockerException, TLSParameterError,
    create_api_error_from_http_exception
)
from ..tls import TLSConfig
from ..transport import SSLAdapter, UnixAdapter
from ..utils import utils, check_resource, update_headers
from ..utils.socket import frames_iter
from ..utils.json_stream import json_stream
try:
    from ..transport import NpipeAdapter
except ImportError:
    pass


class APIClient(
        requests.Session,
        BuildApiMixin,
        ContainerApiMixin,
        DaemonApiMixin,
        ExecApiMixin,
        ImageApiMixin,
        NetworkApiMixin,
        PluginApiMixin,
        SecretApiMixin,
        ServiceApiMixin,
        SwarmApiMixin,
        VolumeApiMixin):
    """
    A low-level client for the Docker Engine API.

    Example:

        >>> import docker
        >>> client = docker.APIClient(base_url='unix://var/run/docker.sock')
        >>> client.version()
        {u'ApiVersion': u'1.24',
         u'Arch': u'amd64',
         u'BuildTime': u'2016-09-27T23:38:15.810178467+00:00',
         u'Experimental': True,
         u'GitCommit': u'45bed2c',
         u'GoVersion': u'go1.6.3',
         u'KernelVersion': u'4.4.22-moby',
         u'Os': u'linux',
         u'Version': u'1.12.2-rc1'}

    Args:
        base_url (str): URL to the Docker server. For example,
            ``unix:///var/run/docker.sock`` or ``tcp://127.0.0.1:1234``.
        version (str): The version of the API to use. Set to ``auto`` to
            automatically detect the server's version. Default: ``1.24``
        timeout (int): Default timeout for API calls, in seconds.
        tls (bool or :py:class:`~docker.tls.TLSConfig`): Enable TLS. Pass
            ``True`` to enable it with default options, or pass a
            :py:class:`~docker.tls.TLSConfig` object to use custom
            configuration.
        user_agent (str): Set a custom user agent for requests to the server.
    """
    def __init__(self, base_url=None, version=None,
                 timeout=DEFAULT_TIMEOUT_SECONDS, tls=False,
                 user_agent=DEFAULT_USER_AGENT, num_pools=DEFAULT_NUM_POOLS):
        super(APIClient, self).__init__()

        if tls and not base_url:
            raise TLSParameterError(
                'If using TLS, the base_url argument must be provided.'
            )

        self.base_url = base_url
        self.timeout = timeout
        self.headers['User-Agent'] = user_agent

        self._auth_configs = auth.load_config()

        base_url = utils.parse_host(
            base_url, IS_WINDOWS_PLATFORM, tls=bool(tls)
        )
        if base_url.startswith('http+unix://'):
            self._custom_adapter = UnixAdapter(
                base_url, timeout, pool_connections=num_pools
            )
            self.mount('http+docker://', self._custom_adapter)
            self._unmount('http://', 'https://')
            self.base_url = 'http+docker://localunixsocket'
        elif base_url.startswith('npipe://'):
            if not IS_WINDOWS_PLATFORM:
                raise DockerException(
                    'The npipe:// protocol is only supported on Windows'
                )
            try:
                self._custom_adapter = NpipeAdapter(
                    base_url, timeout, pool_connections=num_pools
                )
            except NameError:
                raise DockerException(
                    'Install pypiwin32 package to enable npipe:// support'
                )
            self.mount('http+docker://', self._custom_adapter)
            self.base_url = 'http+docker://localnpipe'
        else:
            # Use SSLAdapter for the ability to specify SSL version
            if isinstance(tls, TLSConfig):
                tls.configure_client(self)
            elif tls:
                self._custom_adapter = SSLAdapter(pool_connections=num_pools)
                self.mount('https://', self._custom_adapter)
            self.base_url = base_url

        # version detection needs to be after unix adapter mounting
        if version is None:
            self._version = DEFAULT_DOCKER_API_VERSION
        elif isinstance(version, six.string_types):
            if version.lower() == 'auto':
                self._version = self._retrieve_server_version()
            else:
                self._version = version
        else:
            raise DockerException(
                'Version parameter must be a string or None. Found {0}'.format(
                    type(version).__name__
                )
            )
        if utils.version_lt(self._version, MINIMUM_DOCKER_API_VERSION):
            warnings.warn(
                'The minimum API version supported is {}, but you are using '
                'version {}. It is recommended you either upgrade Docker '
                'Engine or use an older version of Docker SDK for '
                'Python.'.format(MINIMUM_DOCKER_API_VERSION, self._version)
            )

    def _retrieve_server_version(self):
        try:
            return self.version(api_version=False)["ApiVersion"]
        except KeyError:
            raise DockerException(
                'Invalid response from docker daemon: key "ApiVersion"'
                ' is missing.'
            )
        except Exception as e:
            raise DockerException(
                'Error while fetching server API version: {0}'.format(e)
            )

    def _set_request_timeout(self, kwargs):
        """Prepare the kwargs for an HTTP request by inserting the timeout
        parameter, if not already present."""
        kwargs.setdefault('timeout', self.timeout)
        return kwargs

    @update_headers
    def _post(self, url, **kwargs):
        return self.post(url, **self._set_request_timeout(kwargs))

    @update_headers
    def _get(self, url, **kwargs):
        return self.get(url, **self._set_request_timeout(kwargs))

    @update_headers
    def _put(self, url, **kwargs):
        return self.put(url, **self._set_request_timeout(kwargs))

    @update_headers
    def _delete(self, url, **kwargs):
        return self.delete(url, **self._set_request_timeout(kwargs))

    def _url(self, pathfmt, *args, **kwargs):
        for arg in args:
            if not isinstance(arg, six.string_types):
                raise ValueError(
                    'Expected a string but found {0} ({1}) '
                    'instead'.format(arg, type(arg))
                )

        quote_f = partial(six.moves.urllib.parse.quote_plus, safe="/:")
        args = map(quote_f, args)

        if kwargs.get('versioned_api', True):
            return '{0}/v{1}{2}'.format(
                self.base_url, self._version, pathfmt.format(*args)
            )
        else:
            return '{0}{1}'.format(self.base_url, pathfmt.format(*args))

    def _raise_for_status(self, response):
        """Raises stored :class:`APIError`, if one occurred."""
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise create_api_error_from_http_exception(e)

    def _result(self, response, json=False, binary=False):
        assert not (json and binary)
        self._raise_for_status(response)

        if json:
            return response.json()
        if binary:
            return response.content
        return response.text

    def _post_json(self, url, data, **kwargs):
        # Go <1.1 can't unserialize null to a string
        # so we do this disgusting thing here.
        data2 = {}
        if data is not None and isinstance(data, dict):
            for k, v in six.iteritems(data):
                if v is not None:
                    data2[k] = v
        elif data is not None:
            data2 = data

        if 'headers' not in kwargs:
            kwargs['headers'] = {}
        kwargs['headers']['Content-Type'] = 'application/json'
        return self._post(url, data=json.dumps(data2), **kwargs)

    def _attach_params(self, override=None):
        return override or {
            'stdout': 1,
            'stderr': 1,
            'stream': 1
        }

    @check_resource
    def _attach_websocket(self, container, params=None):
        url = self._url("/containers/{0}/attach/ws", container)
        req = requests.Request("POST", url, params=self._attach_params(params))
        full_url = req.prepare().url
        full_url = full_url.replace("http://", "ws://", 1)
        full_url = full_url.replace("https://", "wss://", 1)
        return self._create_websocket_connection(full_url)

    def _create_websocket_connection(self, url):
        return websocket.create_connection(url)

    def _get_raw_response_socket(self, response):
        self._raise_for_status(response)
        if self.base_url == "http+docker://localnpipe":
            sock = response.raw._fp.fp.raw.sock
        elif six.PY3:
            sock = response.raw._fp.fp.raw
            if self.base_url.startswith("https://"):
                sock = sock._sock
        else:
            sock = response.raw._fp.fp._sock
        try:
            # Keep a reference to the response to stop it being garbage
            # collected. If the response is garbage collected, it will
            # close TLS sockets.
            sock._response = response
        except AttributeError:
            # UNIX sockets can't have attributes set on them, but that's
            # fine because we won't be doing TLS over them
            pass

        return sock

    def _stream_helper(self, response, decode=False):
        """Generator for data coming from a chunked-encoded HTTP response."""

        if response.raw._fp.chunked:
            if decode:
                for chunk in json_stream(self._stream_helper(response, False)):
                    yield chunk
            else:
                reader = response.raw
                while not reader.closed:
                    # this read call will block until we get a chunk
                    data = reader.read(1)
                    if not data:
                        break
                    if reader._fp.chunk_left:
                        data += reader.read(reader._fp.chunk_left)
                    yield data
        else:
            # Response isn't chunked, meaning we probably
            # encountered an error immediately
            yield self._result(response, json=decode)

    def _multiplexed_buffer_helper(self, response):
        """A generator of multiplexed data blocks read from a buffered
        response."""
        buf = self._result(response, binary=True)
        buf_length = len(buf)
        walker = 0
        while True:
            if buf_length - walker < STREAM_HEADER_SIZE_BYTES:
                break
            header = buf[walker:walker + STREAM_HEADER_SIZE_BYTES]
            _, length = struct.unpack_from('>BxxxL', header)
            start = walker + STREAM_HEADER_SIZE_BYTES
            end = start + length
            walker = end
            yield buf[start:end]

    def _multiplexed_response_stream_helper(self, response):
        """A generator of multiplexed data blocks coming from a response
        stream."""

        # Disable timeout on the underlying socket to prevent
        # Read timed out(s) for long running processes
        socket = self._get_raw_response_socket(response)
        self._disable_socket_timeout(socket)

        while True:
            header = response.raw.read(STREAM_HEADER_SIZE_BYTES)
            if not header:
                break
            _, length = struct.unpack('>BxxxL', header)
            if not length:
                continue
            data = response.raw.read(length)
            if not data:
                break
            yield data

    def _stream_raw_result_old(self, response):
        ''' Stream raw output for API versions below 1.6 '''
        self._raise_for_status(response)
        for line in response.iter_lines(chunk_size=1,
                                        decode_unicode=True):
            # filter out keep-alive new lines
            if line:
                yield line

    def _stream_raw_result(self, response):
        ''' Stream result for TTY-enabled container above API 1.6 '''
        self._raise_for_status(response)
        for out in response.iter_content(chunk_size=1, decode_unicode=True):
            yield out

    def _read_from_socket(self, response, stream):
        socket = self._get_raw_response_socket(response)

        if stream:
            return frames_iter(socket)
        else:
            return six.binary_type().join(frames_iter(socket))

    def _disable_socket_timeout(self, socket):
        """ Depending on the combination of python version and whether we're
        connecting over http or https, we might need to access _sock, which
        may or may not exist; or we may need to just settimeout on socket
        itself, which also may or may not have settimeout on it. To avoid
        missing the correct one, we try both.

        We also do not want to set the timeout if it is already disabled, as
        you run the risk of changing a socket that was non-blocking to
        blocking, for example when using gevent.
        """
        sockets = [socket, getattr(socket, '_sock', None)]

        for s in sockets:
            if not hasattr(s, 'settimeout'):
                continue

            timeout = -1

            if hasattr(s, 'gettimeout'):
                timeout = s.gettimeout()

            # Don't change the timeout if it is already disabled.
            if timeout is None or timeout == 0.0:
                continue

            s.settimeout(None)

    def _get_result(self, container, stream, res):
        cont = self.inspect_container(container)
        return self._get_result_tty(stream, res, cont['Config']['Tty'])

    def _get_result_tty(self, stream, res, is_tty):
        # Stream multi-plexing was only introduced in API v1.6. Anything
        # before that needs old-style streaming.
        if utils.compare_version('1.6', self._version) < 0:
            return self._stream_raw_result_old(res)

        # We should also use raw streaming (without keep-alives)
        # if we're dealing with a tty-enabled container.
        if is_tty:
            return self._stream_raw_result(res) if stream else \
                self._result(res, binary=True)

        self._raise_for_status(res)
        sep = six.binary_type()
        if stream:
            return self._multiplexed_response_stream_helper(res)
        else:
            return sep.join(
                [x for x in self._multiplexed_buffer_helper(res)]
            )

    def _unmount(self, *args):
        for proto in args:
            self.adapters.pop(proto)

    def get_adapter(self, url):
        try:
            return super(APIClient, self).get_adapter(url)
        except requests.exceptions.InvalidSchema as e:
            if self._custom_adapter:
                return self._custom_adapter
            else:
                raise e

    @property
    def api_version(self):
        return self._version
