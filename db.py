"""
SQLite storage for the collected data (one file: league.db).

Tables (raw data, filled in by Data_Gather.py)
  players       - sampled ladder players: puuid, tier, division, LP, whether they're in the sample
  match_queue   - every match ID found, with the tier of the player it came from (seed_tier)
                  and status 'pending' / 'done' / 'missing' / 'stale'
  matches       - one row per downloaded match, including its tier
  participants  - one row per player per match (10 per match)
  bans          - one row per ban
  champions     - champion id -> display name (from Data Dragon), e.g. 62 -> 'Wukong'
  items         - every Summoner's Rift item from Data Dragon: cost, stat tags, what it builds
                  from/into, and whether it's a completed item or boots
  raw_matches   - zlib-compressed full match JSON, so new fields can be parsed later

Views (readable summaries; open these in DB Browser). Every view leaves out
remakes and only uses ranked solo/duo (queue 420). They update on their own as
new matches come in.
  v_games                    - one row per game: tier, patch, duration, winner, both teams' champions by role
  v_lane_results             - one row per lane per game: winning champion vs losing champion
  v_champion_stats           - champion + role: games, winrate %, pick %, ban %, KDA, CS/min ...
  v_champion_stats_by_tier   - same, split by tier
  v_matchup_stats            - champion vs lane opponent: games, winrate %
  v_matchup_stats_by_tier    - same, split by tier
  v_collection_progress      - how much has been collected for each tier
  v_champion_damage_profile  - champion + role: % physical / magic / true damage, healing, CC
"""

import json
import sqlite3
import time
import zlib

TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND",
              "MASTER", "GRANDMASTER", "CHALLENGER"]
APEX_TIERS = ["MASTER", "GRANDMASTER", "CHALLENGER"]
DIVISIONS = ["I", "II", "III", "IV"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid           TEXT PRIMARY KEY,
    tier            TEXT,
    division        TEXT,
    league_points   INTEGER,
    wins            INTEGER,
    losses          INTEGER,
    sampled         INTEGER DEFAULT 0,  -- 1 = match IDs get fetched for this player
    ids_fetched_at  INTEGER             -- epoch seconds; NULL = never
);

CREATE TABLE IF NOT EXISTS match_queue (
    match_id       TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'pending',
    added_at       INTEGER,
    seed_puuid     TEXT,                -- the player whose history this match came from
    seed_tier      TEXT,
    seed_division  TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    match_id       TEXT PRIMARY KEY,
    platform       TEXT,
    queue_id       INTEGER,
    game_version   TEXT,
    patch          TEXT,
    game_creation  INTEGER,             -- epoch ms
    game_duration  INTEGER,             -- seconds
    winning_team   INTEGER,             -- 100 (blue) or 200 (red)
    is_remake      INTEGER,
    tier           TEXT,                -- seed player's tier (matchmaking keeps lobbies close)
    division       TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    match_id         TEXT,
    puuid            TEXT,
    team_id          INTEGER,
    champion_id      INTEGER,
    champion_name    TEXT,
    champion_key     TEXT,
    role             TEXT,
    opponent_name    TEXT,
    opponent_key     TEXT,
    win              INTEGER,
    kills            INTEGER,
    deaths           INTEGER,
    assists          INTEGER,
    cs               INTEGER,
    gold             INTEGER,
    damage_to_champs INTEGER,
    damage_taken     INTEGER,
    vision_score     INTEGER,
    champ_level      INTEGER,
    items            TEXT,
    summoner1        INTEGER,
    summoner2        INTEGER,
    phys_dmg         INTEGER,        -- damage dealt to champions, by type
    magic_dmg        INTEGER,
    true_dmg         INTEGER,
    total_heal       INTEGER,
    cc_time          INTEGER,        -- seconds of crowd control applied to enemies
    PRIMARY KEY (match_id, puuid)
);

CREATE TABLE IF NOT EXISTS bans (
    match_id     TEXT,
    team_id      INTEGER,
    champion_id  INTEGER,
    pick_turn    INTEGER
);

