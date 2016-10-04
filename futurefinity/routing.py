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
``futurefinity.routing`` contains the url-based routing system used by
``futurefinity.web``.

"""

from .utils import deprecated_attr, FutureFinityError, Text, TYPE_CHECKING

from typing.re import Pattern
from typing import Optional, Tuple, List, Any, Dict, Union

import re
import warnings
import collections

if TYPE_CHECKING:
    from futurefinity import web


class RoutingError(FutureFinityError):
    pass


class NotMatched(RoutingError):
    pass


class NoMatchesFound(NotMatched):
    pass


class ReverseError(NotMatched):
    pass


class _ReMatchGroup:
    _named_group_re = re.compile(r"\?P<(.*)>.*")

    def __init__(self, group_str: Text, index: int):
        self._group_str = group_str
        self._index = index

    @property
    def _name(self) -> Text:
        if not hasattr(self, "_prepared_name"):
            matched = self._named_group_re.fullmatch(self._group_str)

            if matched:
                self._prepared_name = matched.groups()[0]
            else:
                raise ReverseError("Cannot reverse positional group by name.")

        return self._prepared_name


class Rule:
    """
    Rule for Routing, which contains a Handler, path_args(Deprecated),
    and a path_kwargs. It can be either a stored rule in a routing locator,
    or a matched rule that will be returned to the application.

    :arg Handler: should be a subclass of ``futurefinity.web.RequestHandler``.
    :arg path_args: is a tuple or list that contains the positional arguments.
        This argument is Deprecated, use keyword arugments instead.
    :arg path_kwargs: is a dict that contains the keyword arguments.
    """

    def __init__(
        self, path: Union[Text, "Pattern[Text]"],
        Handler: "web.RequestHandler", path_args: Tuple[Any]=(),
        name: Optional[Text]=None, path_kwargs: Dict[Text, Any]={}):
        self.path = path

        if isinstance(self.path, str):
            self.path = re.compile(path)

        self.Handler = Handler

        self.name = name

        self.path_args = list(path_args)
        self.path_kwargs = path_kwargs

        if len(self.path_args) != 0:
            warnings.warn(
                    "Arguments without a name are deprecated, "
                    "use keyword arguments instead.", DeprecationWarning)

    def match(
            self, path: Text) -> (
                "web.RequestHandler", Tuple[Any], Dict[Text, Any]):
        matched_obj = self.path.fullmatch(path)

        if not matched_obj:
            raise NotMatched("The path does not match the rule.")

        path_args = []
        path_args.extend(self.path_args)
        matched_args = matched_obj.groups() or []
        path_args.extend(matched_args)

        path_kwargs = {}
        matched_kwargs = matched_obj.groupdict() or {}
        path_kwargs.update(matched_kwargs)
        path_kwargs.update(self.path_kwargs)

        return self.Handler, path_args, path_kwargs

    @property
    def _match_groups(self) -> List[Union[Text, _ReMatchGroup]]:
        if not hasattr(self, "_prepared_match_groups"):
            groups = []
            rest_pattern_str = self.path.pattern

            index_count = 0

            inside_group = False

            while True:
                begin_pos = rest_pattern_str.find("(")

                if begin_pos == -1:
                    groups.append(rest_pattern_str)
                    rest_pattern_str = ""
                    break
                groups.append(rest_pattern_str[:begin_pos])

                rest_pattern_str = rest_pattern_str[begin_pos + 1:]

                end_pos = rest_pattern_str.find(")")
                groups.append(
                    _ReMatchGroup(rest_pattern_str[:end_pos], index_count))

                index_count += 1

                rest_pattern_str = rest_pattern_str[end_pos + 1:]

            self._prepared_match_groups = groups

        return self._prepared_match_groups

    def reverse(self, *args, **kwargs) -> Text:
        result = ""

        if len(args) != 0 and len(kwargs) != 0:
            raise ReverseError(
                "Cannot Reverse the path using positional and "
                "keyword arguments at the same time.")

        use_kwargs = False
        if len(kwargs) != 0:
            use_kwargs = True

        for group in self._match_groups:
            if isinstance(group, str):
                result += group
                continue

            if use_kwargs:
                result += kwargs[group._name]
                continue

            result += args[group._index]

        return result


class Dispatcher:
    """
    A Routing Dispatcher.

    :arg DefaultHandler: should be a subclass of
        ``futurefinity.web.RequestHandler`` , which will be returned if a
        handler cannot be found during the matching.
    """
    def __init__(self, DefaultHandler: Optional["web.RequestHandler"]=None):
        self._rules = []
        self._name_dict = {}

        self._DefaultHandler = DefaultHandler

    def add(
        self, path: Union[Text, "Pattern[Text]"], *args,
            Handler: Optional["web.RequestHandler"]=None,
            name: Optional[Text]=None, **kwargs):
        """
        Add a `futurefinity.web.RequestHandler` to the `Dispatcher`.
        If you specific a Handler in parameter, it will return nothing.

        On the other hand, if you use it as a decorator, you should not pass
        a handler to this function or it will cause unexcepted results.

        That is::

          @app.handlers.add("/")
          class RootHandler(ReuqestHandler): pass

        :arg path: is a regular expression of the path that will be matched.
        :arg Handler: should be a ``futurefinity.web.RequestHandler`` subclass.
        :arg \*args: all the other positional arguments will be come the
            path_args of the routing rule. The arguments passed here always
            have a higher priority in the matched routing rule, which means
            that all the positional arguments passed here will be the first
            part of the matched object.
        :arg name: the name of the routing rule.
        :arg \*\*kwargs: all the other keyword arguments will be come the
            path_kwargs of the routing rule. The arguments passed here always
            have a higher priority in the matched routing rule, which means
            that if the same key also exsits in the regular expression,
            this one will override the one in the path.
        """
        def wrapper(Handler):
            self.add(path=path, *args, Handler=Handler, name=name, **kwargs)
            return Handler

        if Handler is None:
            return wrapper

        self.add_rules(
            Rule(path=path, Handler=Handler, path_args=args, name=name,
                 path_kwargs=kwargs))

    def add_rules(self, *args):
        for rule in args:
            if rule.name is not None:
                if rule.name in self._name_dict.keys():
                    raise KeyError(
                        "Rule with the name {} already existed."
                        .format(rule.name))

                self._name_dict[rule.name] = rule
            self._rules.append(rule)

    def find(
        self, path: Text) -> Tuple[
            "web.RequestHandler", Tuple[Any], Dict[Text, Any]]:
        """
        Find a `Rule` that matches the path and combine both arguments from
        `Rule` and the provided path together.

        If a `Rule` that matches the path cannot be found, the `DefaultHandler`
        will be returned.

        It returns Tuple[
            futurefinity.web.RequestHandler, Tuple[Any], Dict[Text, Any]].

        For the path_args and path_kwargs, the ones defined in the `Rule`
        will have a higher priority.
        """
        for rule in self._rules:
            try:
                Handler, path_args, path_kwargs = rule.match(path)
            except NotMatched:
                continue

            return Handler, path_args, path_kwargs

        else:
            if self._DefaultHandler is None:
                raise NoMatchesFound(
                    "No rules matched the given path, "
                    "and a DefaultHandler is not set.")

            return self._DefaultHandler, [], {}

    def reverse(
        self, name: Text, path_args: List[Text]=(),
            path_kwargs: Dict[Text, Text]={}) -> Text:
        """
        Reverse a Rule in the dispatcher.

        .. code-block:: python3
            >>> dispatcher = Dispatcher()
            >>> dispatcher.add("/page/(?P<page_id>.*)/", name="page")
            >>> dispatcher.reverse("index", path_kwargs={"page_id": "1"})
            /page/1/

        """
        if name not in self._name_dict.keys():
            raise KeyError("Unknown Name.")

        rule = self._name_dict[name]

        return rule.reverse(*path_args, **path_kwargs)

RoutingLocator = deprecated_attr(
    Dispatcher, __name__,
    "RoutingLocator is deprecated, use `routing.Dispatcher` instead.")
