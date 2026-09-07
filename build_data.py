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

import guillotine_chop as gc

LEAGUE_ID = int(os.environ.get("FF_LEAGUE_ID", 350513))
SEASON = int(os.environ.get("FF_SEASON", 2026))
N_SIMS = int(os.environ.get("FF_SIMS", 50_000))
RHO = float(os.environ.get("FF_RHO", gc.DEFAULT_RHO))

OUT = pathlib.Path(__file__).parent / "docs" / "data.json"


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
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--sims", type=int, default=N_SIMS)
    ap.add_argument("--rho", type=float, default=RHO)
    ap.add_argument("--exclude", type=int, nargs="*", default=[])
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    args = ap.parse_args()

    week = args.week or current_week(args.league, args.season)
    print(f"building week {week}", file=sys.stderr)

    pool = gc.build_pool(args.league, args.season, week, set(args.exclude))
    rows = gc.simulate(pool, n_sims=args.sims, rho=args.rho)

    live_players = sum(
        1 for t in pool.values() for p in t["players"] if p["remaining"] > 0
    )
    banked_players = sum(
        1 for t in pool.values() for p in t["players"] if p["state"] == "FINAL"
    )

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "league_id": args.league,
        "season": args.season,
        "week": week,
        "sims": args.sims,
        "rho": args.rho,
        "teams_live": len(rows),
        "players_yet_to_play": live_players,
        "players_finished": banked_players,
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
