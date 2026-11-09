# -*- coding: utf-8 -*-
"""Fork patch: token check on revit_mcp routes (revit_mcp/auth.py).

pyrevit is not importable outside Revit, so a minimal pyrevit.routes stub is
installed. Dispatch mirrors pyRevit 6.5.5 routes/server: handler arguments are
read with inspect (doc/uidoc/uiapp => Revit API context), only named
arguments are passed, query parameters come from parse_qs.
"""
import importlib
import inspect
import os
import re
import sys
import types
from urllib.parse import parse_qs

import pytest


class _Response(object):
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data


class _Request(object):
    def __init__(self, data=None, params=None, query_params=None):
        self.data = data
        self.params = params or []
        self.query_params = query_params or {}
        self.headers = {}  # pyRevit 6.5.5 never fills this


@pytest.fixture
def auth(tmp_path, monkeypatch):
    registry = {}

    class API(object):
        def __init__(self, name):
            self.name = name

        def route(self, pattern, methods=["GET"]):
            def wrapper(f):
                for m in methods:
                    registry[(pattern, m)] = f
                return f
            return wrapper

    routes = types.ModuleType("pyrevit.routes")
    routes.API = API
    routes.make_response = lambda data, status=200, headers=None: _Response(status, data)
    pyrevit = types.ModuleType("pyrevit")
    pyrevit.routes = routes
    monkeypatch.setitem(sys.modules, "pyrevit", pyrevit)
    monkeypatch.setitem(sys.modules, "pyrevit.routes", routes)
    monkeypatch.setenv("REVIT_MCP_TOKEN_FILE", str(tmp_path / "sub" / "token"))

    module = importlib.import_module("revit_mcp.auth")
    module = importlib.reload(module)
    module.init_token()
    module.registry = registry
    module.API = API
    return module


def _args(f):
    return inspect.getfullargspec(f)[0]


def _dispatch(handler, query="", data=None, params=None):
    query_params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(query).items()}
    request = _Request(data=data, params=params, query_params=query_params)
    api_context = any(a in _args(handler) for a in ("uiapp", "uidoc", "doc"))
    kwargs = {"request": request}
    kwargs.update(dict(request.params))
    kwargs.update({k: v for k, v in query_params.items()
                   if k not in ("request", "uiapp", "uidoc", "doc")})
    kwargs.update(uiapp="UIAPP" if api_context else None,
                  uidoc="UIDOC" if api_context else None,
                  doc="DOC" if api_context else None)
    return handler(**{a: kwargs.get(a) for a in _args(handler)}), request


def _register(auth, calls):
    api = auth.GuardedAPI(auth.API("revit_mcp"))

    @api.route("/status/", methods=["GET"])
    def revit_status():
        """status doc"""
        calls.append(("status",))
        return {"status": "active"}

    @api.route("/get_view/<view_name>", methods=["GET"])
    def get_view(doc, view_name):
        calls.append(("get_view", doc, view_name))
        return {"ok": True}

    @api.route("/execute_code/", methods=["POST"])
    def execute_code(doc, uidoc, request):
        calls.append(("execute_code", doc, uidoc, request.data))
        return {"ok": True}

    return {"revit_status": revit_status, "get_view": get_view, "execute_code": execute_code}


def test_token_file_created_and_reused(auth):
    path = os.environ["REVIT_MCP_TOKEN_FILE"]
    token = open(path).read()
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    auth.init_token()
    assert open(path).read() == token


def test_wrappers_keep_pyrevit_signature(auth):
    originals = _register(auth, [])
    for (_, _), guarded in auth.registry.items():
        original = originals[guarded.__name__]
        assert set(_args(guarded)) == set(_args(original)) | {"request"}
        assert guarded.__doc__ == original.__doc__


@pytest.mark.parametrize("query", ["", "token=" + "0" * 64, "token=abc"])
def test_rejects_without_valid_token(auth, query):
    calls = []
    _register(auth, calls)
    for handler in auth.registry.values():
        response, _ = _dispatch(handler, query=query, data={"code": "x"})
        assert response.status == 401
    assert calls == []


def test_rejects_duplicate_token_and_token_in_body(auth):
    calls = []
    _register(auth, calls)
    token = open(os.environ["REVIT_MCP_TOKEN_FILE"]).read()
    handler = auth.registry[("/execute_code/", "POST")]
    assert _dispatch(handler, query="token=%s&token=%s" % (token, token))[0].status == 401
    assert _dispatch(handler, data={"code": "x", "token": token})[0].status == 401
    assert calls == []


def test_accepts_valid_token(auth):
    calls = []
    _register(auth, calls)
    token = open(os.environ["REVIT_MCP_TOKEN_FILE"]).read()
    q = "token=" + token
    assert _dispatch(auth.registry[("/status/", "GET")], query=q)[0] == {"status": "active"}
    _dispatch(auth.registry[("/get_view/<view_name>", "GET")], query=q,
              params=[("view_name", "Plan%201")])
    _dispatch(auth.registry[("/execute_code/", "POST")], query=q, data={"code": "print 1"})
    assert calls == [("status",), ("get_view", "DOC", "Plan%201"),
                     ("execute_code", "DOC", "UIDOC", {"code": "print 1"})]


def test_callback_url_removed_and_rejected(auth):
    calls = []
    _register(auth, calls)
    token = open(os.environ["REVIT_MCP_TOKEN_FILE"]).read()
    handler = auth.registry[("/execute_code/", "POST")]
    response, request = _dispatch(handler, query="token=" + token,
                                  data={"code": "x", "callbackUrl": "http://example.com/"})
    assert response.status == 400
    assert "callbackUrl" not in request.data
    assert calls == []


def test_fails_closed_on_bad_token_file(auth, tmp_path, monkeypatch):
    bad = tmp_path / "short"
    bad.write_text("abc")
    monkeypatch.setenv("REVIT_MCP_TOKEN_FILE", str(bad))
    auth.init_token()
    calls = []
    _register(auth, calls)
    response, _ = _dispatch(auth.registry[("/status/", "GET")], query="token=abc")
    assert response.status == 503
    assert calls == []
