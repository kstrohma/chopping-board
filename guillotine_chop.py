#!/usr/bin/env python3
"""
Guillotine "chop screen" for a Fleaflicker league.

Computes, for every surviving team, the probability that it finishes the week
with the lowest score in the league (i.e. gets chopped), via Monte Carlo over
per-player score distributions.

Usage:
    pip install requests numpy
    python guillotine_chop.py --league 350513 --week 3
    python guillotine_chop.py --league 350513 --week 3 --json out.json
    python guillotine_chop.py --league 350513 --week 3 --inspect 12345

Read-only, unauthenticated API. Be polite with request rate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque

import numpy as np
import requests

BASE = "https://www.fleaflicker.com/api"

# Coefficient of variation (SD / mean) for weekly fantasy scores by position.
# These are ballpark empirical values for half-PPR; tune them against your
# league's own scoring history if you want to be rigorous.
POSITION_CV = {
    "QB": 0.40,
    "RB": 0.55,
    "WR": 0.65,
    "TE": 0.70,
    "K": 0.50,
    "D/ST": 0.65,
    "DST": 0.65,
    "DEF": 0.65,
}
DEFAULT_CV = 0.60

# Weight of the shared per-NFL-team random factor (captures QB/WR stacking and
# game-environment effects). 0 disables correlation entirely.
DEFAULT_RHO = 0.15

# Fallback fraction of a projection still "live" for an in-progress player when
# we can't tell how far along their game is (no kickoff timestamp). When we do
# have a kickoff time we scale by wall-clock elapsed instead — see
# elapsed_fraction — so this constant only bites on malformed game data.
IN_PROGRESS_REMAINING = 0.45

# Typical NFL broadcast length, kickoff to final whistle, in seconds. Fleaflicker
# exposes a game's start time and status but no game clock, so we approximate how
# much of a live player's projection is still to come from wall-clock elapsed.
GAME_WALLCLOCK_SECONDS = 11_700  # ~3h15m


# ---------------------------------------------------------------- API plumbing


def fetch(endpoint: str, **params):
    params.setdefault("sport", "NFL")
    last = None
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=25)
        except requests.RequestException as exc:
            last = exc
            time.sleep(2**attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            last = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
    raise RuntimeError(f"{endpoint} failed after retries: {last}")


def _num(v):
    """Coerce a Fleaflicker value (raw number or {value, formatted}) to float."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        if isinstance(v.get("value"), (int, float)):
            return float(v["value"])
        if isinstance(v.get("formatted"), str):
            try:
                return float(v["formatted"].replace(",", ""))
            except ValueError:
                return None
    return None


def find_num(obj, must, must_not=()):
    """
    Breadth-first search for the shallowest key whose name contains every
    substring in `must` and none in `must_not`, returning its numeric value.

    Fleaflicker's JSON is protobuf-derived: field names differ from the published
    docs (camelCase in responses, snake_case in the docs) and zero values are
    omitted entirely. Searching by key fragment is more durable than hardcoding
    a path. Use --inspect to see the real shape and pin these down if you'd
    rather be explicit.
    """
    queue = deque([obj])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = k.lower()
                if all(m in kl for m in must) and not any(m in kl for m in must_not):
                    n = _num(v)
                    if n is not None:
                        return n
                queue.append(v)
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


def find_str(obj, must):
    queue = deque([obj])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if all(m in k.lower() for m in must) and isinstance(v, str):
                    return v
                queue.append(v)
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


# ------------------------------------------------------------- data extraction


def get_teams(league_id: int, season: int):
    data = fetch("FetchLeagueStandings", league_id=league_id, season=season)
    teams = []
    for div in data.get("divisions", []):
        for t in div.get("teams", []):
            teams.append({"id": t["id"], "name": t.get("name", f"team {t['id']}")})
    if not teams:
        raise RuntimeError("No teams found — check league_id and season.")
    return teams


