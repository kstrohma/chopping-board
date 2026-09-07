#!/usr/bin/env python3
"""
Archive a completed week's FINAL scores for the history view.

Run after a week's games are all final (the scheduled job fires Wednesday, well
after Monday Night Football). Writes, for the finished week:

    docs/history/week-NN.json   the snapshot the page renders
    docs/history/week-NN.csv    raw final scores, for download / spreadsheets
    docs/history/index.json     the list of archived weeks the dropdown reads

By default it auto-detects the most recently completed week: it walks back from
the league's current scoring period until it finds a week whose starters have
all finished. Pass --week to archive a specific one.

Usage:
    python archive.py                 # auto-detect the finished week
    python archive.py --week 1        # archive week 1 explicitly
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import sys

import build_data as bd
import guillotine_chop as gc

LEAGUE_ID = int(os.environ.get("FF_LEAGUE_ID", 350513))
SEASON = int(os.environ.get("FF_SEASON", 2026))

HISTORY = pathlib.Path(__file__).parent / "docs" / "history"


def week_pool(league, season, week):
    """Build the pool for one week and report how 'final' it is."""
    pool = gc.build_pool(league, season, week, set(), verbose=False)
    pending = sum(
        1 for t in pool.values() for p in t["players"] if p["remaining"] > 0
    )
    banked = sum(t["fixed"] for t in pool.values())
    return pool, pending, banked


def find_completed_week(league, season):
    """Walk back from the current period to the last fully-final week."""
    start = bd.current_week(league, season)
    for cand in range(start, 0, -1):
        try:
            pool, pending, banked = week_pool(league, season, cand)
        except RuntimeError:
            continue
        if banked > 0 and pending == 0:
            return cand, pool
        if banked > 0 and pending > 0:
            print(f"week {cand} still has games in progress — skipping", file=sys.stderr)
    return None, None


def snapshot(week, pool):
    """Turn a final-week pool into ranked rows plus who got chopped."""
    rows = [
        {"team": name, "team_id": t["id"], "score": round(t["fixed"], 2)}
        for name, t in pool.items()
    ]
    rows.sort(key=lambda r: r["score"])  # lowest first — the chop end
    low = rows[0]["score"] if rows else 0.0
    for r in rows:
        r["chopped"] = r["score"] <= low + 1e-9  # ties: flag all at the minimum
    chopped = [r["team"] for r in rows if r["chopped"]]
    return {
        "week": week,
        "final": True,
        "archived_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "chopped": chopped[0] if len(chopped) == 1 else chopped,
        "low_score": low,
        "teams": rows,
    }


def write_week(payload):
    HISTORY.mkdir(parents=True, exist_ok=True)
    wk = payload["week"]
    stem = f"week-{wk:02d}"

    (HISTORY / f"{stem}.json").write_text(json.dumps(payload, indent=2) + "\n")

    with (HISTORY / f"{stem}.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "team", "final_score", "chopped"])
        for i, r in enumerate(payload["teams"], 1):
            w.writerow([i, r["team"], f"{r['score']:.2f}", "yes" if r["chopped"] else "no"])

    return stem


def update_index(payload, stem):
    """Upsert this week into the dropdown's index, kept sorted by week."""
    idx_path = HISTORY / "index.json"
    try:
        index = json.loads(idx_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        index = {"weeks": []}

    entry = {
        "week": payload["week"],
        "chopped": payload["chopped"],
        "low_score": payload["low_score"],
        "archived_at": payload["archived_at"],
        "json": f"history/{stem}.json",
        "csv": f"history/{stem}.csv",
    }
    weeks = [w for w in index.get("weeks", []) if w.get("week") != payload["week"]]
    weeks.append(entry)
    weeks.sort(key=lambda w: w["week"])
    index["weeks"] = weeks
    idx_path.write_text(json.dumps(index, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", type=int, default=LEAGUE_ID)
    ap.add_argument("--season", type=int, default=SEASON)
    ap.add_argument("--week", type=int, default=None,
                    help="week to archive (default: auto-detect the finished week)")
    args = ap.parse_args()

    if args.week is not None:
        pool, pending, banked = week_pool(args.league, args.season, args.week)
        if banked <= 0:
            print(f"week {args.week} has no scores yet — nothing to archive", file=sys.stderr)
            return 0
        if pending > 0:
            print(f"warning: week {args.week} still has games in progress", file=sys.stderr)
        week = args.week
    else:
        week, pool = find_completed_week(args.league, args.season)
        if week is None:
            print("no completed week to archive yet", file=sys.stderr)
            return 0

    payload = snapshot(week, pool)
    stem = write_week(payload)
    update_index(payload, stem)

    chopped = payload["chopped"]
    print(f"archived week {week}: chopped {chopped} at {payload['low_score']:.1f} "
          f"({len(payload['teams'])} teams)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        sys.exit(1)
