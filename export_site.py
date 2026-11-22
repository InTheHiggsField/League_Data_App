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

Champion builds (from match timelines) go into one file per tier group,
docs/builds_<group>.js, which the site only loads when a champion profile
is opened. See export_builds() below.
"""

import argparse
import json
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict

MIN_COMBO_GAMES = 100      # champion+role combos below this are not shown at all
SMALL_SAMPLE_GAMES = 500   # combos below this get a "small sample size" badge (done in the site)
MIN_MATCHUP_GAMES = 50     # a single lane matchup needs this many games to be listed
MIN_BUILD_GAMES = MIN_COMBO_GAMES  # games with a timeline needed before a build is shown
                                   # (per champion + role + rank group + enemy comp type)
STARTER_SECONDS = 90       # purchases in the first 90 seconds count as starting items
MIN_OPTION_GAMES = 10      # an item / path / boots option needs this many games to be listed

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
        SELECT m.tier, p.role, p.champion_id, COUNT(*), SUM(p.win),
               SUM(COALESCE(p.phys_dmg, 0)), SUM(COALESCE(p.magic_dmg, 0)), SUM(COALESCE(p.true_dmg, 0))
        FROM participants p JOIN matches m ON m.match_id = p.match_id
        WHERE {GOOD} AND {pf} AND p.role IS NOT NULL
        GROUP BY m.tier, p.role, p.champion_id""", patches).fetchall()

    ban_rows = conn.execute(f"""
        SELECT m.tier, b.champion_id, COUNT(DISTINCT b.match_id)
        FROM bans b JOIN matches m ON m.match_id = b.match_id
        WHERE {GOOD} AND {pf} GROUP BY m.tier, b.champion_id""", patches).fetchall()

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

        combos = defaultdict(lambda: [0, 0, 0, 0, 0])   # (role, champ) -> [games, wins, phys, magic, true]
        for tier, role, cid, n, w, ph, mg, tr in combo_rows:
            if in_group(tier):
                for i, v in enumerate((n, w, ph, mg, tr)):
                    combos[(role, cid)][i] += v
        bans = defaultdict(int)
        for tier, cid, n in ban_rows:
            if in_group(tier):
                bans[cid] += n
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
            shown = {cid for (r, cid), v in combos.items() if r == role and v[0] >= MIN_COMBO_GAMES}
            combo_list = []
            for cid in sorted(shown):
                n, w, ph, mg, tr = combos[(role, cid)]
                spread, safe, k = matchup_spread(by_champ[(role, cid)])
                dmg = max(ph + mg + tr, 1)
                combo_list.append([cid, n, w,
                                   None if spread is None else round(spread, 4),
                                   None if safe is None else round(safe, 4), k,
                                   round(ph / dmg, 3), round(mg / dmg, 3), round(tr / dmg, 3)])
            mu_list = [[cid, oid, n, w] for (r, cid, oid), (n, w) in mus.items()
                       if r == role and cid in shown and oid in shown and n >= MIN_MATCHUP_GAMES]
            roles[role] = {"combos": combo_list, "matchups": sorted(mu_list)}

        total = sum(n for t, n in games_by_tier.items() if in_group(t))
        groups[gkey] = {"label": g["label"], "games": total, "roles": roles,
                        "bans": {str(c): n for c, n in bans.items()}}

    used = {c[0] for g in groups.values() for r in g["roles"].values() for c in r["combos"]}
    out_dir = os.path.dirname(out_path) or "."
    build_meta, build_items = export_builds(conn, patches, groups, out_dir)
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "patches": patches,
        "thresholds": {"combo": MIN_COMBO_GAMES, "small": SMALL_SAMPLE_GAMES, "matchup": MIN_MATCHUP_GAMES},
        "champions": {str(c): champions[c] for c in sorted(used)},
        "groups": groups,
        "items": build_items,
        "builds": build_meta,
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


# ------------------------------------------------------------------ builds
SKILLS = "QWE"


def skill_max_order(skills):
    """
    'QEWQQRQEQE...' -> 'QEW': the order the basic abilities were maxed (reached 5 points).
    Abilities not maxed yet are ranked by points, then by which was taken first.
    """
    pts = {k: 0 for k in SKILLS}
    maxed = []
    for ch in skills:
        if ch in pts:
            pts[ch] += 1
            if pts[ch] == 5:
                maxed.append(ch)
    rest = sorted((k for k in SKILLS if k not in maxed),
                  key=lambda k: (-pts[k], skills.find(k) if k in skills else 99))
    return "".join(maxed) + "".join(rest)