def game_state(slot_obj) -> str:
    """Return FINAL, IN_PROGRESS or PRE for the player's NFL game."""
    status = (find_str(slot_obj, ("status",)) or "").upper()
    if "FINAL" in status or "COMPLETE" in status:
        return "FINAL"
    if "PROGRESS" in status or "LIVE" in status or "HALF" in status:
        return "IN_PROGRESS"
    return "PRE"


def elapsed_fraction(slot_obj, now_ms=None):
    """
    Fraction of a player's game elapsed by wall clock, clamped to [0, 1].

    Fleaflicker gives a kickoff timestamp but no game clock, so progress is
    approximated as (now - kickoff) / typical broadcast length. Returns None when
    no usable kickoff time is present, letting callers fall back to a flat guess.
    """
    ts = find_str(slot_obj, ("starttime",))
    if not ts:
        return None
    try:
        kickoff_ms = float(ts)
    except (TypeError, ValueError):
        return None
    if now_ms is None:
        now_ms = time.time() * 1000.0
    frac = (now_ms - kickoff_ms) / (GAME_WALLCLOCK_SECONDS * 1000.0)
    return min(1.0, max(0.0, frac))


def get_starters(league_id: int, team_id: int, season: int, week: int):
    data = fetch(
        "FetchRoster",
        league_id=league_id,
        team_id=team_id,
        season=season,
        scoring_period=week,
    )
    players = []
    for group in data.get("groups", []):
        if group.get("group") != "START":
            continue
        for slot in group.get("slots", []):
            lp = slot.get("leaguePlayer")
            if not lp:
                continue  # empty starting slot
            pp = lp.get("proPlayer", {})
            proj = find_num(lp, ("proj",), ("season", "total", "average", "rank"))
            actual = find_num(lp, ("actual",), ("season", "total", "average"))
            pro_team = pp.get("proTeamAbbreviation") or (
                pp.get("proTeam", {}) or {}
            ).get("abbreviation")
            pos = pp.get("position") or (slot.get("position", {}) or {}).get("label")
            state = game_state(slot)

            proj = proj or 0.0
            actual = actual or 0.0
            if state == "FINAL":
                fixed, remaining = actual, 0.0
            elif state == "IN_PROGRESS":
                # Bank what's scored; project only the slice of the game still to
                # be played. Scale the pre-game projection by wall-clock time
                # left, falling back to the flat estimate if we can't place the
                # game on the clock. As the game ends, remaining -> 0 and the
                # score converges to actual (and its simulated spread collapses).
                frac = elapsed_fraction(slot)
                remaining_frac = (
                    (1.0 - frac) if frac is not None else IN_PROGRESS_REMAINING
                )
                fixed, remaining = actual, proj * remaining_frac
            else:
                fixed, remaining = 0.0, proj

            players.append(
                {
                    "name": pp.get("nameFull") or pp.get("nameShort") or "?",
                    "pos": pos or "FLEX",
                    "unit": pro_team,
                    "state": state,
                    "fixed": fixed,
                    "remaining": remaining,
                }
            )
    return players, data


def build_pool(league_id, season, week, exclude, delay=0.3, verbose=True):
    pool = {}
    for t in get_teams(league_id, season):
        if t["id"] in exclude:
            continue
        players, _ = get_starters(league_id, t["id"], season, week)
        total_proj = sum(p["fixed"] + p["remaining"] for p in players)
        if not players or total_proj <= 0:
            if verbose:
                print(
                    f"  skipping {t['name']} (no live starters — chopped?)",
                    file=sys.stderr,
                )
            continue
        pool[t["name"]] = {
            "id": t["id"],
            "players": players,
            "fixed": sum(p["fixed"] for p in players),
            "projection": total_proj,
        }
        if verbose:
            print(f"  {t['name']}: {len(players)} starters, {total_proj:.1f} proj",
                  file=sys.stderr)
        time.sleep(delay)
    if len(pool) < 2:
        raise RuntimeError("Fewer than two live teams — nothing to simulate.")
    return pool


# ------------------------------------------------------------------ simulation


