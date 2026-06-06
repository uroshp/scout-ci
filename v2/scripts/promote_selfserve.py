"""Promote a finished self-serve card from the PRIVATE data repo into the public
monitored roster (battlecards/<slug>/) — WITHOUT regenerating.

A self-serve job is produced by the same generate() pipeline as the showcase
cards (just write=False), so its output is already a finished, grounded card. The
private user_reports/ folder is the review queue: generate -> review -> promote
the good ones here. Promotion is deterministic file plumbing, not a re-run:

  card.md     -> battlecards/<slug>/current.md
  claims.json -> battlecards/<slug>/claims.json
  meta.json   -> synthesized (new_meta + monitored:true), baseline/last_checked
                 anchored to the generation time so the monitor's first run looks
                 for news since the card was generated.

Reads through scout.selfserve, so it transparently hits the private repo when
SELFSERVE_GH_TOKEN + SELFSERVE_REPO are set (local FS in dev).

  python scripts/promote_selfserve.py <job_id> [--cadence H] [--force] [--dry-run]

After it writes, review `git status`, then commit + push — the monitor picks the
new card up on its next scheduled run.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import config, selfserve, store


def _load_request(job_id: str) -> dict:
    raw = selfserve._read(f"{selfserve.REQUESTS_DIR}/{job_id}.json")
    return json.loads(raw) if raw else {}


def promote(job_id: str, cadence: int | None = None, force: bool = False,
            dry_run: bool = False) -> str | None:
    res = selfserve.get_result(job_id)
    if not res:
        print(f"No result for job '{job_id}' yet — is it still generating? "
              f"(or wrong job_id)")
        return None
    if res.get("status") != "done":
        print(f"Job '{job_id}' status is '{res.get('status')}', not 'done' "
              f"({res.get('message','')}). Nothing to promote.")
        return None

    req = _load_request(job_id)
    competitor = req.get("competitor")
    my_company = req.get("my_company")
    focus = req.get("focus")
    if not competitor:
        print(f"Could not read the request for '{job_id}' (need competitor/"
              f"my_company/focus to build meta). Aborting.")
        return None

    slug = res.get("slug") or store.make_slug(competitor, my_company, focus)

    claims_raw = selfserve._read(f"{selfserve.RESULTS_DIR}/{job_id}/claims.json")
    markdown = res.get("markdown") or ""
    if not claims_raw or not markdown:
        print(f"Missing card.md or claims.json for '{job_id}'. Aborting.")
        return None
    claims = json.loads(claims_raw)

    dest = store.battlecard_dir(slug)
    if os.path.exists(dest) and not force:
        print(f"battlecards/{slug}/ already exists — refusing to overwrite. "
              f"Re-run with --force to replace it.")
        return None

    meta = store.new_meta(competitor, my_company, focus, slug, cadence_hours=cadence)
    meta["monitored"] = True
    # Anchor baseline/last_checked to generation time so the first monitor run
    # searches for news SINCE the card was generated, not since promotion.
    gen_ts = res.get("finished_at")
    if gen_ts:
        meta["last_checked"] = gen_ts
        meta["baseline_date"] = gen_ts[:10]

    print(f"Promote '{job_id}'  ->  battlecards/{slug}/")
    print(f"  {my_company or '(no company)'} vs {competitor}"
          f"{' · focus: ' + focus if focus else ''}")
    print(f"  {len(claims)} claims · cadence {meta['cadence_hours']}h · "
          f"baseline {meta['baseline_date']} · monitored=True")
    if dry_run:
        print("  (dry run — nothing written)")
        return slug

    paths = store.write_baseline(slug, claims, meta, markdown)
    print(f"  wrote {paths['dir']}")
    print("\nNext: review `git status`, then commit + push. The monitor picks it "
          "up on its next scheduled run.")
    return slug


def main() -> None:
    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("--")}
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        print("usage: python scripts/promote_selfserve.py <job_id> "
              "[--cadence H] [--force] [--dry-run]")
        sys.exit(1)
    cadence = None
    for a in argv:
        if a.startswith("--cadence"):
            # supports --cadence 12 or --cadence=12
            cadence = int(a.split("=", 1)[1]) if "=" in a else int(argv[argv.index(a) + 1])
    promote(pos[0], cadence=cadence, force="--force" in flags, dry_run="--dry-run" in flags)


if __name__ == "__main__":
    main()
