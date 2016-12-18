#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#   Copyright 2016 Futur Solo
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.


"""
Server State :
stream_created(writer) -> event_received() * n -> stream_closed()
RequestReceived --> DataReceived * n --> EOFReceived.
                |
                --> UpgradeRequested --> accept_upgrade() --> stream_closed()
                                     |
                                     --> decline_upgrade() --> stream_closed()
"""


from typing import (
    Mapping, Optional, Callable, Any, Union, MutableMapping, Tuple)

from .utils import Identifier, cached_property
from . import log
from . import compat
from . import httpabc
from . import streams
from . import encoding
from . import httputils
from . import magicdict
from . import httpevents

from ._version import version as futurefinity_version

import abc
import sys
import enum
import asyncio
import inspect
import threading

_log = log.get_child_logger("h1connection")

_DEFAULT_MARK = Identifier()

_SELF_IDENTIFIER = "futurefinity/" + futurefinity_version


class CapitalizedH1Headers(dict):
    """
    Convert a string to HTTP Header style capitalized string.

    .. code-block:: python3

      >>> capitalize_header = CapitalizedHTTPv1Header()
      >>> capitalize_header["set-cookie"]
      'Set-Cookie'
      >>> capitalize_header["SET-COOKIE"]
      'Set-Cookie'
      >>> capitalize_header["sET-CooKIe"]
      'Set-Cookie'
      >>> capitalize_header["MY-cUsToM-heAdER"]
      'My-Custom-Header'
    """
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.update({
            "te": "TE",
            "age": "Age",
            "date": "Date",
            "etag": "ETag",
            "from": "From",
            "host": "Host",
            "vary": "Vary",
            "allow": "Allow",
            "range": "Range",
            "accept": "Accept",
            "cookie": "Cookie",
            "expect": "Expect",
            "server": "Server",
            "referer": "Referer",
            "if-match": "If-Match",
            "if-range": "If-Range",
            "location": "Location",
            "connection": "Connection",
            "keep-alive": "Keep-Alive",
            "set-cookie": "Set-Cookie",
            "user-agent": "User-Agent",
            "content-md5": "Content-MD5",
            "retry-after": "Retry-After",
            "content-type": "Content-Type",
            "max-forwards": "Max-Forwards",
            "accept-ranges": "Accept-Ranges",
            "authorization": "Authorization",
            "content-range": "Content-Range",
            "if-none-match": "If-None-Match",
            "last-modified": "Last-Modified",
            "accept-charset": "Accept-Charset",
            "content-length": "Content-Length",
            "accept-encoding": "Accept-Encoding",
            "accept-language": "Accept-Language",
            "content-encoding": "Content-Encoding",
            "www-authenticate": "WWW-Authenticate",
            "if-modified-since": "If-Modified-Since",
            "proxy-authenticate": "Proxy-Authenticate",
            "content-disposition": "Content-Disposition",
            "if-unmodified-since": "If-Unmodified-Since",
            "proxy-authorization": "Proxy-Authorization",
        })
        self._mutex_lock = threading.Lock()

    def __getitem__(self, key: Text) -> Text:
        key = key.lower()

        if key not in self:
            with self._mutex_lock:
                self[key] = key.title()

        return dict.__getitem__(self, key)

capitalize_h1_header = CapitalizedH1Headers()


class _H1Request(httpabc.AbstractHTTPRequest):
    def __init__(
        self, *, method: compat.Text,
        uri: compat.Text, authority: Optional[compat.Text]=None,
        scheme: Optional[compat.Text]=None,
            headers: Optional[Mapping[compat.Text, compat.Text]]=None):
        self._method = method
        self._uri = uri
        self._headers = magicdict.TolerantMagicDict()

        if headers:
            self._headers.update(headers)

        if authority:
            self._headers["host"] = authority

        self._scheme = scheme

    @property
    def headers(self) -> Mapping[compat.Text, compat.Text]:
        return self._headers

    @property
    def method(self) -> compat.Text:
        return self._method

    @property
    def uri(self) -> compat.Text:
        return self._uri

    @property
    def authority(self) -> compat.Text:
        authority = self.headers.get("host", _DEFAULT_MARK)

        if authority is not _DEFAULT_MARK:
            return authority

        raise AttributeError("Authority(Host) is not set.")

    @property
    def scheme(self) -> compat.Text:
        """
        This is not mandatory in HTTP/1.1 since the scheme is not included
        in the request initial.
        """
        if self._scheme is None:
            raise AttributeError("Scheme is not set.")

        return self._scheme