CREATE TABLE IF NOT EXISTS champions (
    champion_id  INTEGER PRIMARY KEY,
    name         TEXT,                  -- display name, e.g. 'Wukong', 'Nunu & Willump'
    api_name     TEXT                   -- name used in match data, e.g. 'MonkeyKing'
);

CREATE TABLE IF NOT EXISTS items (
    item_id       INTEGER PRIMARY KEY,
    name          TEXT,
    gold_total    INTEGER,
    tags          TEXT,              -- JSON list, e.g. ["SpellDamage", "Armor"]
    builds_from   TEXT,              -- JSON list of item ids
    builds_into   TEXT,              -- JSON list of item ids
    depth         INTEGER,           -- 1 = basic, 2-3 = built from components
    is_completed  INTEGER,           -- 1 = finished item worth tracking in builds
    is_boots      INTEGER,           -- 1 = tier 2 or tier 3 boots
    patch         TEXT               -- Data Dragon version this came from
);

CREATE TABLE IF NOT EXISTS raw_matches (
    match_id  TEXT PRIMARY KEY,
    data      BLOB
);
"""

# Columns added after the first version, so existing league.db files get upgraded.
MIGRATIONS = {
    "players": [("division", "TEXT"), ("sampled", "INTEGER DEFAULT 0")],
    "match_queue": [("seed_puuid", "TEXT"), ("seed_tier", "TEXT"), ("seed_division", "TEXT")],
    "matches": [("tier", "TEXT"), ("division", "TEXT")],
    "participants": [("phys_dmg", "INTEGER"), ("magic_dmg", "INTEGER"), ("true_dmg", "INTEGER"),
                     ("total_heal", "INTEGER"), ("cc_time", "INTEGER")],
}

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_queue_status   ON match_queue(status, seed_tier);
CREATE INDEX IF NOT EXISTS idx_matches_patch  ON matches(patch);
CREATE INDEX IF NOT EXISTS idx_matches_tier   ON matches(tier);
CREATE INDEX IF NOT EXISTS idx_part_champ     ON participants(champion_key, role);
CREATE INDEX IF NOT EXISTS idx_part_opponent  ON participants(champion_key, opponent_key);
CREATE INDEX IF NOT EXISTS idx_part_match     ON participants(match_id, team_id, role);
CREATE INDEX IF NOT EXISTS idx_part_puuid     ON participants(puuid);
CREATE INDEX IF NOT EXISTS idx_bans_champ     ON bans(champion_id);
CREATE INDEX IF NOT EXISTS idx_bans_match     ON bans(match_id);
CREATE INDEX IF NOT EXISTS idx_players_sample ON players(sampled, tier);
"""

# --------------------------------------------------------------------- views
# Shared filter: real games (no remakes), ranked solo/duo only.
_GOOD = "m.is_remake = 0 AND m.queue_id = 420"
# Display name: Data Dragon name if the champions table is filled, otherwise the API name.
_NAME = "COALESCE(c.name, p.champion_name)"

_ROLES = ["top", "jungle", "mid", "bot", "support"]
_TIER_SORT = "CASE m.tier " + " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(TIER_ORDER)) + " END"
_team_cols = ",\n    ".join(
    f"MAX(CASE WHEN p.team_id = {t} AND p.role = '{r}' THEN {_NAME} END) AS {side}_{r}"
    for t, side in ((100, "blue"), (200, "red")) for r in _ROLES)

