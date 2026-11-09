# -*- coding: UTF-8 -*-
"""
Token check for revit_mcp routes (fork patch, not part of upstream; see FORK.md).

Any process that can reach the Routes port can run arbitrary code in Revit.
On Docker Desktop that includes every container with a normal network,
because host.docker.internal is forwarded to the host's 127.0.0.1.

pyRevit Routes does not pass HTTP headers to handlers (base.Request.headers
stays empty), so the token travels in the query string: ?token=<value>.
Without the right token the handler is not called and Revit answers 401.

Token file: %APPDATA%\\revit-mcp\\token (override: REVIT_MCP_TOKEN_FILE).
Created on first load if missing. main.py reads the same file.
"""

import os
import binascii
import inspect
import logging

from pyrevit import routes

logger = logging.getLogger(__name__)

TOKEN_PARAM = "token"
TOKEN_BYTES = 32

try:
    _STRING_TYPES = (basestring,)  # noqa: F821  (IronPython 2.7)
except NameError:
    _STRING_TYPES = (str,)

_state = {"token": None, "error": "not initialized"}


def token_path():
    """Path of the shared token file."""
    override = os.environ.get("REVIT_MCP_TOKEN_FILE")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "revit-mcp", "token")


def _load_or_create_token(path):
    if os.path.isfile(path):
        with open(path, "r") as f:
            value = f.read().strip()
        if len(value) < 32:
            raise ValueError("token file is too short: " + path)
        return value
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    value = binascii.hexlify(os.urandom(TOKEN_BYTES))
    if not isinstance(value, _STRING_TYPES):
        value = value.decode("ascii")
    with open(path, "w") as f:
        f.write(value)
    logger.info("Revit MCP token created: %s", path)
    return value


def init_token():
    """Load the token once at extension startup. Fails closed on error."""
    path = token_path()
    try:
        _state["token"] = _load_or_create_token(path)
        _state["error"] = None
    except Exception as ex:
        _state["token"] = None
        _state["error"] = "{}: {}".format(path, ex)
        logger.error("Revit MCP token unavailable, all requests will be rejected: %s",
                     _state["error"])


def _same(a, b):
    """Constant-time string comparison."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


def check_request(request):
    """Return None if the request carries the right token, else a Response."""
    # pyRevit posts the handler result to data["callbackUrl"] if present.
    # Drop it before answering so no request can make Revit call out
    # (the dispatcher re-reads it after the handler returns).
    data = getattr(request, "data", None)
    if isinstance(data, dict) and "callbackUrl" in data:
        data.pop("callbackUrl", None)
        return routes.make_response(
            data={"status": "error", "error": "callbackUrl is not supported"},
            status=400)

    expected = _state["token"]
    if not expected:
        return routes.make_response(
            data={"status": "error",
                  "error": "Revit MCP token is not configured",
                  "details": _state["error"]},
            status=503)

    query = getattr(request, "query_params", None) or {}
    supplied = query.get(TOKEN_PARAM) if hasattr(query, "get") else None
    if isinstance(supplied, _STRING_TYPES) and _same(supplied, expected):
        return None

    return routes.make_response(
        data={"status": "error",
              "error": "Unauthorized: missing or wrong token"},
        status=401)


def _arg_names(func):
    spec = getattr(inspect, "getfullargspec", None) or inspect.getargspec
    return list(spec(func)[0])


def guard(handler):
    """Wrap a route handler with the token check.

    pyRevit inspects handler argument names: doc/uidoc/uiapp decide whether
    the handler runs in Revit API context, and only named arguments are
    passed. So the wrapper is generated with the same argument names plus
    'request'.
    """
    names = _arg_names(handler)
    for name in names:
        if not (name.replace("_", "a").isalnum() and not name[0].isdigit()):
            raise ValueError("unexpected handler argument name: %r" % name)

    signature = list(names)
    if "request" not in signature:
        signature.append("request")

    src = (
        "def guarded_handler({sig}):\n"
        "    denied = _check(request)\n"
        "    if denied is not None:\n"
        "        return denied\n"
        "    return _handler({call})\n"
    ).format(
        sig=", ".join(signature),
        call=", ".join(["{0}={0}".format(n) for n in names]),
    )
    namespace = {"_check": check_request, "_handler": handler}
    exec(src, namespace)
    guarded = namespace["guarded_handler"]
    guarded.__name__ = handler.__name__
    guarded.__doc__ = handler.__doc__
    return guarded


class GuardedAPI(object):
    """routes.API wrapper: every registered route requires the token."""

    def __init__(self, api):
        self._api = api
        self.name = api.name

    def route(self, pattern, methods=["GET"]):  # noqa: B006 (same as pyRevit)
        register = self._api.route(pattern, methods=methods)

        def decorator(func):
            register(guard(func))
            return func

        return decorator
