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

from . import compat
from typing import Optional, Callable, Union, Any

import io
import asyncio
import functools
import threading
import collections.abc
import concurrent.futures

__all__ = ["get_default_executor", "run_on_executor",
           "AsyncFilePointer", "AsyncBytesIO", "AsyncStringIO",
           "AsyncFileSystemOperations", "aopen"]

_THREAD_LOCALS = threading.local()


def get_default_executor():
    if not hasattr(_THREAD_LOCALS, "default_executor"):
        setattr(_THREAD_LOCALS, "default_executor",
                concurrent.futures.ThreadPoolExecutor())

    return _THREAD_LOCALS.default_executor


def run_on_executor(*args, **kwargs):
    def decorator(
        func: Callable[[Any], Any], *,
            executor: Optional[concurrent.futures.Executor]=None,
            loop: Optional[asyncio.AbstractEventLoop]=None) -> Callable[
                [Any], compat.Awaitable[Any]]:
        if asyncio.iscoroutine(func) or asyncio.iscoroutinefunction(func):
            raise TypeError(
                "coroutines cannot be used with run_on_executor().")

        executor = executor or get_default_executor()

        loop = loop or asyncio.get_event_loop()

        async def wrapper(*args, **kwargs) -> Any:
            fur = loop.run_in_executor(
                executor, functools.partial(func, *args, **kwargs))

            return await fur

        return wrapper

    if len(args) == 1 and len(kwargs) == 0:
        return decorator(args[0])

    elif len(args) > 0:
        raise TypeError(
            "run_on_executor can accept 1 positional arugment "
            "with no keyowrd arguments or only keyword arguments.")

    else:
        return functools.partial(decorator, **kwargs)


class AsyncFilePointer(collections.abc.Awaitable):
    def __init__(self, *args, **kwargs):
        self._async_fs_ops = kwargs.pop("_async_fs_ops")
        self._fp = None
        self._args = args
        self._kwargs = kwargs

        self._mutex_lock = asyncio.Lock(loop=self._async_fs_ops._loop)

    async def _open_fp(self):
        @run_on_executor(
            executor=self._async_fs_ops._executor,
            loop=self._async_fs_ops._loop)
        def open_fp_in_executor():
            self._fp = open(*self._args, **self._kwargs)

        async with self._mutex_lock:
            if self._fp is not None:
                raise RuntimeError(
                    "This file pointer has already been initialized.")

            await open_fp_in_executor()

    async def __await__(self) -> "AsyncFilePointer":
        await self._open_fp()
        return self

    async def __aenter__(self) -> "AsyncFilePointer":
        await self._open_fp()
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()  # Generated by self.__getattr__.

    def __getattr__(
        self, name: compat.Text) -> Callable[
            [Any], Union[Any, compat.Awaitable[Any]]]:
        if self._fp is None:
                raise RuntimeError(
                    "This file pointer has not been initialized yet. "
                    "Please initialize it first.")

        attr = getattr(self._fp, name)

        if not callable(attr):
            return attr

        @run_on_executor(
            executor=self._async_fs_ops._executor,
            loop=self._async_fs_ops._loop)
        def run_fn_in_executor(fn, *args, **kwargs) -> Any:
            return fn(*args, **kwargs)

        return functools.partial(run_fn_in_executor, attr)


class _AsyncIOBase(AsyncFilePointer):
    def __init__(self, *args, **kwargs):
        assert hasattr(self, "_fp")
        assert isinstance(self._fp, io.IOBase)

    async def __await__(self):
        return self

    async def __aenter__(self) -> "AsyncFilePointer":
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()

    def __getattr__(
        self, name: compat.Text) -> Callable[
            [Any], Union[Any, compat.Awaitable[Any]]]:
        attr = getattr(self._fp, name)

        if not callable(attr):
            return attr

        async def run_fn(fn, *args, **kwargs) -> Any:
            return fn(*args, **kwargs)

        return functools.partial(run_fn, attr)


class AsyncBytesIO(_AsyncIOBase):
    def __init__(self, *args, **kwargs):
        self._fp = io.BytesIO(*args, **kwargs)

        super().__init__(*args, **kwargs)


class AsyncStringIO(_AsyncIOBase):
    def __init__(self, *args, **kwargs):
        self._fp = io.StringIO(*args, **kwargs)

        super().__init__(*args, **kwargs)


class AsyncFileSystemOperations:
    def __init__(
        self, *, executor: Optional[
            concurrent.futures.ThreadPoolExecutor]=None,
            loop: Optional[asyncio.AbstractEventLoop]=None):
        self._loop = loop or asyncio.get_event_loop()
        self._executor = executor or get_default_executor()

    def aopen(self, *args, **kwargs) -> AsyncFilePointer:
        return AsyncFilePointer(*args, **kwargs, _async_fs_ops=self)


def aopen(*args, **kwargs) -> AsyncFilePointer:
    if not hasattr(_THREAD_LOCALS, "default_async_fs_ops"):
        _THREAD_LOCALS.default_async_fs_ops = AsyncFileSystemOperations()

    return _THREAD_LOCALS.default_async_fs_ops.aopen(*args, **kwargs)