class _H1Response(httpabc.AbstractHTTPResponse):
    def __init__(
        self, *, status_code: int,
            headers: Optional[Mapping[compat.Text, compat.Text]]=None):
        self._status_code = status_code
        self._headers = magicdict.TolerantMagicDict()

        if headers:
            self._headers.update(headers)

    @property
    def headers(self) -> Mapping[compat.Text, compat.Text]:
        return self._headers

    @property
    def status_code(self) -> int:
        return self._status_code


class H1Context(httpabc.AbstractHTTPContext):
    def __init__(
        self, is_client: bool, *, idle_timeout: int=10,
        max_initial_length: int=8 * 1024,  # 8K
        allow_keep_alive: bool=True,
        chunk_size: int=10 * 1024,  # 10K
            ):
        self._is_client = is_client
        self._idle_timeout = idle_timeout
        self._max_initial_length = max_initial_length
        self._allow_keep_alive = allow_keep_alive
        self._chunk_size = chunk_size

    @property
    def is_client(self) -> bool:
        return self._is_client

    @property
    def idle_timeout(self) -> int:
        return self._idle_timeout

    @property
    def max_initial_length(self) -> int:
        return self._max_initial_length

    @property
    def allow_keep_alive(self) -> bool:
        return self._allow_keep_alive

    @property
    def chunk_size(self) -> int:
        return self._chunk_size


class _H1ConnnectionVariables:
    def __init__(
        self, tcp_stream: streams.AbstractStream,
            handler_factory: Callable[[], httpabc.AbstractHTTPStreamHandler]):
        self._http_version = 11
        self._can_keep_alive = True

        self._tcp_stream = tcp_stream
        self._handled_stream_num = 0

        self._handler_factory = handler_factory

    @property
    def http_version(self) -> int:
        return self._http_version

    @property
    def tcp_stream(self) -> Optional[streams.AbstractStream]:
        return self._tcp_stream

    @property
    def can_keep_alive(self) -> bool:
        return self._can_keep_alive

    @property
    def handled_stream_num(self) -> int:
        return self._handled_stream_num

    def disable_keep_alive(self):
        self._can_keep_alive = False

    def downgrade_http_version(self):
        self._http_version = 10
        self.disable_keep_alive()

    def inc_handled_stream_num(self):
        self._handled_stream_num += 1

    def create_stream_handler(self) -> httpabc.AbstractHTTPStreamHandler:
        return self._handler_factory()

    def detach_stream(self) -> streams.AbstractStream:
        assert self._tcp_stream is not None

        self.disable_keep_alive()
        self._tcp_stream, tcp_stream = None, self._tcp_stream
        return tcp_stream


class _BaseH1InitialProcessor:
    def __init__(
            self, context: "H1Context", variables: _H1ConnnectionVariables):
        self._context = context
        self._variables = variables


class _H1BadInitialError(Exception):
    pass


class _H1InitialTooLargeError(Exception):
    pass


