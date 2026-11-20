"""
Data collection pipeline. Every step can be stopped (Ctrl+C) and started again
without losing progress or wasting API calls.

    python Data_Gather.py players                 # 1. sample ~200 players from every division, Iron IV -> Challenger
    python Data_Gather.py mark-stale              #    (once) skip the old June match IDs that have no tier
    python Data_Gather.py ids                     # 2. last 14 days of solo-queue match IDs for each sampled player
    python Data_Gather.py matches --limit 3000    # 3. download + parse queued matches, taking turns across tiers
    python Data_Gather.py status                  # progress per tier

Options: players --per-division 200 | ids --days 14 | matches --limit N --no-raw

In PyCharm, put the step and options (e.g. `matches --limit 3000`) in the
Run Configuration's "Parameters" field.

Rate limits are handled by riot_client.RiotClient. Leave it to the client and
don't add sleeps here.
"""

import argparse
import json
import sys
import time

import requests

import db
from parse import parse_match
from riot_client import RiotClient, RiotAuthError

LEAGUE_FILES = {"CHALLENGER": "challengerleagues.json",
                "GRANDMASTER": "grandmasterleagues.json",
                "MASTER": "masterleagues.json"}
LOWER_TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
PAGE_SIZE = 205   # players per page from the league entries endpoint


# ----------------------------------------------------------------- champions
def update_champions(conn):
    """Champion id -> display name from Data Dragon (Riot's static data; no API key or rate limit)."""
    try:
        version = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10).json()[0]
        data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json",
                            timeout=10).json()["data"]
        print(f"champions: {db.save_champions(conn, data)} (Data Dragon {version})")
    except requests.RequestException as e:
        print(f"couldn't reach Data Dragon ({e}); views will use API champion names")


# ------------------------------------------------------------------ step 1
def collect_players(client, conn, per_division=200):
    """
    Iron IV through Diamond I: downloads enough pages to have per_division players,
    then marks a random per_division of them as 'sampled'.
    Master/GM/Challenger: downloads each ladder (and saves the JSON file as before),
    then samples per_division players from each.
    About 31 API calls in total.
    """
    update_champions(conn)

    for tier in LOWER_TIERS:
        for division in ["IV", "III", "II", "I"]:
            got, page = 0, 1
            while got < per_division:
                entries = client.get_league_entries(tier, division, page=page)
                if not entries:
                    break
                got += db.upsert_players(conn, entries, tier)
                if len(entries) < PAGE_SIZE:
                    break
                page += 1
            n = db.sample_players(conn, tier, division, per_division)
            print(f"{tier:<11} {division:<3} {got:>5} players downloaded, {n} sampled")

    for tier in db.APEX_TIERS:
        ladder = client.get_league(tier.lower())
        with open(LEAGUE_FILES[tier], "w") as f:
            json.dump(ladder, f, indent=4)
        got = db.upsert_players(conn, ladder.get("entries", []), tier)
        n = db.sample_players(conn, tier, "I", per_division)
        print(f"{tier:<11} {'I':<3} {got:>5} players downloaded, {n} sampled")


# ------------------------------------------------------------------ step 2
def query_match_ids(client, conn, days=14, count=100, refresh_after_days=None):
    """
    For each sampled player, gets up to `count` ranked solo matches from the last
    `days` days and queues them, labeled with the player's tier. Players are
    visited tier by tier in turn, so stopping early still gives every tier some data.
    """
    older = refresh_after_days * 86400 if refresh_after_days else None
    players = db.players_needing_ids(conn, older)
    start_time = int(time.time()) - days * 86400
    print(f"{len(players)} players to fetch match IDs for (matches since {days} days ago)")
    total = 0
    for i, (puuid, tier, division) in enumerate(players, 1):
        ids = client.get_match_ids(puuid, count=count, queue=420, start_time=start_time)
        total += db.enqueue_match_ids(conn, ids, puuid, tier, division)
        db.mark_ids_fetched(conn, puuid)
        conn.commit()
        if i % 50 == 0 or i == len(players):
            print(f"  {i}/{len(players)} players, {total} new matches queued")


