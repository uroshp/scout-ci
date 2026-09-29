"""Set a card's structured-source watch (WS3): `meta.watch = {edgar_cik, job_boards: [{host, token}]}`.
The caller commits the meta change. Examples:

  python scripts/set_watch.py --slug salesforce__vs__hubspot__x --edgar-cik 1108524
  python scripts/set_watch.py --slug anthropic__vs__openai__x --job-board ashby:openai --job-board greenhouse:anthropic
  python scripts/set_watch.py --slug x --clear
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--edgar-cik", default=None)
    ap.add_argument("--job-board", action="append", default=[], help="host:token (greenhouse|ashby|lever)")
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    from scout import store
    meta = store.load_meta(a.slug)
    if meta is None:
        sys.exit(f"no such card: {a.slug}")
    if a.clear:
        meta.pop("watch", None)
    else:
        w = meta.get("watch") or {}
        if a.edgar_cik:
            ciks = [c for c in (w.get("edgar_ciks") or []) if c]
            if str(int(a.edgar_cik)) not in ciks:
                ciks.append(str(int(a.edgar_cik)))
            w["edgar_ciks"] = ciks
            w.pop("edgar_cik", None)
        boards = [b for b in (w.get("job_boards") or []) if isinstance(b, dict)]
        for jb in a.job_board:
            host, _, token = jb.partition(":")
            if host not in ("greenhouse", "ashby", "lever") or not token:
                sys.exit(f"bad --job-board {jb!r}: host:token with host in greenhouse|ashby|lever")
            if not any(b.get("host") == host and b.get("token") == token for b in boards):
                boards.append({"host": host, "token": token})
        w["job_boards"] = boards
        meta["watch"] = w
    store.save_meta(a.slug, meta)
    print(a.slug, "watch =", meta.get("watch"))


if __name__ == "__main__":
    main()