VIEWS = {
"v_games": f"""
SELECT m.match_id,
    datetime(m.game_creation / 1000, 'unixepoch') AS played_at,
    m.patch, m.tier, m.division,
    printf('%d:%02d', m.game_duration / 60, m.game_duration % 60) AS duration,
    CASE m.winning_team WHEN 100 THEN 'blue' WHEN 200 THEN 'red' END AS winner,
    {_team_cols}
FROM matches m
JOIN participants p ON p.match_id = m.match_id
LEFT JOIN champions c ON c.champion_id = p.champion_id
WHERE {_GOOD}
GROUP BY m.match_id
""",

# "Winner" = the champion whose team won the game. Lane outcome itself isn't in the match data.
"v_lane_results": f"""
SELECT m.match_id, m.patch, m.tier, b.role,
    CASE WHEN b.win = 1 THEN COALESCE(cb.name, b.champion_name) ELSE COALESCE(cr.name, r.champion_name) END AS winning_champion,
    CASE WHEN b.win = 1 THEN COALESCE(cr.name, r.champion_name) ELSE COALESCE(cb.name, b.champion_name) END AS losing_champion,
    CASE WHEN b.win = 1 THEN 'blue' ELSE 'red' END AS winning_side,
    CASE WHEN b.win = 1 THEN b.kills || '/' || b.deaths || '/' || b.assists
                        ELSE r.kills || '/' || r.deaths || '/' || r.assists END AS winner_kda,
    CASE WHEN b.win = 1 THEN r.kills || '/' || r.deaths || '/' || r.assists
                        ELSE b.kills || '/' || b.deaths || '/' || b.assists END AS loser_kda,
    CASE WHEN b.win = 1 THEN b.gold - r.gold ELSE r.gold - b.gold END AS winner_gold_lead,
    CASE WHEN b.win = 1 THEN b.cs - r.cs ELSE r.cs - b.cs END AS winner_cs_lead
FROM participants b
JOIN participants r ON r.match_id = b.match_id AND r.team_id = 200 AND r.role = b.role
JOIN matches m ON m.match_id = b.match_id
LEFT JOIN champions cb ON cb.champion_id = b.champion_id
LEFT JOIN champions cr ON cr.champion_id = r.champion_id
WHERE b.team_id = 100 AND b.role IS NOT NULL AND {_GOOD}
""",

"v_champion_stats": f"""
WITH total AS (SELECT COUNT(*) AS n FROM matches m WHERE {_GOOD}),
     banned AS (SELECT b.champion_id, COUNT(DISTINCT b.match_id) AS bans
                FROM bans b JOIN matches m ON m.match_id = b.match_id WHERE {_GOOD}
                GROUP BY b.champion_id)
SELECT {_NAME} AS champion, p.role,
    COUNT(*) AS games,
    SUM(p.win) AS wins,
    ROUND(100.0 * SUM(p.win) / COUNT(*), 1) AS winrate_pct,
    ROUND(100.0 * COUNT(*) / (SELECT n FROM total), 2) AS pick_pct,
    ROUND(100.0 * COALESCE(MAX(bn.bans), 0) / (SELECT n FROM total), 2) AS ban_pct_all_roles,
    ROUND(1.0 * (SUM(p.kills) + SUM(p.assists)) / MAX(SUM(p.deaths), 1), 2) AS kda,
    ROUND(AVG(p.kills), 1) AS avg_kills,
    ROUND(AVG(p.deaths), 1) AS avg_deaths,
    ROUND(AVG(p.assists), 1) AS avg_assists,
    ROUND(AVG(p.cs * 60.0 / m.game_duration), 1) AS cs_per_min,
    ROUND(AVG(p.gold * 60.0 / m.game_duration), 0) AS gold_per_min,
    ROUND(AVG(p.damage_to_champs * 60.0 / m.game_duration), 0) AS dmg_per_min,
    ROUND(AVG(p.vision_score), 1) AS vision
FROM participants p
JOIN matches m ON m.match_id = p.match_id
LEFT JOIN champions c ON c.champion_id = p.champion_id
LEFT JOIN banned bn ON bn.champion_id = p.champion_id
WHERE {_GOOD} AND p.role IS NOT NULL
GROUP BY p.champion_id, p.role
ORDER BY p.role, games DESC
""",

"v_champion_stats_by_tier": f"""
WITH total AS (SELECT m.tier, COUNT(*) AS n FROM matches m WHERE {_GOOD} GROUP BY m.tier)
SELECT m.tier, {_NAME} AS champion, p.role,
    COUNT(*) AS games,
    SUM(p.win) AS wins,
    ROUND(100.0 * SUM(p.win) / COUNT(*), 1) AS winrate_pct,
    ROUND(100.0 * COUNT(*) / MAX(t.n), 2) AS pick_pct,
    ROUND(1.0 * (SUM(p.kills) + SUM(p.assists)) / MAX(SUM(p.deaths), 1), 2) AS kda,
    ROUND(AVG(p.cs * 60.0 / m.game_duration), 1) AS cs_per_min
FROM participants p
JOIN matches m ON m.match_id = p.match_id
JOIN total t ON t.tier IS m.tier
LEFT JOIN champions c ON c.champion_id = p.champion_id
WHERE {_GOOD} AND p.role IS NOT NULL
GROUP BY m.tier, p.champion_id, p.role
ORDER BY {_TIER_SORT}, p.role, games DESC
""",

"v_matchup_stats": f"""
SELECT {_NAME} AS champion, p.role, COALESCE(co.name, p.opponent_name) AS opponent,
    COUNT(*) AS games,
    SUM(p.win) AS wins,
    ROUND(100.0 * SUM(p.win) / COUNT(*), 1) AS winrate_pct
FROM participants p
JOIN matches m ON m.match_id = p.match_id
JOIN participants o ON o.match_id = p.match_id AND o.team_id != p.team_id AND o.role = p.role
LEFT JOIN champions c  ON c.champion_id  = p.champion_id
LEFT JOIN champions co ON co.champion_id = o.champion_id
WHERE {_GOOD} AND p.role IS NOT NULL
GROUP BY p.champion_id, p.role, o.champion_id
ORDER BY champion, p.role, games DESC
""",

"v_matchup_stats_by_tier": f"""
SELECT m.tier, {_NAME} AS champion, p.role, COALESCE(co.name, p.opponent_name) AS opponent,
    COUNT(*) AS games,
    SUM(p.win) AS wins,
    ROUND(100.0 * SUM(p.win) / COUNT(*), 1) AS winrate_pct
FROM participants p
JOIN matches m ON m.match_id = p.match_id
JOIN participants o ON o.match_id = p.match_id AND o.team_id != p.team_id AND o.role = p.role
LEFT JOIN champions c  ON c.champion_id  = p.champion_id
LEFT JOIN champions co ON co.champion_id = o.champion_id
WHERE {_GOOD} AND p.role IS NOT NULL
GROUP BY m.tier, p.champion_id, p.role, o.champion_id
ORDER BY {_TIER_SORT}, champion, p.role, games DESC
""",

# Average damage split per champion + role. Uses damage dealt to champions only, so
# building/objective damage doesn't skew it. Rows not yet backfilled (NULL) are skipped.
"v_champion_damage_profile": f"""
SELECT {_NAME} AS champion, p.role,
    COUNT(*) AS games,
    ROUND(100.0 * SUM(p.phys_dmg)  / MAX(SUM(p.phys_dmg + p.magic_dmg + p.true_dmg), 1), 1) AS physical_pct,
    ROUND(100.0 * SUM(p.magic_dmg) / MAX(SUM(p.phys_dmg + p.magic_dmg + p.true_dmg), 1), 1) AS magic_pct,
    ROUND(100.0 * SUM(p.true_dmg)  / MAX(SUM(p.phys_dmg + p.magic_dmg + p.true_dmg), 1), 1) AS true_pct,
    ROUND(AVG((p.phys_dmg + p.magic_dmg + p.true_dmg) * 60.0 / m.game_duration), 0) AS dmg_per_min,
    ROUND(AVG(p.total_heal * 60.0 / m.game_duration), 0) AS heal_per_min,
    ROUND(AVG(p.cc_time * 60.0 / m.game_duration), 2) AS cc_sec_per_min
FROM participants p
JOIN matches m ON m.match_id = p.match_id
LEFT JOIN champions c ON c.champion_id = p.champion_id
WHERE {_GOOD} AND p.role IS NOT NULL AND p.phys_dmg IS NOT NULL
GROUP BY p.champion_id, p.role
ORDER BY champion, games DESC
""",

"v_collection_progress": """
SELECT t.tier,
    (SELECT COUNT(*) FROM players WHERE tier = t.tier AND sampled = 1) AS sampled_players,
    (SELECT COUNT(*) FROM players WHERE tier = t.tier AND sampled = 1 AND ids_fetched_at IS NOT NULL) AS players_done,
    (SELECT COUNT(*) FROM match_queue WHERE seed_tier = t.tier AND status = 'pending') AS matches_pending,
    (SELECT COUNT(*) FROM matches WHERE tier = t.tier AND is_remake = 0 AND queue_id = 420) AS solo_games_stored
FROM (SELECT DISTINCT tier FROM players WHERE tier IS NOT NULL
      UNION SELECT DISTINCT tier FROM matches WHERE tier IS NOT NULL) t
ORDER BY CASE t.tier """ + " ".join(f"WHEN '{tr}' THEN {i}" for i, tr in enumerate(TIER_ORDER)) + " END",
}