def import_match_id_file(conn, path="matchID.txt"):
    """Loads an old matchID.txt into the queue (no tier). Mostly kept for reference."""
    with open(path) as f:
        added = db.enqueue_match_ids(conn, f)
    conn.commit()
    print(f"queued {added} new match IDs from {path}")


# ------------------------------------------------------------------ step 3
def fetch_matches(client, conn, limit=None, keep_raw=True, commit_every=20):
    """
    Downloads queued matches (taking turns across tiers), parses them, and stores
    the rows with the seed player's tier. keep_raw=True also saves the compressed
    full JSON (~5 KB per match) so new fields can be parsed later without the API.
    """
    pending = db.pending_matches(conn, limit)
    print(f"{len(pending)} matches to download")
    done = 0
    for match_id, tier, division in pending:
        raw = client.get_match(match_id)
        parsed = parse_match(raw) if raw else None
        if parsed is None:
            db.set_match_status(conn, match_id, "missing")
        else:
            db.save_parsed_match(conn, parsed, raw if keep_raw else None, tier, division)
            db.set_match_status(conn, match_id, "done")
        done += 1
        if done % commit_every == 0:
            conn.commit()
        if done % 100 == 0:
            print(f"  {done}/{len(pending)} matches ({client.request_count} requests this run)")
    conn.commit()
    filled = db.backfill_match_tiers(conn)
    print(f"finished {done} matches" + (f"; filled in tier for {filled} older matches" if filled else ""))


def status(conn):
    print(f"match queue: {db.queue_summary(conn)}")
    rows = conn.execute("SELECT * FROM v_collection_progress").fetchall()
    print(f"\n{'tier':<12}{'sampled':>9}{'ids done':>10}{'pending':>10}{'games':>8}")
    for r in rows:
        print(f"{r['tier']:<12}{r['sampled_players']:>9}{r['players_done']:>10}"
              f"{r['matches_pending']:>10}{r['solo_games_stored']:>8}")


# -------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="League data collection")
    ap.add_argument("step", choices=["players", "mark-stale", "ids", "matches", "status",
                                     "champions", "import-ids"])
    ap.add_argument("--per-division", type=int, default=200, help="players sampled per division (players step)")
    ap.add_argument("--days", type=int, default=14, help="only matches from the last N days (ids step)")
    ap.add_argument("--refresh-days", type=int, default=None,
                    help="ids step: also re-fetch players last fetched more than N days ago")
    ap.add_argument("--limit", type=int, default=None, help="max matches to download this run")
    ap.add_argument("--no-raw", action="store_true", help="don't keep compressed raw match JSON")
    ap.add_argument("--db", default="league.db")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    try:
        if args.step == "status":
            status(conn)
        elif args.step == "mark-stale":
            print(f"marked {db.mark_unseeded_stale(conn)} old match IDs as 'stale' (kept, but skipped)")
        elif args.step == "champions":
            update_champions(conn)
        elif args.step == "import-ids":
            import_match_id_file(conn)
        else:
            client = RiotClient()
            if args.step == "players":
                collect_players(client, conn, args.per_division)
            elif args.step == "ids":
                query_match_ids(client, conn, days=args.days, refresh_after_days=args.refresh_days)
            elif args.step == "matches":
                fetch_matches(client, conn, limit=args.limit, keep_raw=not args.no_raw)
    except RiotAuthError as e:
        print(f"API key rejected ({e.status}). Dev keys expire every 24h - "
              f"get a new one at developer.riotgames.com and put it in api_key.txt.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nstopped - progress saved, run the same step again to continue")
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    main()


# Notes
"""PUUIDs are encrypted per Riot developer project: they keep working when you regenerate your dev key,
but a different project (e.g. a separate production app) gets different PUUIDs.

Match tier = the tier of the player whose history the match came from. Looking up all 10 players'
ranks would cost 10x the API calls, and matchmaking keeps lobbies close in rank."""
