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


def record_id_for(rid: str) -> str:
    """The answer id a client-chosen request token maps to (viewer and engine agree on it), so
    the panel knows where its answer will land before the engine starts: recovery after a dropped
    stream or a reload. The client cannot pick the id itself: it is a hash of the token."""
    import hashlib
    return "a_" + hashlib.sha256(f"rid:{rid}".encode()).hexdigest()[:12]


def owner_key_ok(authorization: str | None) -> bool:
    """True when the bearer matches one of ASK_API_KEYS (the owner). Constant-time compare."""
    import hmac
    import os
    tok = (authorization or "").removeprefix("Bearer ").strip()
    keys = [k.strip() for k in os.environ.get("ASK_API_KEYS", "").split(",") if k.strip()]
    return bool(tok) and any(hmac.compare_digest(tok, k) for k in keys)