def _migrate(conn):
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ctype in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")


def create_views(conn):
    # Dropped and re-created every time, so view changes in this file always take effect.
    for name, sql in VIEWS.items():
        conn.execute(f"DROP VIEW IF EXISTS {name}")
        conn.execute(f"CREATE VIEW {name} AS {sql}")


def connect(path="league.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    create_views(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------- champions
def save_champions(conn, ddragon_data):
    """ddragon_data = the 'data' dict from Data Dragon's champion.json."""
    rows = [(int(v["key"]), v["name"], v["id"]) for v in ddragon_data.values()]
    conn.executemany("INSERT OR REPLACE INTO champions (champion_id, name, api_name) VALUES (?,?,?)", rows)
    conn.commit()
    return len(rows)


# -------------------------------------------------------------------- items
def save_items(conn, ddragon_items, version):
    """
    ddragon_items = the 'data' dict from Data Dragon's item.json.
    Keeps only items sold on Summoner's Rift (map 11) and labels them:
      is_boots     - tier 2/3 boots (Boots tag, built from basic Boots)
      is_completed - a finished item: nothing builds out of it, costs 2000+ gold, not a
                     consumable/trinket, and not a champion-specific item (e.g. Ornn upgrades)
    The raw cost, tags and recipe are stored too, so these labels can be refined later.
    """
    rows = []
    for key, it in ddragon_items.items():
        gold = it.get("gold", {})
        tags = it.get("tags", [])
        if not it.get("maps", {}).get("11") or not gold.get("purchasable") or it.get("inStore") is False:
            continue
        if int(key) >= 100000:   # 6-digit ids are copies for other game modes (e.g. 326617 = Moonstone Renewer)
            continue
        special = it.get("requiredChampion") or it.get("requiredAlly")
        is_boots = "Boots" in tags and (it.get("depth") or 1) >= 2
        is_completed = (not it.get("into") and gold.get("total", 0) >= 2000 and not special
                        and "Consumable" not in tags and "Trinket" not in tags)
        rows.append((int(key), it.get("name"), gold.get("total"), json.dumps(tags),
                     json.dumps([int(x) for x in it.get("from", [])]),
                     json.dumps([int(x) for x in it.get("into", [])]),
                     it.get("depth") or 1, int(is_completed), int(is_boots), version))
    conn.execute("DELETE FROM items")
    conn.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows), sum(r[7] for r in rows), sum(r[8] for r in rows)


