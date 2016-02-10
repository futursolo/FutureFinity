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

from futurefinity.utils import ensure_str
from futurefinity.template import render_template

import futurefinity.web

import asyncio

import nose2
import jinja2
import requests
import unittest
import functools


class TemplateInterfaceTestCollector(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.get_event_loop()
        self.app = futurefinity.web.Application(
            allow_keep_alive=False, debug=True,
            template_path="examples/template"
        )

    def test_jinja2_template_request(self):
        @self.app.add_handler("/template_test")
        class TestHandler(futurefinity.web.RequestHandler):
            @render_template("main.htm")
            async def get(self, *args, **kwargs):
                return {"name": "John Smith"}

        server = self.app.listen(8888)

        async def get_requests_result(self):
            try:
                self.requests_result = await self.loop.run_in_executor(
                    None, functools.partial(
                        requests.get, "http://127.0.0.1:8888/template_test"
                    )
                )
            except:
                traceback.print_exc()
            finally:
                server.close()
                await server.wait_closed()
                self.loop.stop()

        asyncio.ensure_future(get_requests_result(self))
        self.loop.run_forever()

        jinja2_envir = jinja2.Environment(loader=jinja2.FileSystemLoader(
            "examples/template",
            encoding="utf-8"
        ))

        template = jinja2_envir.get_template("main.htm")

        self.assertEqual(self.requests_result.status_code, 200,
                         "Wrong Status Code")
        self.assertEqual(ensure_str(self.requests_result.text),
                         ensure_str(template.render(name="John Smith")))