#!/usr/bin/env python
"""Run one auto-apply pass right now, by hand.

    .venv/bin/python tools/auto_apply_run.py            follows DRY_RUN (on unless you set DRY_RUN=false)
    .venv/bin/python tools/auto_apply_run.py --no-sync  skip the Simplify/JobRight sync (use what's stored)
    .venv/bin/python tools/auto_apply_run.py --no-push  don't send the summary notification

Prints a Pass/Skip table for every candidate. Shares the run lock with the scheduler, so it
won't overlap a scheduled pass. Builds the app without background threads.
"""

import argparse
import os
import sys

os.environ["AUTOAPPLY_NO_BACKGROUND"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg  # noqa: E402
from app_factory import create_app  # noqa: E402
from automation import auto_apply_run  # noqa: E402
from models import AutoApplyDecision  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        print(f"mode: {'DRY RUN (nothing will be applied)' if cfg.DRY_RUN else 'LIVE - this will apply'}")
        run = auto_apply_run.run_once(app, trigger="manual", sync=not args.no_sync, notify=not args.no_push)
        if run is None:
            print("Another run is in progress; nothing done.")
            return 1
        print(f"run {run.id}: {run.status}  found={run.found} passed={run.passed} "
              f"{'would_apply' if run.dry_run else 'applied'}={run.would_apply if run.dry_run else run.applied} "
              f"failed={run.failed} needs_you={run.needs_you}")
        if run.message:
            print("notes:", run.message)
        print("skipped by rule:", run.skipped or "none")
        rows = run.decisions.order_by(AutoApplyDecision.verdict.desc(), AutoApplyDecision.match_score.desc(), AutoApplyDecision.id).all()
        for d in rows:
            print(f"{'Pass' if d.verdict == 'pass' else 'Skip':4} | {d.source:8} | {d.match_score if d.match_score is not None else '-':>3} | "
                  f"{(d.title or '')[:45]:45} | {(d.company or '')[:22]:22} | {d.category or d.action:14} | {d.reason or d.message or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