def backfill_damage(conn, batch=1000):
    """
    Fills the damage columns for matches stored before they existed, by re-reading
    the compressed raw match JSON. No API calls. Safe to stop and re-run: it only
    touches matches that still have NULL damage values.
    """
    from parse import damage_fields
    todo = [r[0] for r in conn.execute(
        "SELECT DISTINCT match_id FROM participants WHERE phys_dmg IS NULL")]
    have_raw = {r[0] for r in conn.execute("SELECT match_id FROM raw_matches")}
    todo = [m for m in todo if m in have_raw]
    done = 0
    for i in range(0, len(todo), batch):
        rows = []
        for match_id in todo[i:i + batch]:
            raw = load_raw_match(conn, match_id)
            for p in raw["info"]["participants"]:
                f = damage_fields(p)
                rows.append((f["phys_dmg"], f["magic_dmg"], f["true_dmg"], f["total_heal"], f["cc_time"],
                             match_id, p.get("puuid")))
        conn.executemany("""UPDATE participants SET phys_dmg=?, magic_dmg=?, true_dmg=?, total_heal=?, cc_time=?
                            WHERE match_id=? AND puuid=?""", rows)
        conn.commit()
        done += len(todo[i:i + batch])
        print(f"  {done}/{len(todo)} matches")
    return done


