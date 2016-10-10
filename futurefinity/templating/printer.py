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

from .utils import CodeGenerationError
from futurefinity.utils import TYPE_CHECKING, Text

from typing import Optional, Union

import types
import typing

if TYPE_CHECKING:
    from . import template
    from . import statement

_INDENT_MARK = "    "
_END_OF_LINE = "\n"


class CodePrinter:
    """
    Print Python code dealing with indent gracefully.
    """
    def __init__(self, tpl: "template.Template"):

        self._indent_num = 0
        self._tpl = tpl

        self._committed_code = ""

        self._finished = False

    def raise_code_gen_error(self, message: Text,
                             from_err: Optional[Exception]=None):
        """
        Raise a `CodeGenerationError` with error location.
        """
        err_str = "{} in file {}.".format(
            message, self._tpl._template_path)

        if from_err:
            raise CodeGenerationError(err_str) from from_err
        else:
            raise CodeGenerationError(err_str)

    def code_indent(self) -> "CodePrinter":
        """
        Indent the code with `with` statement.

        Example:
        ..code-block:: python3

            printer.print_line("def a():")
            with printer.code_indent():
                printer.print_line("return \"Text from function a.\"")

            printer.print_line("a()")
        """
        if self._finished:
            raise CodeGenerationError(
                "Code Generation has already been finished.")

        return self

    def __enter__(self):
        self._inc_indent_num()

    def __exit__(self, *exc):
        self._dec_indent_num()

    def _inc_indent_num(self):
        """
        Increase the indent.
        """
        if self._finished:
            raise CodeGenerationError(
                "Code Generation has already been finished.")

        self._indent_num += 1

    def _dec_indent_num(self):
        """
        Decrease the indent.
        """
        if self._finished:
            raise CodeGenerationError(
                "Code Generation has already been finished.")

        self._indent_num -= 1

    def write_line(self, line_str: Text, smt_at: Union[Text, int]="<unknown>"):
        """
        Write a line with indent.
        """
        if self._finished:
            raise CodeGenerationError(
                "Code Generation has already been finished.")

        self._committed_code += _INDENT_MARK * self._indent_num

        self._committed_code += line_str

        self._committed_code += "  # In file {} at line {}.".format(
            self._tpl._template_path, smt_at)

        self._committed_code += _END_OF_LINE

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def plain_code(self) -> Text:
        """
        Return the plain, printed code.
        """
        self._finished = True
        return self._committed_code

    @property
    def compiled_code(self) -> types.CodeType:
        """
        Return the compiled code.
        """
        if not hasattr(self, "_compiled_code"):
            self._compiled_code = compile(
                self.plain_code,
                "<Template: {}>".format(self._tpl._template_name), "exec",
                dont_inherit=True)

        return self._compiled_code

    def from_root(self, root: "statement.RootStatement"):
        """
        Print the code from the root statement.
        """
        try:
            root.print_code(self)
        except Exception as e:
            self.raise_code_gen_error("Error During Printing Code", from_err=e)
