"""Reset the public demo now: wipe what visitors made and seed it again.

    python -m scripts.demo_reset            # reset (needs DEMO_MODE=true)
    python -m scripts.demo_reset --seed     # only add what is missing, wipe nothing

Run inside the engine's container (it reads the same settings), by hand or
from an outside scheduler. The engine also does this itself every night at
DEMO_RESET_HOUR_UTC (demo_mode/reset.py); see docs/deploy/demo.md.
Exit code 0 on success, 1 on a failure, 2 when demo mode is off.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", action="store_true", help="only seed what is missing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from demo_mode import settings

    if not settings.enabled():
        print("DEMO_MODE is not on: this command only runs on a demo install.", file=sys.stderr)
        return 2
    try:
        from demo_mode import account, reset, seed

        account.ensure_demo_user()
        result = seed.seed() if args.seed else reset.reset()
    except Exception as exc:  # noqa: BLE001
        print(f"demo reset failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