class _H1InitialParser(_BaseH1InitialProcessor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._read_len = 0
        self._line_separator = None
        self._parsed_basic_info_tuple = None
        self._parsed_headers = None
        self._parsed_initial = None

        self._exc = None

    @cached_property
    def parsed_initial(self) -> Union[_H1Request, _H1Response]:
        if self._parsed_initial is None:
            raise httpabc.InvalidHTTPOperationError(
                "The initial is not parsed properly.")

        return self._parsed_initial

    def _parse_http_version(self, http_version: compat.Text):
        http_version = http_version.strip().lower()
        if http_version == "http/1.1":
            pass  # nothing is going to happen.

        elif http_version == "http/1.0":
            self._variables.downgrade_http_version()

        else:
            raise ValueError("Unknown HTTP version.")

    async def _read_and_parse_basic_info(self):
        basic_info_line = await asyncio.wait_for(
            self._variables._tcp_stream.readuntil(b"\n", keep_separator=False),
            self.context.idle_timeout)

        basic_info_len = len(basic_info_line) + 1

        if basic_info_len > self._context.max_initial_length:
            raise _H1InitialTooLargeError(
                "The initial exceeded the initial limit.",
                total_length)

        self._read_len += basic_info_len

        if basic_info_line.endswith(b"\r"):
            # Remote is using CRLF.
            self._line_separator = b"\r\n"
            basic_info_line = basic_info_line[:-1]

        else:  # Remote is using LF.
            self._line_separator = b"\n"

        try:
            self._parsed_basic_info_tuple = encoding.ensure_str(
                basic_info_line, encoding="latin-1").split(" ", 2)
            # The basic info of a HTTP message uses latin-1 encoding.

        except Exception as e:
            raise _H1BadInitialError(
                "The basic info cannot be properly parsed.") from e

    async def _read_and_parse_headers(self):
        self._parsed_headers = magicdict.TolerantMagicDict()
        line_separator_len = len(self._line_separator)
        while True:
            header_line = await asyncio.wait_for(
                self._variables._tcp_stream.readuntil(
                    self._line_separator, keep_separator=False),
                self._context.idle_timeout)

            self._read_len += header_line + line_separator_length

            if not header_line:
                break  # Headers Completed.

            if self._read_len > self._context.max_initial_length:
                raise asyncio.LimitOverrunError(
                    "The intitial exceeded the initial limit.",
                    total_length)

            try:
                key, value = ensure_str(
                    header_line, encoding="latin-1").split(":", 1)
                # Headers of a HTTP message use latin-1 encoding.

            except Exception as e:
                raise H1BadMessageError(
                    "Headers cannot be properly parsed.") from e

            self._parsed_headers.add(key.strip(), value.strip())

        self._parsed_headers.freeze()

    def _parse_request(self):
        self._parse_http_version(
            self._parsed_basic_info_tuple[2])
        method = self._parsed_basic_info_tuple[0]
        uri = self._parsed_basic_info_tuple[1]

        self._parsed_initial = _H1Request(
            method=method, uri=uri, headers=self._parsed_headers)

    def _parse_response(self):
        self._parse_http_version(
            self._parsed_basic_info_tuple[0])
        try:
            status_code = int(self._parsed_basic_info_tuple[1])

        except Exception as e:
            raise _H1BadInitialError(
                "Unable to parse the status code.") from e

        self._parsed_initial = _H1Response(
            status_code=status_code, headers=self._parsed_headers)

    async def read_and_parse(self) -> Union[_H1Request, _H1Response]:
        if self._exc:
            raise self._exc

        if self._parsed_initial is not None:
            raise httpabc.InvalidHTTPOperationError(
                "The parser is not reusable.")

        try:
            await self._read_and_parse_basic_info()
            await self._read_and_parse_headers()

            if self._parsed_headers.get(
                    "connection", "close").lower().strip() != "keep-alive":
                self._variables.disable_keep_alive()

            if self.context.is_client:  # Client reads responses.
                self._parse_response()

            else:  # Server reads requests.
                self._parse_request()

            return self._parsed_initial

        except Exception as e:
            self._exc = e
            raise


class _H1InitialBuilder(_BaseH1InitialProcessor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._pending_data = []

        self._http_version = http_version

        self._justified_headers = None
        self._built_initial = None

        self._exc = None

    @cached_property
    def http_version(self) -> int:
        return self._http_version

    @cached_property
    def _http_version_str(self) -> int:
        if self.http_version == 11:
            return "HTTP/1.1"

        else:  # HTTP Version Cannot be something else than 10 or 11.
            return "HTTP/1.0"

    @cached_property
    def built_initial(self) -> Union[_H1Request, _H1Response]:
        if self._built_initial is None:
            raise httpabc.InvalidHTTPOperationError(
                "The initial is not built properly.")

        return self._built_initial

    def _build_request_basic_info(self, request: _H1Request):
        self._pending_data.append("{} {} {}\r\n".format(
            request.method, request.uri, self._http_version_str))

    def _build_response_basic_info(self, response: _H1Response):
        self._pending_data.append("{} {} {}\r\n".format(
            self._http_version_str,
            encoding.ensure_str(response.status_code),
            httputils.status_code_descriptions[status_code]))

    def _justify_request_headers(
            self, headers: Mapping[compat.Text, compat.Text]):
        assert self._justified_headers is None
        self._justified_headers = magicdict.TolerantMagicDict()
        self._justified_headers.update(headers)

        self._justified_headers.setdefault("accept", "*/*")
        self._justified_headers.setdefault("user-agent", _SELF_IDENTIFIER)

        if "connection" not in self._justified_headers.keys():
            if self._variables.can_keep_alive:
                self._justified_headers["connection"] = "Keep-Alive"

            else:
                self._justified_headers["connection"] = "Close"

    def _justify_response_headers(
            self, headers: Mapping[compat.Text, compat.Text]):
        assert self._justified_headers is None
        self._justified_headers = magicdict.TolerantMagicDict()
        self._justified_headers.update(headers)

        if status_code >= 400:
            self._justified_headers["connection"] = "Close"

        if "connection" not in self._justified_headers.keys():
            if self._variables.can_keep_alive:
                self._justified_headers["connection"] = "Keep-Alive"

            else:
                self._justified_headers["connection"] = "Close"

        self._justified_headers.setdefault("server", _SELF_IDENTIFIER)

        if "content-length" not in headers.keys():
            if self.http_version == 11:
                self._justified_headers.setdefault(
                    "transfer-encoding", "Chunked")
                # Auto Chunked Content Transfer is only enabled for responses.

    def _build_justified_headers(self):
        for key, value in self._justified_headers.items():
            self._pending_data.append(
                "{}: {}\r\n".format(capitalize_h1_header[key], value))

        self._justified_headers.freeze()

    def _write_pending_data(self):
        self._tcp_stream.write(encoding.ensure_bytes(
            "".join(self._pending_data), encoding="latin-1"))

    async def build_and_write(self, initial: Union[_H1Request, _H1Response]):
        if self._exc:
            raise self._exc

        if self._built_initial is not None:
            raise httpabc.InvalidHTTPOperationError(
                "The builder is not reusable.")

        if self.context.is_client:  # Client sends requests.
            assert isinstance(initial, _H1Request)

        else:  # Server sends responses.
            assert isinstance(initial, _H1Response)

        try:
            if self.context.is_client:  # Client sends requests.
                self._build_request_basic_info(initial)
                self._justify_request_headers(initial.headers)

            else:  # Server sends responses.
                self._build_response_basic_info(initial)
                self._justify_response_headers(initial.headers)

            if self._justified_headers.get(
                    "connection", "close").lower().strip() != "keep-alive":
                self._variables.disable_keep_alive()

            self._build_justified_headers()
            self._pending_data.append("\r\n")

            self._write_pending_data()
            await self._tcp_stream.drain()

            if self.context.is_client:
                self._built_initial = _H1Request(
                    method=initial.method, uri=initial.uri,
                    authority=getattr(initial, "authority", None),
                    scheme=getattr(initial, "scheme", None),
                    headers=self._justified_headers)

            else:  # Server-Side.
                self._built_initial = _H1Response(
                    status_code=initial.status_code,
                    headers=self._justified_headers)

        except Exception as e:
            self._exc = e
            raise


class _H1BodyPrematureEOFError(Exception):
    pass


class _H1BaseBodyStreamReader(streams.BaseStreamReader):
    def __init__(
        self, last_reader: streams.AbstractStreamReader,
            incoming: Union[_H1Request, _H1Response], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_reader = last_reader
        self._incoming = incoming


class _H1ContentLengthBodyStreamReader(_H1BaseBodyStreamReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._length_read = 0

    @cached_property
    def _total_length(self) -> int:
        try:
            return int(self._incoming.headers["content-length"])

        except Exception as e:
            raise _H1BadInitialError(
                "Cannot Parse content-length as an integer.") from e

    @property
    def _length_left(self) -> int:
        return self._total_length - self._length_read

    async def _fetch_data(self) -> bytes:
        if self._length_left == 0:
            raise streams.StreamEOFError

        try:
            while True:
                data = await self._last_reader.read(self._length_left)
                if data:  # Filter Empty Data.
                    self._length_read += len(data)
                    return data

        except streams.StreamEOFError as e:
            if self._length_left != 0:
                raise _H1BodyPrematureEOFError from e

            raise


class _H1ChunkedBodyStreamReader(_H1BaseBodyStreamReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._current_length_left = None
        self._crlf_dropped = False

        self._eof_reached = False

    async def _try_drop_crlf(self):
        if self._crlf_dropped:
            return

        if self._current_length_left > 0:
            return

        await self._last_reader.readexactly(2)  # Drop Redundant CRLF.
        self._crlf_dropped = True

    async def _read_next_length(self):
        if self._current_length_left > 0:
            return

        if self.has_eof():
            return

        await self._try_drop_crlf()

        length_str = await self._last_reader.readuntil(
            b"\r\n", keep_separator=False)

        length_str = list(
            httputils.parse_semicolon_header(length_str).keys())[0]
        # Drop the chunked extension.

        try:
            self._current_length_left = int(length_str, 16)

        except ValueError as e:
            # Not Valid Hexadecimal bytes
            raise _H1BadInitialError("Bad Chunk Length Received.") from e

        self._crlf_dropped = False

        if self._current_length_left == 0:
            raise streams.StreamEOFError

    async def _fetch_data(self) -> bytes:
        if self._eof_reached:
            raise streams.StreamEOFError

        try:
            await self._read_next_length()

        except streams.StreamEOFError:
            self._eof_reached = True
            await self._try_drop_crlf()
            raise

        try:
            while True:
                data = await self._last_reader.read(self._current_length_left)
                if data:  # Filter Empty Data.
                    self._current_length_left -= len(data)
                    return data

        except streams.StreamEOFError as e:
            raise _H1BodyPrematureEOFError from e


class _H1ReadUntilEOFBodyStreamReader(_H1BaseBodyStreamReader):
    async def _fetch_data(self) -> bytes:
        while True:
            data = await self._body_reader.read(65536)  # 64K.
            if data:  # Filter Empty Bytes.
                return data


class _H1EmptyBodyStreamReader(_H1BaseBodyStreamReader):
    def __init__(
        self, last_reader: Optional[streams.AbstractStreamReader],
        incoming: Optional[Union[_H1Request, _H1Response]],
            *args, **kwargs):
        super(streams.BaseStreamReader, self).__init__(*args, **kwargs)

    async def _fetch_data(self) -> bytes:
        raise streams.StreamEOFError


class _H1BaseBodyStreamWriter(streams.BaseStreamWriter):
    def __init__(
        self, last_writer: streams.AbstractStreamWriter,
            *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_writer = last_writer

    @abc.abstractmethod
    def _write_impl(self, data: bytes):
        raise NotImplementedError

    async def drain(self):
        await self._last_writer.drain()

    def can_write_eof(self) -> bool:
        return True

    @abc.abstractmethod
    def _write_eof_impl(self):
        raise NotImplementedError

    def _check_if_closed_impl(self) -> bool:
        return self.eof_written()

    def _close_impl(self):
        self.write_eof()  # For body writers, close == write_eof.

    @abc.abstractmethod
    async def wait_closed(self):
        """
        Wait the writer to close.
        """
        raise NotImplementedError

    def _abort_impl(self):
        self.close()  # For most body writers, abort == close.


class _H1ChunkedBodyStreamWriter(_H1BaseBodyStreamWriter):  # "{}\r\n"
    pass


class _H1WriteUntilEOFBodyStreamWriter(_H1BaseBodyStreamWriter):
    pass


class _H1EmptyBodyStreamWriter(_H1BaseBodyStreamWriter):
    def __init__(
        self, last_writer: Optional[streams.AbstractStreamWriter],
            *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_eof()

    def _write_impl(self, data: bytes):
        raise StreamEOFError("EOF written.")

    async def drain(self):
        return

    def _write_eof_impl(self):
        pass

    async def wait_closed(self):
        return


class _H1StreamWriter(
        httpabc.AbstractHTTPStreamWriter, streams.BaseStreamWriter):
    def __init__(self, http_stream: "_H1Stream"):
        self._http_stream = http_stream

        self._outgoing = None

        self._variables.inc_handled_stream_num()
        self._stream_id = self._variables.handled_stream_num

        self._body_writer = None

        self._eof_written = False

        self._closed = False

    @property
    def http_version(self) -> int:
        return self._variables.http_version

    @property
    def stream_id(self) -> int:
        """
        A positive stream id means this is a stream over a connection.

        In HTTP/1.x, it keeps tracking the number of handled streams in
        current connection.
        """
        return self._stream_id

    @property
    def context(self) -> AbstractHTTPContext:
        return self._http_stream.context

    @property
    def _variables(self) -> _H1ConnnectionVariables:
        return self._http_stream._variables

    @cached_property
    def request(self) -> _H1Request:
        try:
            if self.context.is_client:
                return self.outgoing

            else:
                return self.incoming

        except AttributeError:
            raise AttributeError("Request is not Ready.") from None

    @cached_property
    def response(self) -> _H1Response:
        try:
            if self.context.is_client:
                return self.incoming

            else:
                return self.outgoing

        except AttributeError:
            raise AttributeError("Response is not Ready.") from None

    @cached_property
    def incoming(self) -> Union[_H1Request, _H1Response]:
        if self._http_stream._incoming is None:
            raise AttributeError("Incoming is not Ready.")

        return self._http_stream._incoming

    @cached_property
    def outgoing(self) -> Union[_H1Request, _H1Response]:
        if self._outgoing is None:
            raise AttributeError("Outgoing is not Ready.")

        return self._outgoing

    async def send_response(
        self, *, status_code: int,
            headers: Optional[Mapping[compat.Text, compat.Text]]=None):
        assert not self.response_written(), "You can only write response once."
        if not self.context.is_client:
            raise httpabc.InvalidHTTPOperationError(
                "An HTTP client cannot send responses to the remote.")

        try:
            response = _H1Response(
                status_code=status_code, headers=headers)

            builder = _H1InitialBuilder(
                context=self.context, variables=self._variables)
            await builder.build_and_write(response)

            self._outgoing = builder.built_initial

        except:
            self.close()
            raise

    def response_written(self) -> bool:
        return self._response is not None

    async def _init_stream_writer(
            self, request: Optional[httpabc.AbstractHTTPRequest]=None):
        if self.context.is_client:
            assert request is not None
            builder = _H1InitialBuilder(
                context=self.context, variables=self._variables)
            await builder.build_and_write(request)
            self._outgoing = builder.built_initial

        else:
            assert request is None

    async def _accept_upgrade(
            self, headers: Mapping[compat.Text, compat.Text]):
        await self.send_response(status_code=101, headers=headers)

        assert self._body_writer is None
        self._body_writer = _H1EmptyBodyStreamWriter()
        # Prevent further writing.

    @abc.abstractmethod
    def _write_impl(self, data: bytes):
        assert self.response_written(), "You must write response first."
        raise NotImplementedError

    async def drain(self):
        if self._body_writer is not None:
            await self._body_writer.drain()

        # The TCP Stream cannot be detached before the body writer is ready.
        else:
            await self._variables.tcp_stream.drain()

    def can_write_eof(self) -> bool:
        return self._body_writer is not None

    @abc.abstractmethod
    def _write_eof_impl(self):
        assert self.response_written(), "You must write response first."
        raise NotImplementedError

    def eof_written(self) -> bool:
        if self._body_writer is None:
            return False

        return self._body_writer.eof_written()

    @abc.abstractmethod
    def _check_if_closed_impl(self) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    def _close_impl(self):
        """if not self._tcp_stream.is_closing():
            if not self._current_stream.at_eof():
                _log.warning(
                    "The body is not properly read, "
                    "tear down the connection.")
                self.close()
                self._tcp_stream.close()"""
        raise NotImplementedError

    @abc.abstractmethod
    async def wait_closed(self):
        """
        Wait the writer to close.
        """
        raise NotImplementedError

    def abort(self):
        if self._body_writer is not None:
            self._body_writer.abort()

        self._close_impl()

    def get_extra_info(
            self, name: compat.Text, default: Any=_DEFUALT_MARK) -> Any:

        if self._variables.tcp_stream is not None:
            return self._variables.tcp_stream.get_extra_info(name, default)

        elif default is _DEFUALT_MARK:
            raise KeyError(name)

        return default


class _H1UpgradeRequested(httpevents.UpgradeRequested):
    def __init__(
            self, proposed_protocol: compat.Text, http_stream: "_H1Stream"):
        self._proposed_protocol = proposed_protocol
        self._http_stream = http_stream

    @property
    def proposed_protocol(self) -> compat.Text:
        return self._proposed_protocol

    async def accept(
        self, headers: Optional[Mapping[compat.Text, compat.Text]]=None
            ) -> streams.AbstractStream:
        return await self._http_stream._accept_upgrade(headers)


class _H1UpgradeResponded(httpevents.UpgradeResponded):
    def __init__(
            self, proposed_protocol: compat.Text, http_stream: "_H1Stream"):
        self._proposed_protocol = proposed_protocol
        self._http_stream = http_stream

    @property
    def proposed_protocol(self) -> compat.Text:
        return self._proposed_protocol

    async def accept(self) -> streams.AbstractStream:
        return await self._http_stream._accept_upgrade()


class _H1ReadFinished(Exception):
    pass


class _H1Stream:
    def __init__(self, conn: "H1Connection"):
        self._conn = conn

        self._pending_incoming = None
        self._incoming = None

        self._writer = None

        self._pending_upgrade = False

        self._body_reader = None

        self._read_finished = False

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        return self._conn._loop

    @property
    def context(self) -> AbstractHTTPContext:
        return self._conn.context

    @property
    def _variables(self) -> _H1ConnnectionVariables:
        return self._conn._variables

    async def _read_incoming_initial(self):
        assert (self._pending_incoming and self._incoming) is None
        parser = _H1InitialParser(
            context=self.context, variables=self._variables)
        self._pending_incoming = await parser.read_and_parse()

    async def init_stream(
        self, request: Optional[httpabc.AbstractHTTPRequest]=None
            ) -> _H1StreamWriter:
        assert self._writer is None
        self._writer = _H1StreamWriter(self)

        await self._writer._init_stream_writer(request)
        if not self.context.is_client:
            # Read the request for server before return writer.
            await self._read_incoming_initial()

        return self._writer

    async def _accept_upgrade(
        self, headers: Optional[Mapping[compat.Text, compat.Text]]=None
            ) -> streams.AbstractStream:
        if not self._pending_upgrade:
            raise httpabc.InvalidHTTPOperationError(
                "There's no pending upgrade.")

        if self.context.is_client:
            assert headers is None

        else:
            new_headers = magicdict.TolerantMagicDict()
            if headers:
                new_headers.update(headers)

            await self._writer._accept_upgrade(headers)

        tcp_stream = self._variables.detach_stream()
        self._pending_upgrade = False

        return tcp_stream

    async def next_event(self) -> httpabc.AbstractEvent:
        if not self._incoming:
            if not self._pending_incoming:
                await self._read_incoming_initial()

            self._incoming, self._pending_incoming = \
                self._pending_incoming, None

            if self.context.is_client:
                return httpevents.ResponseReceived(self._incoming)

            else:
                return httpevents.RequestReceived(self._incoming)

        if not self._body_reader:  # Initialize Body Reader(s).
            if "connection" in self._incoming.headers.keys():
                conn_header = self._incoming.headers["connection"]
                if conn_header.lower() == "upgrade":  # Upgrade Found.
                    self._pending_upgrade = True
                    self._body_reader = _H1EmptyBodyStreamReader(
                        None, None, loop=self._loop)

                    proposed_protocol = self._incoming.headers.get(
                        "upgrade", "")

                    if self.context.is_client:
                        return _H1UpgradeResponded(
                            proposed_protocol=proposed_protocol,
                            http_stream=self)

                    else:
                        return _H1UpgradeRequested(
                            proposed_protocol=proposed_protocol,
                            http_stream=self)

            readers = []
            eof_determined = False

            if self.context.is_client:
                # HEAD requests and 204, 304 responses has no body.
                if self._writer.outgoing.method == "HEAD":
                    readers.append(_H1EmptyBodyStreamReader)
                    eof_determined = True

                elif self._incoming.status_code in (204, 304):
                    readers.append(_H1EmptyBodyStreamReader)
                    eof_determined = True

            if not eof_determined:  # Check Transfer-Encoding.
                if "transfer-encoding" in self._incoming.headers.keys():
                    transfer_encoding = httputils.parse_semicolon_header(
                        self._incoming.headers["transfer-encoding"])

                    last_transfer_encoding = list(
                        transfer_encoding.keys())[-1].strip()

                    if ("chunked" in transfer_encoding.keys() and
                            last_transfer_encoding != "chunked"):
                        raise _H1BadInitialError(
                            "Chunked transfer encoding found, "
                            "but not at last.")

                    if last_transfer_encoding == "identity":
                        if len(transfer_encoding) != 1:
                            raise _H1BadInitialError(
                                "Identity is not the only transfer encoding.")

                    if last_transfer_encoding == "chunked":
                        readers.append(_H1ChunkedBodyStreamReader)
                        eof_determined = True

            if not eof_determined:  # Check Content-Length.
                if "content-length" not in self._incoming.headers.keys():
                    if self.context.is_client:
                        if self._variables.can_keep_alive:
                            self._variables.disable_keep_alive()

                        readers.append(_H1ReadUntilEOFBodyStreamReader)
                        # Read until connection close.

                    else:  # On server-side.
                        raise _H1BadInitialError(
                            "Content-Length MUST be present in the Request.")

                else:
                    readers.append(_H1ContentLengthBodyStreamReader)

            assert len(readers) > 0
            last_reader = self._variables.tcp_stream

            for BodyReader in readers:
                last_reader = BodyReader(
                    last_reader=last_reader, incoming=self._incoming,
                    loop=self._loop)

            self._body_reader = last_reader

        if self._read_finished:
            raise _H1ReadFinished

        self._pending_upgrade = False

        try:
            while True:
                data = await self._body_reader.read(65536)  # 64K.
                if data:  # Filter Empty Bytes.
                    return httpevents.DataReceived(data)

        except streams.StreamEOFError:
            self._read_finished = True
            return httpevents.EOFReceived()


class H1Connection(httpabc.AbstractHTTPConnection):
    def __init__(
        self, context: H1Context, tcp_stream: streams.AbstractStream,
        handler_factory: Callable[[], httpabc.AbstractHTTPStreamHandler], *,
            loop: Optional[asyncio.AbstractEventLoop]=None):
        self._loop = loop or asyncio.get_event_loop()
        self._context = context

        self._variables = _H1ConnnectionVariables(
            tcp_stream=tcp_stream, handler_factory=handler_factory)

        self._closing = False

        self._serving_fur = None

        self._send_request_fur = None
        self._handler_for_request_fur = None

    @property
    def http_version(self) -> int:
        return self._variables.http_version

    @property
    def context(self) -> H1Context:
        return self._context

    async def send_request(
        self, *, method: compat.Text,
        uri: compat.Text, authority: Optional[compat.Text]=None,
        headers: Optional[Mapping[compat.Text, compat.Text]]=None
            ) -> AbstractHTTPStreamHandler:
        """
        Send a request.

        This method is only usable on client side.
        """
        if not self.context.is_client:
            raise httpabc.InvalidHTTPOperationError(
                "An HTTP server cannot send requests to the remote.")

        assert self._serving_fur is not None, \
            "The connection is not being handled."

        if not self._variables.tcp_stream:
                raise httpabc.InvalidHTTPOperationError(
                    "The tcp stream has been "
                    "detached from the connection.")

        if self._variables._tcp_stream.closed():
            self.close()

        if self.closed():
            raise httpabc.InvalidHTTPOperationError(
                "The connection has been closed.")

        if self._send_request_fur.done():
            raise RuntimeError(
                "HTTP/1.x can only have one stream per connection "
                "at the same time.")

        scheme = "http" if self._variables._tcp_stream.get_extra_info(
            "sslcontext", None) is None else "https"
        request = _H1Request(
            method=method, scheme=scheme, uri=uri, authority=authority,
            headers=headers)

        self._send_request_fur.set_result(request)

        self._handler_for_request_fur = compat.create_future(self._loop)

        handler = await self._handler_for_request_fur

        return handler

    async def start_serving(self):
        assert self._serving_fur is False, "The connection is serving."
        assert self._closing is False, "The connection is closing."

        self._serving_fur = compat.ensure_future(
            self._serve_until_close(self), loop=self._loop)

    async def _serve_until_close(self):
        try:
            while True:
                http_stream = _H1Stream(self)

                try:
                    if self.context.is_client:
                        while True:
                            try:
                                self._send_request_fur = compat.create_future(
                                    self._loop)
                                request = await self._send_request_fur

                            except asyncio.CancelledError:
                                continue

                            else:
                                break

                        writer = await http_stream.init_stream(request)

                    else:
                        writer = await http_stream.init_stream()

                except (streams.StreamEOFError, asyncio.IncompleteReadError,
                        ConnectionError):
                    break
                handler = self._variables.create_stream_handler()

                try:
                    maybe_awaitable = handler.stream_created(writer)
                    if inspect.isawaitable(maybe_awaitable):
                        await maybe_awaitable

                    if (self._handler_for_request_fur is not None and
                            not self._handler_for_request_fur.done()):
                        self._handler_for_request_fur.set_result(handler)

                    while True:
                        try:
                            event = await http_stream.next_event()

                        except (streams.StreamEOFError,
                                asyncio.IncompleteReadError, ConnectionError,
                                _H1ReadFinished):
                            break

                        maybe_awaitable = handler.event_received(event)
                        if inspect.isawaitable(maybe_awaitable):
                            await maybe_awaitable

                    await writer.wait_closed()

                    if not self._variables.can_keep_alive:
                        break  # Keep-Alive Disabled.

                    if not self._variables.tcp_stream:
                        break  # Detached by upgrading.

                    if self._variables.tcp_stream.closed():
                        break  # Stream Closed.

                except Exception as exc:
                    maybe_awaitable = handler.stream_closed(exc)
                    if inspect.isawaitable(maybe_awaitable):
                        await maybe_awaitable
                    # Log Exception of stream_closed() if available.
                    """_log.error(
                    "Error Occurred inside stream_closed.",
                    exc_info=sys.exc_info())"""

                else:
                    maybe_awaitable = handler.stream_closed(None)
                    if inspect.isawaitable(maybe_awaitable):
                        await maybe_awaitable
                    # Log Exception of stream_closed() if available.

        finally:
            self.close()

    def close(self):
        if self._closing:
            return

        self._closing = True

        self._variables.disable_keep_alive()

    async def wait_closed(self):
        try:
            await self._serving_fur

        except:
            pass

    def __end__(self):
        super().__end__()
        self.close()