def simulate(pool, n_sims=50_000, rho=DEFAULT_RHO, seed=None):
    """
    Per player:  score ~ Gamma(mean = projection * G_team, cv = cv[position])
    where G_team is a shared Gamma(mean 1, cv = rho) factor for every player on
    the same NFL team. Gamma is used rather than Normal because weekly scores
    are right-skewed and bounded below at roughly zero; a Normal model puts
    mass on negative scores and understates ceiling weeks.

    Independence across players would make league-minimum estimates too
    confident, hence the shared factor. Teams with no remaining players are
    still included: their score is deterministic.
    """
    rng = np.random.default_rng(seed)
    names = list(pool)

    units = sorted(
        {p["unit"] for t in pool.values() for p in t["players"] if p["unit"]}
    )
    uidx = {u: i for i, u in enumerate(units)}
    if rho > 0 and units:
        shape = 1.0 / rho**2
        gf = rng.gamma(shape, 1.0 / shape, size=(n_sims, len(units)))
    else:
        gf = None

    scores = np.empty((n_sims, len(names)))
    for j, name in enumerate(names):
        team = pool[name]
        s = np.full(n_sims, team["fixed"], dtype=float)
        for p in team["players"]:
            mean = p["remaining"]
            if mean <= 0:
                continue
            cv = POSITION_CV.get(str(p["pos"]).upper(), DEFAULT_CV)
            shape = 1.0 / cv**2
            if gf is not None and p["unit"] in uidx:
                m = mean * gf[:, uidx[p["unit"]]]
            else:
                m = np.full(n_sims, mean)
            s += rng.gamma(shape, m / shape)
        scores[:, j] = s

    ranks = scores.argsort(axis=1).argsort(axis=1)  # 0 == lowest score
    chop = (ranks == 0).mean(axis=0)
    bottom2 = (ranks <= 1).mean(axis=0)

    rows = []
    for j, name in enumerate(names):
        col = scores[:, j]
        rows.append(
            {
                "team": name,
                "team_id": pool[name]["id"],
                "projection": pool[name]["projection"],
                "banked": pool[name]["fixed"],
                "median": float(np.median(col)),
                "p10": float(np.percentile(col, 10)),
                "p90": float(np.percentile(col, 90)),
                "chop_prob": float(chop[j]),
                "bottom2_prob": float(bottom2[j]),
                "safety": 1.0 - float(chop[j]),
            }
        )
    rows.sort(key=lambda r: -r["chop_prob"])
    return rows


# ----------------------------------------------------------------------- entry


def print_table(rows, week):
    print(f"\nChop probabilities — week {week}  ({len(rows)} live teams)\n")
    header = f"{'Team':<26}{'Proj':>8}{'Banked':>9}{'p10':>8}{'p90':>8}{'Chop%':>8}{'Bot2%':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['team'][:25]:<26}"
            f"{r['projection']:>8.1f}"
            f"{r['banked']:>9.1f}"
            f"{r['p10']:>8.1f}"
            f"{r['p90']:>8.1f}"
            f"{r['chop_prob']*100:>7.1f}%"
            f"{r['bottom2_prob']*100:>7.1f}%"
        )
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--sims", type=int, default=50_000)
    ap.add_argument("--rho", type=float, default=DEFAULT_RHO,
                    help="shared per-NFL-team factor CV; 0 for independence")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--exclude", type=int, nargs="*", default=[],
                    help="team ids to drop (already chopped)")
    ap.add_argument("--json", metavar="PATH", help="also write results as JSON")
    ap.add_argument("--inspect", type=int, metavar="TEAM_ID",
                    help="dump one raw roster response and exit")
    args = ap.parse_args()

    if args.inspect:
        _, raw = get_starters(args.league, args.inspect, args.season, args.week)
        json.dump(raw, sys.stdout, indent=2)
        return

    print("Fetching rosters...", file=sys.stderr)
    pool = build_pool(args.league, args.season, args.week, set(args.exclude))
    rows = simulate(pool, n_sims=args.sims, rho=args.rho, seed=args.seed)
    print_table(rows, args.week)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"week": args.week, "teams": rows}, fh, indent=2)
        print(f"wrote {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