def _new_acc():
    return {
        "n": 0, "w": 0,
        "start": defaultdict(lambda: [0, 0]),                    # starting items (sorted tuple) -> [n, w]
        "boots": defaultdict(lambda: [0, 0, 0]),                 # boots id -> [n, w, total seconds]
        "path": defaultdict(lambda: [0, 0]),                     # first 3 completed items -> [n, w]
        "slots": [defaultdict(lambda: [0, 0]) for _ in range(5)],  # nth completed item -> {id: [n, w]}
        "reached": [0] * 5,                                      # games that got an nth completed item
        "secs": [[0, 0] for _ in range(3)],                      # time of 1st/2nd/3rd item [total, count]
        "any": defaultdict(lambda: [0, 0]),                      # completed item built at all -> [n, w]
        "skill": defaultdict(lambda: [0, 0]),                    # max order -> [n, w]
        "first3": Counter(),                                     # first three skill points
    }


def _top(d, k, key=lambda kv: -kv[1][0]):
    """The k most common options that have at least MIN_OPTION_GAMES games."""
    return [kv for kv in sorted(d.items(), key=key) if kv[1][0] >= MIN_OPTION_GAMES][:k]


def _summarize(a, general=None):
    """Turns one accumulator into the compact lists stored in builds_<group>.js."""
    n = a["n"]
    out = {"n": n, "w": a["w"]}
    if n < MIN_BUILD_GAMES:
        return out                     # not enough games: the site shows "not enough build data"
    out["start"] = [[list(k), v[0], v[1]] for k, v in _top(a["start"], 2)]
    out["boots"] = [[k, v[0], v[1], round(v[2] / v[0] / 60, 1)] for k, v in _top(a["boots"], 4)]
    out["path"] = [[list(k), v[0], v[1]] for k, v in _top(a["path"], 3)]
    out["slots"] = []
    for i in range(5):
        r = a["reached"][i]
        items = [[k, v[0], v[1]] for k, v in _top(a["slots"][i], 4) if r and v[0] / r >= 0.03]
        out["slots"].append({"r": r, "items": items})
    out["mins"] = [round(t / c / 60, 1) if c else None for t, c in a["secs"]]
    out["core"] = [[k, v[0], v[1]] for k, v in _top(a["any"], 6)]
    out["skill"] = [[k, v[0], v[1]] for k, v in _top(a["skill"], 2)]
    out["first3"] = [[k, c] for k, c in a["first3"].most_common(1)]
    if general is not None and general["n"]:
        # Situational: built much more often against this comp than in general.
        sit = []
        for item, (nb, wb) in a["any"].items():
            rate_b = nb / n
            rate_g = general["any"].get(item, [0, 0])[0] / general["n"]
            if nb >= 10 and rate_b >= 0.05 and rate_b >= 1.4 * rate_g:
                sit.append([item, round(rate_b, 3), round(rate_g, 3), nb, wb])
        out["sit"] = sorted(sit, key=lambda s: s[2] - s[1])[:6]   # biggest jump first
    return out


