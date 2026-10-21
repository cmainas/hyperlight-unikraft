# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Hyperlight Authors.

# Runs in the guest's CPython once, when the function is created, before
# its snapshot is taken: what it defines and imports is in every guest
# restored from it.  Each block is then a guest function call,
# _clickhouse_block(), in a guest restored afresh for it.
#
# A block's rows and results cross as JSON text through host function
# calls, in pieces, as a guest function call's input and result are bounded
# by the transport (some 64 KiB) and a block is not.
#
# The host checks each result against the return type, so the guest only
# converts what JSON cannot carry as it is: the floats that are not finite,
# both ways, and numpy's values, which json.dumps does not know.  Values
# that need neither cost the block nothing but the function's own call.

import ast
import builtins
import json
import keyword
import linecache
import math
import random
import reprlib
import sys
import traceback
import unicodedata

import hyperlight


# Bytes of a piece of results once quoted again as the host function's argument, within the 69,620 its name and
# arguments may take (docs/calls.md, "Limits")
_PIECE_BYTES = 65000

_FLOATS = ("Float32", "Float64")

# The most an error's JSON may take, within the 69,620 bytes a host function call's name and arguments may (see
# docs/calls.md, "Limits")
_ERROR_BYTES = 60000


def _decoder(kind):
    """What makes an argument of `kind` from its JSON, or None where the JSON is the argument."""
    if isinstance(kind, list):
        wrapper, inner = kind
        decode = _decoder(inner)
        if decode is None:
            return None
        if wrapper == "Array":
            return lambda value: [decode(item) for item in value]
        return lambda value: None if value is None else decode(value)
    if kind in _FLOATS:
        # "nan", "inf" and "-inf", which JSON has no number for
        return lambda value: float(value) if value.__class__ is str else value
    return None


def _encoder(kind):
    """What makes the JSON of a result of `kind`, or None where json.dumps can take it as it is."""
    if isinstance(kind, list):
        wrapper, inner = kind
        encode = _encoder(inner)
        if wrapper == "Array":
            # Any sequence, whatever the items' type: a tuple, a range or numpy's array as a list, but not a dict's
            # keys or a set's elements

            def encode_array(value):
                if isinstance(value, (str, bytes, dict, set, frozenset)):
                    raise TypeError(f"a {type(value).__name__} is not a list")
                tolist = getattr(value, "tolist", None)
                items = tolist() if tolist is not None else value
                # Always a copy: the function may change its list again on a later row
                if encode is None:
                    return list(items)
                return [encode(item) for item in items]

            return encode_array
        if encode is None:
            return None
        return lambda value: None if value is None else encode(value)
    if kind in _FLOATS:

        def encode(value):
            if value is None:
                # Left for the host to refuse, with the row it is on
                return None
            if isinstance(value, (str, bytes)):
                # Which float() would read, where every other type takes only its own values
                raise TypeError(f"a {type(value).__name__} is not a number")
            value = float(value)
            if math.isfinite(value):
                return value
            return "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"

        return encode
    return None


def _plain(value):
    """json.dumps' fallback: numpy's scalars and arrays as Python's own values."""
    tolist = getattr(value, "tolist", None)
    if tolist is None:
        raise TypeError(f"a result of type {type(value).__name__}, which has no JSON form")
    return tolist()


def _row_decoder(kinds):
    decoders = [_decoder(kind) for kind in kinds]
    if not any(decoders):
        return None
    pairs = [(i, decode) for i, decode in enumerate(decoders) if decode is not None]

    def decode_row(row):
        for i, decode in pairs:
            row[i] = decode(row[i])
        return row

    return decode_row


def _parse_body(body, filename):
    """The body's statements: as written, or as a block where all its lines are indented, as they can be in
    `AS $$ ... $$`. Line numbers are the body's own."""
    try:
        return ast.parse(body, filename).body
    except IndentationError as error:
        try:
            tree = ast.parse("if 1:\n" + body, filename)
        except SyntaxError as block_error:
            # Which error says what is wrong: for a body whose first line is indented, the block's, at the body's
            # own line
            if body.lstrip("\n")[:1].isspace():
                if block_error.lineno:
                    block_error.lineno -= 1
                raise block_error from None
            raise error from None
        if len(tree.body) != 1 or tree.body[0].orelse:
            # Not one indented block either, where a dedented line would end it, or would be taken for the
            # wrapper's own else or elif and dropped
            raise error from None
        ast.increment_lineno(tree, -1)
        return tree.body[0].body


