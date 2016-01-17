#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#   Copyright 2015 Futur Solo
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

from futurefinity.utils import *

import futurefinity.web

import nose2
import asyncio
import requests
import unittest
import functools


class SessionTestCollector(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.get_event_loop()
        self.app = futurefinity.web.Application(debug=True)

    def test_keep_alive_request(self):
        @self.app.add_handler("/test_keep_alive")
        class TestHandler(futurefinity.web.RequestHandler):
            async def get(self, *args, **kwargs):
                return "Hello, World!"

        server = self.app.listen(8888)

        async def get_requests_result(self):
            try:
                self.requests_result = []
                with requests.Session() as s:
                    result_getter = functools.partial(
                        s.get, "http://127.0.0.1:8888/test_keep_alive"
                    )
                    self.requests_result.append(
                        await self.loop.run_in_executor(None, result_getter)
                    )
                    self.requests_result.append(
                        await self.loop.run_in_executor(None, result_getter)
                    )
            except:
                traceback.print_exc()
            finally:
                server.close()
                await server.wait_closed()
                self.loop.stop()

        asyncio.ensure_future(get_requests_result(self))
        self.loop.run_forever()

        self.assertEqual(self.requests_result[0].status_code, 200,
                         "Wrong Status Code for First Request.")

        self.assertEqual(self.requests_result[1].status_code, 200,
                         "Wrong Status Code for Second Request.")