# ---------------------------------------------------------------- players
def upsert_players(conn, entries, tier=None):
    """
    entries: list of league entries (from a ladder's 'entries' or a division page).
    Each entry has puuid, rank (division), leaguePoints, wins, losses, and sometimes tier.
    """
    rows = [(e["puuid"], e.get("tier") or tier, e.get("rank"), e.get("leaguePoints"),
             e.get("wins"), e.get("losses"))
            for e in entries if e.get("puuid")]
    conn.executemany("""
        INSERT INTO players (puuid, tier, division, league_points, wins, losses) VALUES (?,?,?,?,?,?)
        ON CONFLICT(puuid) DO UPDATE SET tier=excluded.tier, division=excluded.division,
            league_points=excluded.league_points, wins=excluded.wins, losses=excluded.losses
    """, rows)
    conn.commit()
    return len(rows)


def sample_players(conn, tier, division, n):
    """Marks up to n random players of this tier/division as sampled (keeps ones already sampled)."""
    have = conn.execute("SELECT COUNT(*) FROM players WHERE tier=? AND division IS ? AND sampled=1",
                        (tier, division)).fetchone()[0]
    need = n - have
    if need > 0:
        conn.execute("""UPDATE players SET sampled = 1 WHERE puuid IN (
                            SELECT puuid FROM players WHERE tier=? AND division IS ? AND sampled=0
                            ORDER BY RANDOM() LIMIT ?)""", (tier, division, need))
        conn.commit()
    return conn.execute("SELECT COUNT(*) FROM players WHERE tier=? AND division IS ? AND sampled=1",
                        (tier, division)).fetchone()[0]


def players_needing_ids(conn, older_than_seconds=None):
    """
    Sampled players whose match IDs haven't been fetched (or were fetched over
    older_than_seconds ago). Returns (puuid, tier, division) rows, alternating
    between tiers so a partial run still covers every tier.
    """
    cond = "ids_fetched_at IS NULL"
    params = []
    if older_than_seconds is not None:
        cond = "(ids_fetched_at IS NULL OR ids_fetched_at < ?)"
        params.append(int(time.time()) - older_than_seconds)
    q = f"""SELECT puuid, tier, division FROM (
                SELECT puuid, tier, division,
                       ROW_NUMBER() OVER (PARTITION BY tier ORDER BY RANDOM()) AS rn
                FROM players WHERE sampled = 1 AND {cond})
            ORDER BY rn, tier"""
    return [tuple(r) for r in conn.execute(q, params)]


def mark_ids_fetched(conn, puuid):
    conn.execute("UPDATE players SET ids_fetched_at=? WHERE puuid=?", (int(time.time()), puuid))


# ------------------------------------------------------------ match queue
def enqueue_match_ids(conn, match_ids, seed_puuid=None, seed_tier=None, seed_division=None):
    """Adds match IDs; ones already queued are ignored (the first seed player is kept)."""
    now = int(time.time())
    before = conn.total_changes
    conn.executemany("""INSERT OR IGNORE INTO match_queue
                        (match_id, added_at, seed_puuid, seed_tier, seed_division) VALUES (?,?,?,?,?)""",
                     [(m.strip(), now, seed_puuid, seed_tier, seed_division)
                      for m in match_ids if m and m.strip()])
    return conn.total_changes - before


