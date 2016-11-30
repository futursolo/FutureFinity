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

from typing import Optional

import sys
import typing
import asyncio
import inspect
import collections.abc

__all__ = [
    "PY350", "PY351", "PY352", "Text", "TYPE_CHECKING", "Awaitable",
    "ensure_future", "create_future"]

PY350 = sys.version_info[:3] >= (3, 5, 0)
PY351 = sys.version_info[:3] >= (3, 5, 1)
PY352 = sys.version_info[:3] >= (3, 5, 2)

Text = getattr(typing, "Text", str)
TYPE_CHECKING = getattr(typing, "TYPE_CHECKING", False)

if hasattr(typing, "Awaitable"):
    Awaitable = typing.Awaitable

else:
    class Awaitable(
            typing.Generic[typing.T_co], extra=collections.abc.Awaitable):
        __slots__ = ()

if PY351:
    ensure_future = asyncio.ensure_future

else:
    def _wrap_awaitable(awaitable: Awaitable[Any]):
        """
        Wrap an awaitable into a coroutine.
        """
        return (yield from awaitable.__await__())

    def ensure_future(
        coro_or_future: Awaitable[Any], *,
            loop: Optional[asyncio.AbstractEventLoop]=None) -> Awaitable[Any]:
        """
        Wrap a coroutine or an awaitable in a future.

        If the argument is a Future, it is returned directly.
        """
        if isinstance(coro_or_future, asyncio.Future) or asyncio.iscoroutine(
                coro_or_future):
            return asyncio.ensure_future(coro_or_future, loop=loop)

        if inspect.isawaitable(coro_or_future):
            return asyncio.ensure_future(
                _wrap_awaitable(coro_or_future), loop=loop)

        else:
            raise TypeError(
                "A Future, a coroutine or an awaitable is required.")


def create_future(
        *, loop: Optional[asyncio.AbstractEventLoop]=None) -> asyncio.Future:
    loop = loop or asyncio.get_event_loop()
    try:
        return loop.create_future()

    except (NotImplementedError, AttributeError):
        return asyncio.Future(loop=loop)
