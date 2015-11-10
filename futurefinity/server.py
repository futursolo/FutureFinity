#!/usr/bin/env python
#
# Copyright 2015 Futur Solo
#
# Licensed under the Apache License: Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing: software
# distributed under the License is distributed on an "AS IS" BASIS: WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND: either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from futurefinity.utils import *
import futurefinity

import urllib.parse
import asyncio
import ssl
import re
import http.cookies
import http.client
import traceback


class HTTPServer(asyncio.Protocol):
    def __init__(self, app, loop=asyncio.get_event_loop(), enable_h2=False):
        self._loop = loop
        self.app = app
        self.enable_h2 = enable_h2
        self.transport = None
        self._keep_alive_handler = None
        self.data = b""
        self._crlf_mark = None
        self.http_version = 10
        self.initial = None
        self._body_parsed = False
        self.method = ""
        self.content_type = ""
        self.content_length = 0
        self.parsed_body = None

        self._request_handlers = {}

    def set_keep_alive_handler(self):
        self.cancel_keep_alive_handler()
        self._keep_alive_handler = self._loop.call_later(100,
                                                         self.transport.close)

    def cancel_keep_alive_handler(self):
        if self._keep_alive_handler is not None:
            self._keep_alive_handler.cancel()
        self._keep_alive_handler = None

    def reset_server(self):
        self._request_handlers = {}
        self.set_keep_alive_handler()
        self.data = b""
        self._crlf_mark = None
        self.initial = None
        self._request_handler = None
        self._body_parsed = False
        self.method = ""
        self.content_type = ""
        self.content_length = 0
        self.parsed_body = None

    def connection_made(self, transport):
        self.transport = transport
        context = self.transport.get_extra_info("sslcontext", None)
        if context and ssl.HAS_ALPN:  # NPN will not be supported
            alpn_protocol = context.selected_alpn_protocol()
            if alpn_protocol in ["h2", "h2-14", "h2-15", "h2-16", "h2-17"]:
                self.http_version = 20
            else:
                self.transport.close()
                raise Exception("Unsupported Protocol")

    def handle_request_error(self, e):
        if self.http_version == 20:
            return  # HTTP/2 will be implemented later.
        else:
            self.handle_request_error_http_v1(e)

    def handle_request_error_http_v1(self, e):
        pass

    def data_received(self, data):
        self.cancel_keep_alive_handler()
        try:
            if self.http_version == 20:
                return  # HTTP/2 will be implemented later.
            else:
                self.data_received_http_v1(data)
        except Exception as e:
            traceback.print_exc()
            self.handle_request_error(e)

    def data_received_http_v1(self, data):
        if self._body_parsed:
            return

        self.data += data

        if self._crlf_mark is None:
            self._crlf_mark = decide_http_v1_mark(
                self.data[:MAX_HEADER_LENGTH + 1])

            if self._crlf_mark is None:
                return  # Request Not Completed, Wait.

            self.initial, self.data = parse_http_v1_initial(
                self.data, use_crlf_mark=self._crlf_mark)

            self.http_version = self.initial["http_version"]
            self.method = self.initial["parsed_headers"][":method"]
            if self.method in BODY_EXPECTED_METHODS:
                self.content_type = self.initial[
                    "parsed_headers"].get_first("content-type")
                self.content_length = int(
                    self.initial["parsed_headers"].get_first("content-length"))

        if self.method in BODY_EXPECTED_METHODS:
            self.parse_body_http_v1()
            if not self._body_parsed:
                return  # Request Not Completed, wait.
        else:
            self._body_parsed = True

        if len(self._request_handlers.keys()) != 0:
            raise HTTPError(500)
            # HTTP/1.x should have only one RequestHandler at the same time.

        self._request_handlers[0] = asyncio.ensure_future(
            self.handle_request(self.initial, self.parsed_body))

    async def handle_request(self, initial, parsed_body):
        matched_obj = self.app.find_handler(initial["parsed_path"])
        request_handler = matched_obj.pop("__handler__")(
            app=self.app,
            server=self,
            method=initial["parsed_headers"][":method"],
            path=initial["parsed_path"],
            matched_path=matched_obj,
            queries=initial["parsed_queries"],
            http_version=self.http_version,
            request_headers=initial["parsed_headers"],
            request_cookies=initial["parsed_cookies"],
            request_body=parsed_body,
            make_response=self.make_response
        )
        await request_handler.handle(**matched_obj)

    def parse_body_http_v1(self):
        if len(self.data) < self.content_length:
            return  # Request Not Completed, wait.
        self.parsed_body = parse_http_v1_body(
            data=self.data,
            content_type=self.content_type,
            content_length=self.content_length
        )
        self._body_parsed = True

    def make_response(self, status_code, response_headers, response_body):
        if self.http_version == 20:
            pass  # HTTP/2 will be implemented later.
        else:
            self.make_http_v1_response(status_code, response_headers,
                                       response_body)

    def make_http_v1_response(self, status_code, response_headers,
                              response_body):
        response_text = b""
        if self.http_version == 10:
            response_text += b"HTTP/1.0 "
        elif self.http_version == 11:
            response_text += b"HTTP/1.1 "

        response_text += ensure_bytes(str(status_code)) + b" "

        response_text += ensure_bytes(http.client.responses[
            status_code]) + b"\r\n"
        for (key, value) in response_headers.get_all():
            response_text += ensure_bytes("%(key)s: %(value)s\r\n" % {
                "key": key, "value": value})
        response_text += b"\r\n"
        response_text += ensure_bytes(response_body)
        self.transport.write(response_text)
        if self.http_version == 11 and self.app.settings.get(
         "allow_keep_alive", True):
            self.reset_server()
        else:
            self.transport.close()

    def connection_lost(self, reason):
        self.cancel_keep_alive_handler()
        for (key, value) in self._request_handlers.items():
            value.cancel()