def _warm_imports(body):
    """Import what the body imports, so that the modules are in the snapshot and a block's imports find them
    loaded. One that fails is left for the function to raise, where it runs."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not getattr(node, "level", 0):
            try:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<import>", "exec"), {})
            except BaseException:
                # A module that exits when imported, say
                pass


def _input():
    """What the host holds for the guest, a block's rows or the function's description, as JSON text."""
    pieces = []
    while piece := hyperlight.call("clickhouse.input"):
        pieces.append(piece)
    return "".join(pieces)


def _clickhouse_define():
    """Compile the function's body into _function, and what its blocks need."""
    global _name, _returns, _function, _decode_row, _encode
    spec = json.loads(_input())
    _name, _returns = spec["name"], spec["returns"]
    # As Python reads the names in the body: NFKC-normalized (a ligature such as "ﬁ" is "fi")
    _name = unicodedata.normalize("NFKC", _name)
    arguments = [unicodedata.normalize("NFKC", argument) for argument, _ in spec["args"]]
    for argument in arguments:
        if not argument.isidentifier() or keyword.iskeyword(argument):
            raise SystemExit(f"argument {argument!r} is not a name Python can use: rename it")
    # Its own name in tracebacks, where Python can call it that
    # (not a dunder name, which the function's module has some of: __builtins__, __name__)
    usable = _name.isidentifier() and not keyword.iskeyword(_name) and not _name.startswith("__")
    function = _name if usable else "function"
    filename = f"<function {_name}>"
    # So that tracebacks show the body's lines, as for a file
    linecache.cache[filename] = (len(spec["body"]), None, spec["body"].splitlines(True), filename)
    try:
        body = _parse_body(spec["body"], filename)
        # The body as written, in a function of the arguments: built as a tree rather than as text, so that
        # nothing indents the lines of its strings
        tree = ast.Module(
            body=[
                ast.FunctionDef(
                    name=function,
                    args=ast.arguments(
                        posonlyargs=[],
                        args=[ast.arg(arg=argument) for argument in arguments],
                        kwonlyargs=[],
                        kw_defaults=[],
                        defaults=[],
                    ),
                    body=body or [ast.Pass()],
                    decorator_list=[],
                    type_params=[],
                )
            ],
            type_ignores=[],
        )
        code = compile(ast.fix_missing_locations(tree), filename, "exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        # Python's error alone, the frames above being this script's: a SyntaxError, or a body Python cannot compile
        # (a NUL character, or nesting deeper than it takes)
        raise SystemExit("".join(traceback.format_exception_only(e)).rstrip()) from None
    # A module of its own, so that its names do not mix with these. A function named after a builtin (len, repr) is
    # taken out of it, so that it still finds the builtin; any other keeps its name, to call itself by.
    scope = {"__name__": function}
    exec(code, scope)
    _function = scope.pop(function) if hasattr(builtins, function) else scope[function]
    _warm_imports(body)
    _decode_row = _row_decoder([kind for _, kind in spec["args"]])
    _encode = _encoder(spec["return"])


def _fail(error, what):
    # Handed to the host, rather than left to fail the call: a traceback comes at the end of the guest's output,
    # which the host keeps the beginning of. What failed last, as ClickHouse shows the end.
    message = f"{error[-8000:]}{what}"
    # Within what a host function call carries, quoted as JSON: \u escapes make a character up to 12 bytes
    while len(json.dumps(message)) > _ERROR_BYTES:
        message = message[len(message) // 2 :]
    hyperlight.call("clickhouse.error", message)


# What an error shows of a row: no copy of a long value, which the guest may not have the memory for
_SHOWN = reprlib.Repr(maxlevel=3, maxlist=10, maxstring=80, maxother=80)


def _row(rows, index):
    # Cut, not shortened: textwrap.shorten would collapse the whitespace of the very values that failed
    shown = _SHOWN.repr(rows[index])
    return f"row {index + 1} of the block, {shown if len(shown) <= 200 else shown[:199] + '…'}"


def _reseed():
    """Seed again the generators whose state the snapshot holds, from the kernel's, which each restore reseeds: else
    every block would draw the numbers the first did."""
    random.seed()
    numpy_random = sys.modules.get("numpy.random")
    if numpy_random is not None:
        numpy_random.seed()


def _clickhouse_block():
    """Run the function on the block the host holds, and hand it the results."""
    try:
        _reseed()
        _run_block()
    except BaseException as e:
        # sys.exit in the function, or results the host refused: told as the function's, not left to fail the call
        _fail("".join(traceback.format_exception_only(e)), f"{_name} did not finish its block")


def _run_block():
    rows = json.loads(_input())
    function, decode_row = _function, _decode_row
    encode = _encode
    results = []
    append = results.append
    for index, row in enumerate(rows):
        try:
            value = function(*(row if decode_row is None else decode_row(row)))
        # BaseException: sys.exit on a row is told with the row, as any other failure is
        except BaseException as e:
            # From the function's own frame down
            error = "".join(traceback.format_exception(type(e), e, e.__traceback__.tb_next))
            return _fail(error, f"{_name} failed on {_row(rows, index)}")
        if encode is not None:
            # As the row ends, not the block: a list the function returns, then changes on a later row, is copied
            # as it was
            try:
                value = encode(value)
            except Exception as e:
                error = "".join(traceback.format_exception_only(e))
                return _fail(error, f"{_name} returned {_SHOWN.repr(value)} for {_returns} on {_row(rows, index)}")
        append(value)
    try:
        text = json.dumps(results, allow_nan=False, default=_plain)
    except Exception as e:
        # Which row's, json.dumps does not say
        return _fail("".join(traceback.format_exception_only(e)), f"{_name} returned a value JSON cannot carry")
    del results, rows
    start = 0
    while start < len(text):
        # ASCII-only JSON, so quoting it again doubles only quotes and backslashes
        piece = text[start : start + _PIECE_BYTES]
        while (cost := len(piece) + piece.count('"') + piece.count("\\")) > _PIECE_BYTES:
            # Cut to about what fits, in proportion, rather than in half
            piece = piece[: max(1, len(piece) * _PIECE_BYTES // cost - 1)]
        hyperlight.call("clickhouse.output", piece)
        start += len(piece)