def mark_unseeded_stale(conn):
    """Old imported IDs (no seed tier) -> 'stale' so they're skipped. Nothing is deleted."""
    n = conn.execute("UPDATE match_queue SET status='stale' WHERE status='pending' AND seed_tier IS NULL").rowcount
    conn.commit()
    return n

"""In our execution of Data_Gather.py matches function, we call on this to get the pending matchings from the match_queue table"""
def pending_matches(conn, limit=None):
    """
    Pending (match_id, seed_tier, seed_division), newest first within each tier and
    alternating between tiers, so every tier fills up at the same pace.
    """
    q = """SELECT match_id, seed_tier, seed_division FROM (
               SELECT match_id, seed_tier, seed_division,
                      ROW_NUMBER() OVER (PARTITION BY seed_tier ORDER BY match_id DESC) AS rn
               FROM match_queue WHERE status = 'pending')
           ORDER BY rn, seed_tier"""
    if limit:
        q += f" LIMIT {int(limit)}"
    return [tuple(r) for r in conn.execute(q)]


def set_match_status(conn, match_id, status):
    conn.execute("UPDATE match_queue SET status=? WHERE match_id=?", (status, match_id))


def queue_summary(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM match_queue GROUP BY status")}


def backfill_match_tiers(conn):
    """
    Matches with no tier (e.g. from the old imported IDs) get the most common
    tier among their participants that are in the players table.
    """
    rank = "CASE pl.tier " + " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(TIER_ORDER)) + " END"
    n = conn.execute(f"""
        UPDATE matches SET tier = (
            SELECT pl.tier FROM participants p JOIN players pl ON pl.puuid = p.puuid
            WHERE p.match_id = matches.match_id
            GROUP BY pl.tier ORDER BY COUNT(*) DESC, {rank} DESC LIMIT 1)
        WHERE tier IS NULL""").rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------- matches
def save_parsed_match(conn, parsed, raw=None, tier=None, division=None):
    """parsed is the dict returned by parse.parse_match()."""
    m = dict(parsed["match"], tier=tier, division=division)
    conn.execute("""INSERT OR REPLACE INTO matches
        (match_id, platform, queue_id, game_version, patch, game_creation, game_duration, winning_team,
         is_remake, tier, division)
        VALUES (:match_id,:platform,:queue_id,:game_version,:patch,:game_creation,:game_duration,:winning_team,
                :is_remake,:tier,:division)""", m)
    conn.execute("DELETE FROM participants WHERE match_id=?", (m["match_id"],))
    conn.executemany("""INSERT INTO participants
        (match_id, puuid, team_id, champion_id, champion_name, champion_key, role, opponent_name, opponent_key,
         win, kills, deaths, assists, cs, gold, damage_to_champs, damage_taken, vision_score, champ_level,
         items, summoner1, summoner2, phys_dmg, magic_dmg, true_dmg, total_heal, cc_time)
        VALUES (:match_id,:puuid,:team_id,:champion_id,:champion_name,:champion_key,:role,:opponent_name,:opponent_key,
                :win,:kills,:deaths,:assists,:cs,:gold,:damage_to_champs,:damage_taken,:vision_score,:champ_level,
                :items,:summoner1,:summoner2,:phys_dmg,:magic_dmg,:true_dmg,:total_heal,:cc_time)""",
        parsed["participants"])
    conn.execute("DELETE FROM bans WHERE match_id=?", (m["match_id"],))
    conn.executemany("INSERT INTO bans (match_id, team_id, champion_id, pick_turn) "
                     "VALUES (:match_id,:team_id,:champion_id,:pick_turn)", parsed["bans"])
    if raw is not None:
        conn.execute("INSERT OR REPLACE INTO raw_matches (match_id, data) VALUES (?, ?)",
                     (m["match_id"], zlib.compress(json.dumps(raw).encode())))


def load_raw_match(conn, match_id):
    row = conn.execute("SELECT data FROM raw_matches WHERE match_id=?", (match_id,)).fetchone()
    return json.loads(zlib.decompress(row[0])) if row else None
