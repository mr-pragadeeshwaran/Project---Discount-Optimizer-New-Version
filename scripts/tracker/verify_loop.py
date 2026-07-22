"""
verify_loop.py — prove the weekly feedback loop actually closes, end to end.

Simulates two weeks with NO new data required: it treats a real historical week
from the existing fact_table as the "fresh export" that fills in actuals, so we
can watch the scorecard compute and the kill-switch fire on real numbers.

Steps:
  1. Reset tracker state (history / baselines / execution log).
  2. Run the tracker for W1 dated to a week that EXISTS in the data -> logs predictions.
  3. Write an execution log marking the cut cells as applied (GAP 3).
  4. Re-run with --actuals = the fact_table -> backfills W1 actuals, runs the
     kill-switch, scores only applied cells.
  5. Assert the loop closed: actuals filled, scored > 0, scorecard populated.

Run:  python -X utf8 scripts/tracker/verify_loop.py
"""
import os, sys, glob, subprocess
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
HIST = os.path.join(ROOT, "DISCOUNT_PLAN", "tracker_history.csv")
EXEC = os.path.join(ROOT, "DISCOUNT_PLAN", "execution_log.csv")
BASE = os.path.join(ROOT, "DISCOUNT_PLAN", "baselines.json")
TRACKER = os.path.join(HERE, "weekly_tracker.py")
SIM_WEEK_DATE = "2026-06-15"   # a Monday that exists in the 6-month data


def _fact_table():
    for r in sorted(glob.glob(os.path.join(ROOT, "output", "runs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f
    raise SystemExit("no fact_table.csv")


def _run(*args):
    r = subprocess.run([sys.executable, "-X", "utf8", TRACKER, *args],
                       cwd=ROOT, capture_output=True, text=True)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("[tracker]"):
            print("   ", ln)
    if r.returncode != 0:
        print(r.stderr[-1500:]); raise SystemExit("tracker run failed")


def main():
    for f in (HIST, EXEC, BASE):
        if os.path.exists(f):
            os.remove(f)
    print("STEP 1-2 — log W1 predictions (dated to a real data week):")
    _run("--week", "W1", "--date", SIM_WEEK_DATE)

    print("STEP 3 — mark the cut cells 'applied' in the execution log:")
    h = pd.read_csv(HIST)
    cuts = h[(h["week"] == "W1") & (h.get("week_action", "") == "cut")]
    pd.DataFrame({"week": "W1", "cell_id": cuts["cell_id"], "applied": "Y"}).to_csv(EXEC, index=False)
    print(f"    marked {len(cuts)} applied cuts")

    print("STEP 4 — fresh export arrives -> backfill actuals + kill-switch + score:")
    _run("--actuals", _fact_table())

    print("STEP 5 — verify the loop closed:")
    h = pd.read_csv(HIST)
    w1 = h[h["week"] == "W1"]
    filled = int(w1["actual_net_rev_delta"].notna().sum())
    scored = int((w1["actual_net_rev_delta"].notna() & w1.get("applied", False).astype(bool)).sum())
    strikes = int((pd.to_numeric(w1.get("strikes"), errors="coerce").fillna(0) > 0).sum()) if "strikes" in w1 else 0
    confounded = int((w1.get("cell_status", "") == "confounded").sum()) if "cell_status" in w1 else 0
    print(f"    W1 rows: {len(w1)} | actuals filled: {filled} | scored (applied+actual): {scored}")
    print(f"    kill-switch: {strikes} cells with >=1 strike | {confounded} confounded (stock-out, excused)")
    ok = filled > 0 and scored > 0 and "strikes" in w1.columns and "cell_status" in w1.columns
    print(f"\n  LOOP CLOSED: {'YES ✓' if ok else 'NO ✗'} — "
          f"actuals fill, only-applied scoring, and the kill-switch all ran on real numbers.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
