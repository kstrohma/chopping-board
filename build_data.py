#!/usr/bin/env python3
"""
Runs the chop simulation and writes docs/data.json for the static page.

Detects the current scoring period from the league scoreboard rather than taking
a week number, so the scheduled job needs no maintenance during the season.

Usage:
    python build_data.py                    # current week, league from config below
    python build_data.py --week 4           # override week
    python build_data.py --out other.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

import guillotine_chop as gc

LEAGUE_ID = int(os.environ.get("FF_LEAGUE_ID", 350513))
SEASON = int(os.environ.get("FF_SEASON", 2026))
N_SIMS = int(os.environ.get("FF_SIMS", 50_000))
RHO = float(os.environ.get("FF_RHO", gc.DEFAULT_RHO))
BUST = float(os.environ.get("FF_BUST", gc.DEFAULT_BUST))

OUT = pathlib.Path(__file__).parent / "docs" / "data.json"

# --- weekly cadence, in Vienna wall-clock time -----------------------------
# The board follows the league's own rhythm rather than Fleaflicker's period:
#   • each league week runs Wednesday 14:00 -> the next Wednesday 14:00;
#   • from Tuesday 07:00 the week is shown as FINAL (games done, cut locked in);
#   • the ROLLOVER to the next week is Wednesday 14:00 — after waivers clear.
# SEASON_ANCHOR is the Wednesday 14:00 that opens Week 1. Boundaries are compared
# in naive Vienna wall time, so the CEST/CET switch needs no adjustment: "Tuesday
# 07:00 Vienna" and "Wednesday 14:00 Vienna" hold year-round automatically.
VIENNA = ZoneInfo("Europe/Vienna")
SEASON_ANCHOR = dt.datetime(2026, 9, 9, 14, 0)  # Wed 14:00 Vienna, opens Week 1
FINAL_HOUR = 7                                    # Tue 07:00 Vienna -> FINAL
MAX_WEEK = 18


def schedule_week(now: dt.datetime | None = None) -> tuple[int, bool]:
    """
    (week, is_final) for this moment on the league's Vienna calendar.

    week steps up one at each Wednesday 14:00 Vienna (the waiver rollover);
    is_final becomes True once Tuesday 07:00 Vienna of that week has passed.
    """
    if now is None:
        now = dt.datetime.now(VIENNA)
    wall = now.astimezone(VIENNA).replace(tzinfo=None) if now.tzinfo else now
    if wall < SEASON_ANCHOR:
        return 1, False
    week = 1
    while week < MAX_WEEK and wall >= SEASON_ANCHOR + dt.timedelta(weeks=week):
        week += 1
    week_start = SEASON_ANCHOR + dt.timedelta(weeks=week - 1)
    final_at = (week_start + dt.timedelta(days=6)).replace(hour=FINAL_HOUR, minute=0)
    return week, wall >= final_at


def current_week(league_id: int, season: int) -> int:
    """
    Read the live scoring period off the scoreboard.

    Fleaflicker returns a schedule_period for the request and a list of eligible
    periods, each flagged with whether it contains the present moment. Field
    names come back camelCase despite the docs showing snake_case, so check both.
    """
    data = gc.fetch("FetchLeagueScoreboard", league_id=league_id, season=season)

    for key in ("schedulePeriod", "schedule_period"):
        period = data.get(key)
        if isinstance(period, dict):
            for path in (("ordinal",), ("low", "ordinal"), ("value",)):
                cur = period
                for step in path:
                    cur = cur.get(step) if isinstance(cur, dict) else None
                if isinstance(cur, int) and cur > 0:
                    return cur

    for key in ("eligibleSchedulePeriods", "eligible_schedule_periods"):
        for period in data.get(key, []) or []:
            if period.get("containsNow") or period.get("contains_now"):
                ordinal = period.get("ordinal") or (period.get("low") or {}).get(
                    "ordinal"
                )
                if isinstance(ordinal, int) and ordinal > 0:
                    return ordinal

    raise RuntimeError(
        "Could not determine the current scoring period from the scoreboard. "
        "Pass --week explicitly and check the response shape."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", type=int, default=LEAGUE_ID)
    ap.add_argument("--season", type=int, default=SEASON)
    ap.add_argument("--week", type=int,
                    default=int(os.environ["FF_WEEK"]) if os.environ.get("FF_WEEK") else None,
                    help="override the Vienna-calendar week (also FF_WEEK)")
    ap.add_argument("--sims", type=int, default=N_SIMS)
    ap.add_argument("--rho", type=float, default=RHO)
    ap.add_argument("--bust", type=float, default=BUST)
    ap.add_argument("--exclude", type=int, nargs="*", default=[])
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    args = ap.parse_args()

    sched_week, cal_final = schedule_week()
    week = args.week if args.week is not None else sched_week
    print(f"building week {week} (schedule says week {sched_week}, "
          f"final={cal_final})", file=sys.stderr)

    pool = gc.build_pool(args.league, args.season, week, set(args.exclude))
    rows = gc.simulate(pool, n_sims=args.sims, rho=args.rho, bust=args.bust)

    live_players = sum(
        1 for t in pool.values() for p in t["players"] if p["remaining"] > 0
    )
    banked_players = sum(
        1 for t in pool.values() for p in t["players"] if p["state"] == "FINAL"
    )
    in_progress_players = sum(
        1 for t in pool.values() for p in t["players"] if p["state"] == "IN_PROGRESS"
    )

    # Show the week as FINAL only when the calendar says so (past Tue 07:00
    # Vienna, on the auto-detected week), the games are actually settled, AND the
    # archive snapshot exists — the page reads that archive for the final view, so
    # gating on it avoids a window where "final" is claimed before the results are
    # published (and keeps the just-chopped team, cleared from live rosters, in
    # the picture).
    archive_ready = (args.out.parent / "history" / f"week-{week:02d}.json").exists()
    final = (
        args.week is None and cal_final and live_players == 0 and archive_ready
    )

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "league_id": args.league,
        "season": args.season,
        "week": week,
        "final": final,
        "phase": "final" if final else "live",
        "sims": args.sims,
        "rho": args.rho,
        "bust": args.bust,
        "teams_live": len(rows),
        "players_yet_to_play": live_players,
        "players_finished": banked_players,
        "players_in_progress": in_progress_players,
        "teams": rows,
    }

    # Write via a temp file so a crash mid-write can't leave the page holding
    # truncated JSON. Stale-but-valid always beats fresh-but-broken here.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(args.out)

    print(f"wrote {args.out} — {len(rows)} teams", file=sys.stderr)
    for r in rows[:3]:
        print(f"  {r['team']}: {r['chop_prob']*100:.1f}%", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail loudly so the workflow goes red
        print(f"build failed: {exc}", file=sys.stderr)
        sys.exit(1)
