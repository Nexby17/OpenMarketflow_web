"""F-131c: make the vendored Finam SDK authenticate the way api.finam.ru now requires.

Symptom it fixes
----------------
DP/journal logs:
    POST https://api.finam.ru/v1/sessions  HTTP/2 400 Bad Request
    [finam_compat] ERROR: F-046 JWT refresh failed (keeping old token): code=3
    [finam_compat] ERROR: call_function error: StatusCode.UNAUTHENTICATED "Invalid access key"
    [main] INFO: PlaceOrder response: None
Robot log:
    orders ERROR: Market order error (attempt 1/2): no response
    robot_of_br WARNING DESYNC: robot=1 broker=0 ... manual fix needed

Root cause
----------
finam-trade-api 4.3.3 sends the API secret twice: in the JSON body ({"secret": ...}) and in the
Authorization header. Finam's API now rejects the header variant:
    "Secret must be passed in the request body, not in the Authorization header"
So every token refresh fails, gRPC keeps a dead token, and order placement silently returns None.

What this script does
---------------------
Patches finam_trade_api/access/access_token.py (TokenClient._exec_request) so the /sessions call
goes out with Content-Type only — the secret stays in the payload. Idempotent.

Why a script and not a commit: /finam_trade_api/ is gitignored (pip --target vendor dir), so the
fix must be re-applied after every reinstall of the SDK.

Usage:  python scripts/patch_finam_sdk_auth.py
"""

import io
import os
import sys

MARKER = "F-131c"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TARGET = os.path.join(REPO, "finam_trade_api", "access", "access_token.py")

OLD = '        async with httpx.AsyncClient(headers=self._auth_headers, http2=True) as client:'
NEW = (
    '        # F-131c: token endpoint must be called WITHOUT Authorization — secret is in the body\n'
    '        async with httpx.AsyncClient(headers={"Content-Type": "application/json"}, http2=True) as client:'
)


def main() -> int:
    if not os.path.exists(TARGET):
        print("MISSING: %s" % TARGET)
        print('Install the SDK first:  pip install --no-deps --target "%s" finam-trade-api==4.3.3' % REPO)
        return 2

    with io.open(TARGET, encoding="utf-8") as f:
        src = f.read()

    if MARKER in src:
        print("already patched: %s" % TARGET)
        return 0

    if OLD not in src:
        print("PATTERN NOT FOUND in %s — SDK layout changed, inspect manually." % TARGET)
        return 3

    patched = src.replace(OLD, NEW, 1)
    with io.open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write(patched)
    print("patched: %s" % TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
