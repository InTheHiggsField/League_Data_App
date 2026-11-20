"""
Builds the website's data file (docs/data.js) from league.db.

    python export_site.py                    # latest 2 patches (default)
    python export_site.py --patches 16.22    # only these patches
    python export_site.py --patches all

Run it again after each collection run to update the site. It only reads
league.db and never calls the Riot API.

What gets exported, for each tier group (All / Iron-Gold / Plat-Emerald / Diamond+)
and each role:
  combos    - every champion with >= MIN_COMBO_GAMES games in that role:
              [champion_id, games, wins, spread, conservative_spread, matchups_used]
              (see matchup_spread below)
  matchups  - lane matchups with >= MIN_MATCHUP_GAMES games where both
              champions pass the combo minimum: champ, opponent, games, wins

The website (docs/index.html + docs/app.js) works out the first-pick and
counter-pick lists from these in the browser.
"""

import argparse
import json
import math
import os
import sqlite3
import time
from collections import defaultdict

MIN_COMBO_GAMES = 100      # champion+role combos below this are not shown at all
SMALL_SAMPLE_GAMES = 500   # combos below this get a "small sample size" badge (done in the site)
MIN_MATCHUP_GAMES = 50     # a single lane matchup needs this many games to be listed

ROLES = ["top", "jungle", "mid", "bot", "support"]
TIER_GROUPS = {
    "all":  {"label": "All ranks",          "tiers": None},
    "low":  {"label": "Iron – Gold",        "tiers": ["IRON", "BRONZE", "SILVER", "GOLD"]},
    "mid":  {"label": "Platinum – Emerald", "tiers": ["PLATINUM", "EMERALD"]},
    "high": {"label": "Diamond+",           "tiers": ["DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]},
}
GOOD = "m.is_remake = 0 AND m.queue_id = 420"


def matchup_spread(matchups, z=1.0):
    """
    How much a champion's winrate changes depending on the lane opponent,
    in winrate points (0.03 = matchups typically sit about ±3% from its average).

    matchups: list of (games, wins), one per lane opponent.
    Returns (spread, conservative_spread, matchups_used).

    A plain standard deviation of matchup winrates is mostly luck. A 20-game
    matchup swings about ±11% by chance. So:

      1. Weighted variance of matchup winrates around the champion's average,
         weighted by games, so common matchups count more. These are the ones
         you'll actually face when you pick blind.
      2. Subtract the variance luck alone would produce with these game counts.
         What's left is real matchup dependence; its square root is `spread`.
      3. With little data per matchup, step 2 can cancel everything out and give
         0, which really means "can't tell". So we also work out how uncertain
         the estimate is and add one standard error. That's `conservative_spread`:
         roughly 84% sure the real spread is no bigger than this. The site ranks
         first picks by it, so a champion only ranks as consistent if the data shows it.
    """
    matchups = [(g, w) for g, w in matchups if g > 0]
    k = len(matchups)
    n = sum(g for g, _ in matchups)
    if k < 2 or n == 0:
        return None, None, k
    p = sum(w for _, w in matchups) / n
    pq = max(p * (1 - p), 1e-6)
    observed = sum(g * (w / g - p) ** 2 for g, w in matchups) / n
    noise = (k - 1) * pq / n
    true_var = max(0.0, observed - noise)

    # Each matchup's squared deviation is roughly (luck + true variance) * chi-square(1),
    # whose variance is 2 * sigma^4. Summing the weighted terms gives the variance of `observed`.
    var_obs = sum(2 * (g * (pq / g + true_var)) ** 2 for g, _ in matchups) / n ** 2
    upper_var = true_var + z * math.sqrt(var_obs)
    return math.sqrt(true_var), math.sqrt(upper_var), k


def patch_key(patch):
    return tuple(int(x) for x in patch.split(".") if x.isdigit())


def choose_patches(conn, arg):
    patches = sorted((r[0] for r in conn.execute(
        f"SELECT DISTINCT patch FROM matches m WHERE {GOOD} AND patch IS NOT NULL")), key=patch_key)
    if arg == "all":
        return patches
    if arg:
        return [p.strip() for p in arg.split(",")]
    return patches[-2:]


def export(db_path="league.db", out_path="docs/data.js", patches_arg=None):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    patches = choose_patches(conn, patches_arg)
    pf = f"m.patch IN ({','.join('?' * len(patches))})"

    champions = {r[0]: {"name": r[1], "api": r[2]}
                 for r in conn.execute("SELECT champion_id, name, api_name FROM champions")}
    # Champions missing from the Data Dragon table fall back to the name in the match data.
    for cid, name in conn.execute("SELECT DISTINCT champion_id, champion_name FROM participants"):
        champions.setdefault(cid, {"name": name, "api": name})

    # Raw counts by tier, so tier groups can be summed in Python.
    games_by_tier = defaultdict(int)
    for tier, n in conn.execute(f"SELECT tier, COUNT(*) FROM matches m WHERE {GOOD} AND {pf} GROUP BY tier",
                                patches):
        games_by_tier[tier] += n

    combo_rows = conn.execute(f"""
        SELECT m.tier, p.role, p.champion_id, COUNT(*), SUM(p.win)
        FROM participants p JOIN matches m ON m.match_id = p.match_id
        WHERE {GOOD} AND {pf} AND p.role IS NOT NULL
        GROUP BY m.tier, p.role, p.champion_id""", patches).fetchall()

    matchup_rows = conn.execute(f"""
        SELECT m.tier, p.role, p.champion_id, o.champion_id, COUNT(*), SUM(p.win)
        FROM participants p
        JOIN matches m ON m.match_id = p.match_id
        JOIN participants o ON o.match_id = p.match_id AND o.role = p.role AND o.team_id != p.team_id
        WHERE {GOOD} AND {pf} AND p.role IS NOT NULL
        GROUP BY m.tier, p.role, p.champion_id, o.champion_id""", patches).fetchall()

    groups = {}
    for gkey, g in TIER_GROUPS.items():
        in_group = (lambda t: True) if g["tiers"] is None else (lambda t, ts=g["tiers"]: t in ts)

        combos = defaultdict(lambda: [0, 0])            # (role, champ) -> [games, wins]
        for tier, role, cid, n, w in combo_rows:
            if in_group(tier):
                combos[(role, cid)][0] += n
                combos[(role, cid)][1] += w
        mus = defaultdict(lambda: [0, 0])               # (role, champ, opp) -> [games, wins]
        for tier, role, cid, oid, n, w in matchup_rows:
            if in_group(tier):
                mus[(role, cid, oid)][0] += n
                mus[(role, cid, oid)][1] += w

        by_champ = defaultdict(list)                    # (role, champ) -> [(games, wins)] for spread
        for (role, cid, oid), (n, w) in mus.items():
            by_champ[(role, cid)].append((n, w))

        roles = {}
        for role in ROLES:
            shown = {cid for (r, cid), (n, _) in combos.items() if r == role and n >= MIN_COMBO_GAMES}
            combo_list = []
            for cid in sorted(shown):
                n, w = combos[(role, cid)]
                spread, safe, k = matchup_spread(by_champ[(role, cid)])
                combo_list.append([cid, n, w,
                                   None if spread is None else round(spread, 4),
                                   None if safe is None else round(safe, 4), k])
            mu_list = [[cid, oid, n, w] for (r, cid, oid), (n, w) in mus.items()
                       if r == role and cid in shown and oid in shown and n >= MIN_MATCHUP_GAMES]
            roles[role] = {"combos": combo_list, "matchups": sorted(mu_list)}

        total = sum(n for t, n in games_by_tier.items() if in_group(t))
        groups[gkey] = {"label": g["label"], "games": total, "roles": roles}

    used = {c[0] for g in groups.values() for r in g["roles"].values() for c in r["combos"]}
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "patches": patches,
        "thresholds": {"combo": MIN_COMBO_GAMES, "small": SMALL_SAMPLE_GAMES, "matchup": MIN_MATCHUP_GAMES},
        "champions": {str(c): champions[c] for c in sorted(used)},
        "groups": groups,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # A .js file (not .json) so index.html also works when opened by double-click,
    # where browsers block loading .json files.
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("window.SITE_DATA = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")

    print(f"patches {', '.join(patches)}")
    for gkey, g in groups.items():
        counts = ", ".join(f"{r} {len(v['combos'])}" for r, v in g["roles"].items())
        print(f"{g['label']:<20} {g['games']:>6} games | champions shown: {counts}")
    print(f"wrote {out_path} ({os.path.getsize(out_path) // 1024} KB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Export website data from league.db")
    ap.add_argument("--db", default="league.db")
    ap.add_argument("--out", default="docs/data.js")
    ap.add_argument("--patches", default=None, help="'16.21,16.22', 'all', or leave out for the latest 2")
    a = ap.parse_args()
    export(a.db, a.out, a.patches)