def export_builds(conn, patches, groups, out_dir):
    """
    Builds from match timelines, for every champion + role shown on the site, in each
    rank group, split by the enemy team's damage type:

      gen    - all games
      ad     - enemy team's expected damage is the most physical 25% of teams
      ap     - enemy team's expected damage is the most magical 25% of teams
      mixed  - the middle 50%

    "Expected damage" uses each enemy champion's average magic share (what you know
    in champion select), not the damage they ended up dealing in that game.

    Writes docs/builds_<group>.js and returns (meta for data.js, item names used).
    """
    items = {r[0]: {"name": r[1], "completed": r[2], "boots": r[3], "depth": r[4], "tags": json.loads(r[5] or "[]")}
             for r in conn.execute("SELECT item_id, name, is_completed, is_boots, depth, tags FROM items")}
    have_tl = conn.execute("SELECT name FROM sqlite_master WHERE name='timelines'").fetchone()
    if not items or not have_tl:
        print("builds: skipped (run `python Data_Gather.py items` and collect timelines first)")
        return {"available": False}, {}
    skip_start = {i for i, v in items.items() if "Trinket" in v["tags"]}
    pf = f"m.patch IN ({','.join('?' * len(patches))})"

    # Each champion's average share of magic damage (physical + magic only).
    magic_share = {cid: mg / (ph + mg) for cid, ph, mg in conn.execute(f"""
        SELECT p.champion_id, SUM(p.phys_dmg), SUM(p.magic_dmg)
        FROM participants p JOIN matches m ON m.match_id = p.match_id
        WHERE {GOOD} AND {pf} AND p.phys_dmg IS NOT NULL GROUP BY p.champion_id""", patches) if ph + mg > 0}

    # Expected magic share of every team in games that have a timeline.
    teams = defaultdict(list)
    for mid, team, cid in conn.execute(f"""
            SELECT p.match_id, p.team_id, p.champion_id FROM participants p
            JOIN matches m ON m.match_id = p.match_id JOIN timelines t ON t.match_id = p.match_id
            WHERE {GOOD} AND {pf} AND t.status = 'done'""", patches):
        teams[(mid, team)].append(magic_share.get(cid, 0.5))
    team_share = {k: sum(v) / len(v) for k, v in teams.items()}
    if not team_share:
        print("builds: no timelines for these patches yet")
        return {"available": False}, {}
    ordered = sorted(team_share.values())
    cut_ad = ordered[len(ordered) // 4]           # at or below: AD-heavy
    cut_ap = ordered[(3 * len(ordered)) // 4]     # at or above: AP-heavy

    def comp_type(share):
        return "ad" if share <= cut_ad else "ap" if share >= cut_ap else "mixed"

    tier_groups = {t: ["all"] + [g for g, v in TIER_GROUPS.items() if v["tiers"] and t in v["tiers"]]
                   for t in set(sum((v["tiers"] or [] for v in TIER_GROUPS.values()), []))}
    acc = defaultdict(_new_acc)                   # (group, role, champion, comp) -> accumulator
    timeline_games = Counter()

    rows = conn.execute(f"""
        SELECT b.match_id, b.purchases, b.skills, m.tier, p.role, p.champion_id, p.team_id, p.win
        FROM builds b
        JOIN participants p ON p.match_id = b.match_id AND p.puuid = b.puuid
        JOIN matches m ON m.match_id = b.match_id
        WHERE {GOOD} AND {pf} AND p.role IS NOT NULL""", patches)
    for mid, purchases, skills, tier, role, cid, team, win in rows:
        enemy = team_share.get((mid, 200 if team == 100 else 100))
        if enemy is None:
            continue
        purchases = json.loads(purchases or "[]")
        completed, seen, boots, start = [], set(), None, []
        for item, sec in purchases:
            info = items.get(item)
            if sec < STARTER_SECONDS and item not in skip_start and info:
                start.append(item)
            if not info:
                continue
            if info["completed"] and not info["boots"] and item not in seen:
                seen.add(item)
                completed.append((item, sec))
            elif info["boots"] and info["depth"] == 2 and boots is None:
                boots = (item, sec)
        order = skill_max_order(skills or "") if len(skills or "") >= 9 else None

        for group in tier_groups.get(tier, ["all"]):
            timeline_games[group] += 1
            for comp in ("gen", comp_type(enemy)):
                a = acc[(group, role, cid, comp)]
                a["n"] += 1
                a["w"] += win
                if start:
                    s = a["start"][tuple(sorted(start))]
                    s[0] += 1; s[1] += win
                if boots:
                    b = a["boots"][boots[0]]
                    b[0] += 1; b[1] += win; b[2] += boots[1]
                if len(completed) >= 3:
                    p = a["path"][tuple(i for i, _ in completed[:3])]
                    p[0] += 1; p[1] += win
                for i, (item, sec) in enumerate(completed[:5]):
                    a["reached"][i] += 1
                    sl = a["slots"][i][item]
                    sl[0] += 1; sl[1] += win
                    if i < 3:
                        a["secs"][i][0] += sec; a["secs"][i][1] += 1
                for item, _ in completed:
                    an = a["any"][item]
                    an[0] += 1; an[1] += win
                if order:
                    sk = a["skill"][order]
                    sk[0] += 1; sk[1] += win
                    a["first3"][skills[:3]] += 1

    used_items = set()
    for gkey, g in groups.items():
        out = {}
        for role, rdata in g["roles"].items():
            for c in rdata["combos"]:
                cid = c[0]
                gen = acc.get((gkey, role, cid, "gen"))
                if gen is None:
                    continue
                entry = {"gen": _summarize(gen)}
                for comp in ("ad", "ap", "mixed"):
                    a = acc.get((gkey, role, cid, comp))
                    entry[comp] = _summarize(a, gen) if a else {"n": 0, "w": 0}
                out.setdefault(role, {})[str(cid)] = entry
                for s in entry.values():
                    for k in ("boots", "slots", "core", "sit", "path", "start"):
                        for x in s.get(k, []):
                            if k == "slots":
                                used_items.update(i[0] for i in x["items"])
                            elif k in ("path", "start"):
                                used_items.update(x[0])
                            else:
                                used_items.add(x[0])
        path = os.path.join(out_dir, f"builds_{gkey}.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("window.BUILD_DATA = window.BUILD_DATA || {};\n")
            f.write(f"window.BUILD_DATA[{json.dumps(gkey)}] = ")
            json.dump(out, f, separators=(",", ":"))
            f.write(";\n")
        print(f"builds {g['label']:<20} {timeline_games[gkey]:>7} player-games with timelines -> "
              f"{path} ({os.path.getsize(path) // 1024} KB)")

    meta = {"available": True, "cut_ad": round(cut_ad, 3), "cut_ap": round(cut_ap, 3),
            "min_games": MIN_BUILD_GAMES, "games": {k: v // 10 for k, v in timeline_games.items()}}
    names = {str(i): items[i]["name"] for i in sorted(used_items) if i in items}
    return meta, names


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Export website data from league.db")
    ap.add_argument("--db", default="league.db")
    ap.add_argument("--out", default="docs/data.js")
    ap.add_argument("--patches", default=None, help="'16.21,16.22', 'all', or leave out for the latest 2")
    a = ap.parse_args()
    export(a.db, a.out, a.patches)
