"""Reset the self-serve gate ledger in the PRIVATE data repo — used to wipe the
counter/spend that your OWN test runs added, so the real free-launch window starts
clean. Preserves free_limit + spend_ceiling_usd; does NOT touch the generated
reports or requests (the private review archive stays intact).

  python scripts/selfserve_reset.py [--used N] [--spend X] [--dry-run]

Defaults: used=0, spend=0. Needs SELFSERVE_GH_TOKEN + SELFSERVE_REPO in the env
(writes through scout.selfserve's GitHub-API backend).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import selfserve


def main() -> None:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    used, spend = 0, 0.0
    for a in argv:
        if a.startswith("--used"):
            used = int(a.split("=", 1)[1]) if "=" in a else int(argv[argv.index(a) + 1])
        if a.startswith("--spend"):
            spend = float(a.split("=", 1)[1]) if "=" in a else float(argv[argv.index(a) + 1])

    s = selfserve.load_state()
    print(f"before: used={s.get('used')}  spend_usd={s.get('spend_usd')}  "
          f"free_limit={s.get('free_limit')}  spend_ceiling_usd={s.get('spend_ceiling_usd')}")
    s["used"] = used
    s["spend_usd"] = round(float(spend), 4)
    print(f"after:  used={s['used']}  spend_usd={s['spend_usd']}  "
          f"(free_limit + spend_ceiling_usd preserved)")
    if dry:
        print("(dry run — nothing written)")
        return
    selfserve.save_state(s)
    print("wrote state.json to the private data repo.")


if __name__ == "__main__":
    main()
