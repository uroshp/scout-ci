"""Page tokens for Ask Scout (WS2, 2026-09-28): minted by the viewer, verified by the engine, with
one shared secret (ASK_VIEWER_SECRET). A token proves the caller came through a rendered page in
the last hour; it is not identity. Stdlib only, so both images import it."""
from __future__ import annotations

import hashlib
import hmac
import re
import time


def page_token(cid: str, secret: str, ttl_s: int = 3600, now: float | None = None) -> str:
    """v1.<exp>.<cid>.<sig>; cid = the visitor cookie (or a random id)."""
    exp = int((now or time.time()) + ttl_s)
    sig = hmac.new(secret.encode(), f"{exp}.{cid}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"v1.{exp}.{cid}.{sig}"


def check_token(token: str, secret: str, now: float | None = None) -> str | None:
    """The cid when the token is valid and unexpired, else None."""
    try:
        v, exp, cid, sig = token.split(".", 3)
        if v != "v1" or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", cid) or int(exp) < (now or time.time()):
            return None
        want = hmac.new(secret.encode(), f"{exp}.{cid}".encode(), hashlib.sha256).hexdigest()[:32]
        return cid if hmac.compare_digest(sig, want) else None
    except Exception:
        return None
