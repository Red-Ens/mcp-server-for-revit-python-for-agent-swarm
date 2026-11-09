# Fork notes — branch `batraks`

Fork of [mcp-servers-for-revit/mcp-server-for-revit-python](https://github.com/mcp-servers-for-revit/mcp-server-for-revit-python)
for a local agent swarm. Branch `batraks` is based on upstream `51b4829`
(2026-07-26), the last version that works with the released pyRevit 6.5.5.
`master` is kept as an unmodified mirror of upstream.

## Why not upstream `master`

Upstream (from `e109f3d`, 2026-10-14) authenticates with an `Authorization`
header and reads `request.headers` in route handlers. pyRevit Routes 6.5.5 does
not copy HTTP headers into the request object (only pyRevit `develop` does), so
on 6.5.5 every request would be rejected. Upstream pairing also stores the
secret with DPAPI CurrentUser (unreadable from containers or another Windows
account) and shows a confirmation dialog in Revit for code execution, which
blocks unattended use.

## What this branch changes

**Token on Routes** (`revit_mcp/auth.py`, `startup.py`, `main.py`):

- Any process that can reach the Routes port can run arbitrary code in Revit.
  On Docker Desktop that includes every container with a normal network:
  `host.docker.internal` is forwarded to the host's `127.0.0.1`, so binding
  Routes to loopback does not help.
- Every `revit_mcp` route is wrapped: without the right token the handler is
  not called and the answer is `401`. The token travels as a query parameter
  `?token=...` because pyRevit 6.5.5 does not pass headers to handlers.
  A token in the POST body is not accepted.
- If the token file cannot be read or is too short, every request gets `503`
  (fail closed).
- Token file: `%APPDATA%\revit-mcp\token`, override with `REVIT_MCP_TOKEN_FILE`.
  Created by the extension (32 random bytes, hex) when Revit loads it.
  `main.py` reads it on every call. To rotate: delete the file, reload pyRevit.
- Requests with `callbackUrl` are rejected with `400` and the key is removed,
  so no request can make Revit call out.
- pyRevit's own `/routes/status` and `/routes/sisters` stay open: they run no
  code but expose the Windows user name.

The token authenticates the caller; it does not sandbox the code it lets in.
Anyone who can read the token file (any program running as the same Windows
user) can drive Revit.

**Cherry-picked from upstream** (`git cherry-pick -x`, applied cleanly):

| Upstream | Change |
|---|---|
| `9d58f5b` (#20) | `format_response`: recoverable statuses are not framed as errors |
| `07d9e29` (#48) | `REVIT_HOST` / `REVIT_PORT` from environment |
| `7a693c0` (#60) | package-relative utility imports (IronPython 3) |
| `aeda9c7` (#59) | integration tests use `127.0.0.1` |
| `2dac9b0` (#65) | optional tool arguments accept explicit `null` |
| `3a71e45` (#66) | `get_element_name` in `find_family_symbol_safely` |
| `e6f35ff` (#67) | `close_document` answers the save prompt instead of hanging |

`REVIT_HOST` may point to a non-loopback host; the token then travels over
plain HTTP. Keep it on loopback.

## Install

Windows Command Prompt (`cmd`):

```bat
git clone -b batraks https://github.com/Red-Ens/mcp-server-for-revit-python-for-agent-swarm.git "%APPDATA%\pyRevit\Extensions\mcp-server-for-revit-python.extension"
cd "%APPDATA%\pyRevit\Extensions\mcp-server-for-revit-python.extension"
uv sync
```

Then enable the extension in pyRevit, reload pyRevit (creates the token file)
and restart the MCP client so `main.py` is reloaded.

Second Windows account (e.g. a dedicated Revit account for agents): install
the same branch in its profile, set a different Routes port in its pyRevit
settings (both instances otherwise take 48884 and the second fails to start),
and point `REVIT_MCP_TOKEN_FILE` to a file readable only by that account and
by the process that talks to its Revit.

## Updating from upstream

```bash
git fetch upstream
git log --oneline batraks..upstream/master   # what is new
git cherry-pick -x <commit>                  # take what fits
```

## Tests

`tests/unit/test_token_auth.py` mirrors pyRevit 6.5.5 dispatch (argument
inspection, `parse_qs` query parameters). Run `pytest tests/unit`.
