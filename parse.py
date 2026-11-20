"""
Turns a Match-V5 match JSON (GET /lol/match/v5/matches/{matchId}) into flat rows.

The parts of the response we use:
  metadata.matchId
  info.gameVersion, info.queueId, info.gameDuration, info.gameCreation, info.platformId
  info.participants[]  -> puuid, championId, championName, teamId, teamPosition, win, kills, ...
  info.teams[]         -> teamId, win, bans[{championId, pickTurn}]
"""

import json
import re

# teamPosition values in the API -> the role names used in Champion.json
ROLE_MAP = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "bot", "UTILITY": "support"}

# The API's championName doesn't always match the in-game name.
# Keys and values are both normalised (see champion_key()).
NAME_ALIASES = {
    "wukong": "monkeyking",
    "nunuwillump": "nunu",
    "renataglasc": "renata",
}


def champion_key(name):
    """'Lee Sin', 'LeeSin', "Kai'Sa", 'Wukong' -> 'leesin', 'leesin', 'kaisa', 'monkeyking'"""
    if name is None:
        return None
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return NAME_ALIASES.get(key, key)


def patch_from_version(game_version):
    """'25.12.689.1234' -> '25.12'"""
    if not game_version:
        return None
    parts = game_version.split(".")
    return ".".join(parts[:2])


def is_remake(info):
    # A remake / early surrender shouldn't count as a real win or loss.
    if info.get("gameDuration", 0) < 300:
        return True
    return any(p.get("gameEndedInEarlySurrender") for p in info.get("participants", []))


def parse_match(match_json):
    """
    Returns {"match": {...}, "participants": [10 dicts], "bans": [...]},
    or None if the JSON isn't a usable match.
    """
    if not match_json or "info" not in match_json:
        return None
    info = match_json["info"]
    match_id = match_json["metadata"]["matchId"]
    parts = info.get("participants", [])
    if len(parts) != 10:
        return None

    winning_team = next((t["teamId"] for t in info.get("teams", []) if t.get("win")), None)
    match = {
        "match_id": match_id,
        "platform": info.get("platformId"),
        "queue_id": info.get("queueId"),
        "game_version": info.get("gameVersion"),
        "patch": patch_from_version(info.get("gameVersion")),
        "game_creation": info.get("gameCreation"),
        "game_duration": info.get("gameDuration"),
        "winning_team": winning_team,
        "is_remake": int(is_remake(info)),
    }

    # (team, role) -> champion, used to find each player's lane opponent
    by_team_role = {}
    for p in parts:
        role = ROLE_MAP.get(p.get("teamPosition"))
        if role:
            by_team_role[(p["teamId"], role)] = p.get("championName")

    rows = []
    for p in parts:
        role = ROLE_MAP.get(p.get("teamPosition"))
        enemy_team = 200 if p["teamId"] == 100 else 100
        opponent = by_team_role.get((enemy_team, role)) if role else None
        rows.append({
            "match_id": match_id,
            "puuid": p.get("puuid"),
            "team_id": p["teamId"],
            "champion_id": p.get("championId"),
            "champion_name": p.get("championName"),
            "champion_key": champion_key(p.get("championName")),
            "role": role,
            "opponent_name": opponent,
            "opponent_key": champion_key(opponent),
            "win": int(bool(p.get("win"))),
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
            "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
            "gold": p.get("goldEarned", 0),
            "damage_to_champs": p.get("totalDamageDealtToChampions", 0),
            "damage_taken": p.get("totalDamageTaken", 0),
            "vision_score": p.get("visionScore", 0),
            "champ_level": p.get("champLevel", 0),
            "items": json.dumps([p.get(f"item{i}", 0) for i in range(7)]),
            "summoner1": p.get("summoner1Id"),
            "summoner2": p.get("summoner2Id"),
        })

    bans = []
    for t in info.get("teams", []):
        for b in t.get("bans", []):
            if b.get("championId", -1) > 0:   # -1 = no ban
                bans.append({"match_id": match_id, "team_id": t["teamId"],
                             "champion_id": b["championId"], "pick_turn": b.get("pickTurn")})

    return {"match": match, "participants": rows, "bans": bans}
