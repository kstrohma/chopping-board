# Guillotine chop screen

Weekly chop probabilities for a Fleaflicker guillotine league, published as a
static page on GitHub Pages and refreshed by a scheduled GitHub Action.

The page answers one question: how likely is each surviving team to post the
lowest score this week and get cut.

## Setup

1. Create a **public** repository and push these files. Public matters for two
   reasons: GitHub Pages is free on public repos for personal accounts, and
   Actions minutes are unmetered on public repos.

2. Settings → Pages → build from a branch → `main`, folder `/docs`.

3. Verify the projection extraction before trusting anything (see below).

4. Actions tab → *update chop data* → *Run workflow* to publish the first
   `docs/data.json`. The page is live once that commit lands.

Local run:

```
pip install -r requirements.txt
python guillotine_chop.py --league 350513 --week 3      # table in the terminal
python build_data.py                                    # writes docs/data.json
python -m http.server -d docs                           # preview at :8000
```

## Verify this first

`guillotine_chop.py` locates each starter's weekly projection by searching the
roster JSON for keys containing `proj`, because Fleaflicker's API is
protobuf-derived and undocumented: responses come back camelCase although the
published docs show snake_case, and zero values are omitted from the JSON
entirely rather than serialized as 0.

That search is durable against renames but it can grab the wrong field — a
season total or a per-game average instead of a weekly projection. Check once:

```
python guillotine_chop.py --league 350513 --week 3 --inspect <team_id> > roster.json
```

Confirm the number it pulls is a single week's projection. If not, replace
`find_num` with the explicit key path. Every number on the page is downstream of
this.

## What the model assumes

Each starter's score is drawn from a Gamma distribution with mean equal to his
projection and a coefficient of variation set by position (`POSITION_CV`). Gamma
rather than Normal because weekly fantasy scores are right-skewed and bounded
near zero; a Normal model puts probability mass on negative scores and clips the
ceiling weeks that decide guillotine survival.

Players on the same NFL team share a random multiplier (`--rho`, default 0.15)
so that QB/WR stacks move together. Without it, league-minimum estimates come
out overconfident.

**The variance numbers are unfitted guesses.** They are ballpark half-PPR values,
not derived from your league. They are also the single biggest driver of the
output, since a chop probability is a statement about the tails — the gap
between `cv=0.5` and `cv=0.7` on receivers moves probabilities by double digits.
Once you have several weeks of scores, fit the standard deviations from your own
history and replace the table.

Mid-game handling is the weakest part. A player whose game is in progress is
credited with his points so far plus `IN_PROGRESS_REMAINING` (45%) of his
projection. The API's game-status field doesn't reliably expose a game clock, so
this is a flat guess. Pre-kickoff the output is sound; during the 1pm window
treat it as directional.

Correlation between opposing players in the same game is not modeled.

## Week history

Once a week's games are final, `archive.py` snapshots the final scores so the
page can show them later. It writes three things under `docs/history/`:

```
week-NN.json    the finished-week board the page renders
week-NN.csv     raw final scores, for download / spreadsheets
index.json      the list of archived weeks the dropdown reads
```

The page's **View** dropdown offers "Current · Week N" (the live chop odds) plus
every archived week. Picking a past week shows that week's final scores and flags
who got chopped, with a CSV download link.

`archive.py` auto-detects the most recently completed week (it walks back from
the current scoring period to the last week whose starters have all finished),
so the scheduled job needs no week number. Archive one explicitly with
`python archive.py --week 1`.

The `archive week` workflow runs every Wednesday at 12:00 UTC (~14:00 in Austria
during the season), well after Monday Night Football has gone final. Trigger it
by hand from the Actions tab — with an optional week number — to backfill.

## Scheduling

Every 15 minutes during NFL game windows, hourly otherwise. Windows are UTC and
run an hour wide on each side so the November EDT → EST shift needs no edit.

GitHub's scheduler is best effort — its own docs note schedule events can be
delayed under load and that queued jobs may be dropped, and delays of 15–60
minutes are common. Runs are set to odd minutes (`:07 :22 :37 :52`) because the
top of the hour is the most congested slot. The page displays its own data age
and turns amber past 90 minutes, so a missed run is visible rather than silent.

If the delays bother you, point a free external scheduler (cron-job.org) at the
`repository_dispatch` webhook with a fine-grained PAT. Dispatch-triggered runs
skip the schedule queue and fire on time. The workflow already accepts a
`refresh` event type.

One seasonal gotcha: on public repos, scheduled workflows are automatically
disabled after 60 days with no repository activity. Bot commits don't reliably
reset that timer, so the schedule will likely go dormant over the offseason.
Re-enable it from the Actions tab, or push any commit, before week 1 next year.

## Excluding chopped teams

`build_pool` drops teams whose starters project to zero, which usually catches
eliminated rosters automatically. If a chopped team lingers, pass its id:

```
python build_data.py --exclude 12345 67890
```

Then add the same flag to the workflow's build step.
