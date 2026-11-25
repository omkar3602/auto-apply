#!/usr/bin/env python
"""Reset the day's application cap (60) for today only.

    .venv/bin/python tools/reset_daily_cap.py          start counting toward the cap again from now
    .venv/bin/python tools/reset_daily_cap.py --undo   put the normal count back

It only moves the start of today's count to "now"; tomorrow (New York time) counting starts at midnight
as usual. The one-application-per-company-per-day limit is NOT reset. Takes effect on the scheduler's
next run, no restart needed.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import auto_apply_run, schedule_state  # noqa: E402


def main(argv=None):
    undo = "--undo" in (argv if argv is not None else sys.argv[1:])
    if undo:
        schedule_state.save(auto_apply_run.CAP_RESET_KEY, None)
        print("Daily cap count restored to the full New York day.")
        return 0
    schedule_state.save(auto_apply_run.CAP_RESET_KEY, datetime.utcnow().isoformat())
    schedule_state.save(auto_apply_run.CAP_STATE_KEY, None)         # so you are told again when the cap is hit
    print("Daily cap reset for today: counting from now. Applications already made stay in the history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
