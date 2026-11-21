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


def damage_fields(p):
    """
    Damage-profile fields for one participant (one entry of info.participants).
    Used for new matches and by db.backfill_damage() for matches already stored.
      phys_dmg / magic_dmg / true_dmg - damage dealt to champions, by type
      total_heal                      - healing done (relevant to anti-heal items)
      cc_time                         - seconds spent crowd-controlling enemies (relevant to tenacity)
    """
    return {
        "phys_dmg": p.get("physicalDamageDealtToChampions", 0),
        "magic_dmg": p.get("magicDamageDealtToChampions", 0),
        "true_dmg": p.get("trueDamageDealtToChampions", 0),
        "total_heal": p.get("totalHeal", 0),
        "cc_time": p.get("timeCCingOthers", 0),
    }


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
            **damage_fields(p),
        })

    bans = []
    for t in info.get("teams", []):
        for b in t.get("bans", []):
            if b.get("championId", -1) > 0:   # -1 = no ban
                bans.append({"match_id": match_id, "team_id": t["teamId"],
                             "champion_id": b["championId"], "pick_turn": b.get("pickTurn")})

    return {"match": match, "participants": rows, "bans": bans}


# ----------------------------------------------------------------- timelines
SKILL_KEYS = {1: "Q", 2: "W", 3: "E", 4: "R"}


def parse_timeline(timeline_json):
    """
    Turns a Match-V5 timeline (GET /lol/match/v5/matches/{matchId}/timeline) into
    one build record per player:

        {"puuid": ..., "purchases": [[item_id, seconds], ...],   # in order, undos removed
                       "sold":      [[item_id, seconds], ...],
                       "skills":    "QWEQQRQEQE..."}             # skill level-up order

    The timeline is a list of one-minute frames, each with a list of events. We use:
      ITEM_PURCHASED  {participantId, itemId, timestamp}
      ITEM_SOLD       {participantId, itemId, timestamp}
      ITEM_UNDO       {participantId, beforeId, afterId}  beforeId = purchase undone,
                                                          afterId  = sale undone
      SKILL_LEVEL_UP  {participantId, skillSlot 1-4, levelUpType}
    Returns None if the JSON isn't a usable timeline.
    """
    if not timeline_json or "info" not in timeline_json:
        return None
    info = timeline_json["info"]

    # participantId (1-10) -> puuid
    ids = {}
    for p in info.get("participants") or []:
        ids[p["participantId"]] = p.get("puuid")
    if not ids:
        for i, puuid in enumerate(timeline_json.get("metadata", {}).get("participants", []), 1):
            ids[i] = puuid
    if len(ids) != 10:
        return None

    builds = {pid: {"puuid": puuid, "purchases": [], "sold": [], "skills": ""} for pid, puuid in ids.items()}

    def remove_last(lst, item_id):
        for i in range(len(lst) - 1, -1, -1):
            if lst[i][0] == item_id:
                del lst[i]
                return

    for frame in info.get("frames", []):
        for e in frame.get("events", []):
            b = builds.get(e.get("participantId"))
            if b is None:
                continue
            t = e.get("type")
            sec = int(e.get("timestamp", 0) / 1000)
            if t == "ITEM_PURCHASED":
                b["purchases"].append([e.get("itemId"), sec])
            elif t == "ITEM_SOLD":
                b["sold"].append([e.get("itemId"), sec])
            elif t == "ITEM_UNDO":
                if e.get("beforeId"):            # undid a purchase
                    remove_last(b["purchases"], e["beforeId"])
                if e.get("afterId"):             # undid a sale
                    remove_last(b["sold"], e["afterId"])
            elif t == "SKILL_LEVEL_UP" and e.get("levelUpType", "NORMAL") == "NORMAL":
                b["skills"] += SKILL_KEYS.get(e.get("skillSlot"), "?")

    return list(builds.values())
