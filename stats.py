"""
Winrates and other stats computed from league.db.

The API never gives winrates directly. Every number here is counted from the
participants table: each row is one player in one game, with win = 1 or 0.

    from stats import Stats
    s = Stats()                              # ranked solo queue, all patches
    s.winrate("Ahri")                        # overall
    s.winrate("Ahri", role="mid")
    s.matchup_winrate("Ahri", "Zed")         # Ahri vs Zed in the same lane
    s.winrate_vs_any("Ahri", "Malphite")     # Malphite anywhere on the enemy team
    s.synergy_winrate("Ahri", "Lee Sin")     # Lee Sin on Ahri's team
    s.matchups("Ahri", role="mid")           # DataFrame of every lane opponent
    s.champion_overview()                    # DataFrame: every champion/role
    s.champion_table("Champion_stats.json")  # same layout as Champion.json, filled in

    Stats(patch="25.12") or Stats(patch=["25.11", "25.12"]) limits everything to those patches.
    Stats(tier="GOLD"), Stats(tier="EMERALD+") (Emerald and above) or
    Stats(tier=["IRON", "BRONZE"]) limits everything to those tiers.

Each winrate result includes the game count and a 95% Wilson confidence
interval. 7 wins in 10 games is 70%, but the real value could be anywhere
from about 40% to 89%, so check `games` and `ci` before trusting a number.
"""

import json
import math

import pandas as pd

import db
from db import TIER_ORDER
from parse import champion_key

ROLES = ["top", "jungle", "mid", "bot", "support"]


def wilson_interval(wins, games, z=1.96):
    """95% confidence interval for a win rate. Behaves well for small samples, unlike wins/games ± error."""
    if games == 0:
        return (0.0, 0.0)
    p = wins / games
    denom = 1 + z * z / games
    centre = (p + z * z / (2 * games)) / denom
    half = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / denom
    return (round(centre - half, 4), round(centre + half, 4))


def _result(games, wins):
    games = games or 0
    wins = wins or 0
    return {
        "games": games,
        "wins": wins,
        "winrate": round(wins / games, 4) if games else None,
        "ci": wilson_interval(wins, games),
    }


class Stats:
    def __init__(self, db_path="league.db", queue_id=420, patch=None, tier=None):
        """
        queue_id: 420 = ranked solo/duo, 440 = flex, None = all queues
        patch:    None = all, '25.12', or a list of patches
        tier:     None = all, 'GOLD', 'EMERALD+' (that tier and above), or a list of tiers
        """
        self.conn = db.connect(db_path)
        self.queue_id = queue_id
        if isinstance(patch, str):
            patch = [patch]
        self.patches = patch
        self.tiers = self._parse_tiers(tier)

    @staticmethod
    def _parse_tiers(tier):
        if tier is None:
            return None
        if isinstance(tier, str):
            t = tier.upper().strip()
            if t.endswith("+"):
                return TIER_ORDER[TIER_ORDER.index(t[:-1]):]
            tier = [t]
        tiers = [t.upper() for t in tier]
        bad = [t for t in tiers if t not in TIER_ORDER]
        if bad:
            raise ValueError(f"unknown tier(s) {bad}; use {TIER_ORDER}")
        return tiers

    # -------------------------------------------------------------- helpers
    def _match_filter(self, alias="m"):
        """SQL conditions + params shared by every query: no remakes, right queue/patch."""
        sql = [f"{alias}.is_remake = 0"]
        params = []
        if self.queue_id is not None:
            sql.append(f"{alias}.queue_id = ?")
            params.append(self.queue_id)
        if self.patches:
            sql.append(f"{alias}.patch IN ({','.join('?' * len(self.patches))})")
            params.extend(self.patches)
        if self.tiers:
            sql.append(f"{alias}.tier IN ({','.join('?' * len(self.tiers))})")
            params.extend(self.tiers)
        return " AND ".join(sql), params

    def _one(self, sql, params):
        row = self.conn.execute(sql, params).fetchone()
        return _result(row[0], row[1])

    def total_matches(self):
        where, params = self._match_filter()
        return self.conn.execute(f"SELECT COUNT(*) FROM matches m WHERE {where}", params).fetchone()[0]

    def champion_id(self, champ):
        row = self.conn.execute("SELECT champion_id FROM participants WHERE champion_key=? LIMIT 1",
                                (champion_key(champ),)).fetchone()
        return row[0] if row else None

    # -------------------------------------------------------------- winrates
    def winrate(self, champ, role=None):
        """Overall winrate for a champion, optionally in one role."""
        where, params = self._match_filter()
        sql = f"""SELECT COUNT(*), SUM(p.win) FROM participants p JOIN matches m USING (match_id)
                  WHERE {where} AND p.champion_key = ?"""
        params.append(champion_key(champ))
        if role:
            sql += " AND p.role = ?"
            params.append(role)
        return self._one(sql, params)

    def matchup_winrate(self, champ, vs, role=None):
        """Winrate of `champ` when the enemy laner in the same role is `vs`."""
        where, params = self._match_filter()
        sql = f"""SELECT COUNT(*), SUM(p.win) FROM participants p JOIN matches m USING (match_id)
                  WHERE {where} AND p.champion_key = ? AND p.opponent_key = ?"""
        params += [champion_key(champ), champion_key(vs)]
        if role:
            sql += " AND p.role = ?"
            params.append(role)
        return self._one(sql, params)

    def winrate_vs_any(self, champ, vs, role=None):
        """Winrate of `champ` when `vs` is anywhere on the enemy team (any lane)."""
        where, params = self._match_filter()
        sql = f"""SELECT COUNT(*), SUM(p.win)
                  FROM participants p
                  JOIN matches m USING (match_id)
                  JOIN participants e ON e.match_id = p.match_id AND e.team_id != p.team_id
                  WHERE {where} AND p.champion_key = ? AND e.champion_key = ?"""
        params += [champion_key(champ), champion_key(vs)]
        if role:
            sql += " AND p.role = ?"
            params.append(role)
        return self._one(sql, params)

    def synergy_winrate(self, champ, ally, role=None):
        """Winrate of `champ` when `ally` is on the same team."""
        where, params = self._match_filter()
        sql = f"""SELECT COUNT(*), SUM(p.win)
                  FROM participants p
                  JOIN matches m USING (match_id)
                  JOIN participants a ON a.match_id = p.match_id AND a.team_id = p.team_id
                                     AND a.puuid != p.puuid
                  WHERE {where} AND p.champion_key = ? AND a.champion_key = ?"""
        params += [champion_key(champ), champion_key(ally)]
        if role:
            sql += " AND p.role = ?"
            params.append(role)
        return self._one(sql, params)

    def player_winrate(self, puuid, champ=None):
        """A single player's winrate, overall or on one champion."""
        where, params = self._match_filter()
        sql = f"""SELECT COUNT(*), SUM(p.win) FROM participants p JOIN matches m USING (match_id)
                  WHERE {where} AND p.puuid = ?"""
        params.append(puuid.strip())
        if champ:
            sql += " AND p.champion_key = ?"
            params.append(champion_key(champ))
        return self._one(sql, params)

    # ------------------------------------------------------ pick / ban rates
    def pick_rate(self, champ, role=None):
        """Share of matches where the champion was picked (by either team)."""
        total = self.total_matches()
        where, params = self._match_filter()
        sql = f"""SELECT COUNT(DISTINCT p.match_id) FROM participants p JOIN matches m USING (match_id)
                  WHERE {where} AND p.champion_key = ?"""
        params.append(champion_key(champ))
        if role:
            sql += " AND p.role = ?"
            params.append(role)
        n = self.conn.execute(sql, params).fetchone()[0]
        return round(n / total, 4) if total else None

    def ban_rate(self, champ):
        """Share of matches where the champion was banned (by either team)."""
        cid = self.champion_id(champ)
        total = self.total_matches()
        if cid is None or not total:
            return None
        where, params = self._match_filter()
        n = self.conn.execute(f"""SELECT COUNT(DISTINCT b.match_id) FROM bans b JOIN matches m USING (match_id)
                                  WHERE {where} AND b.champion_id = ?""", params + [cid]).fetchone()[0]
        return round(n / total, 4)

    # -------------------------------------------------------- tables (pandas)
    def matchups(self, champ, role=None, min_games=20):
        """Every lane opponent `champ` has faced, with winrate and CI, best matchups first."""
        where, params = self._match_filter()
        sql = f"""SELECT p.opponent_name AS opponent, p.role, COUNT(*) AS games, SUM(p.win) AS wins
                  FROM participants p JOIN matches m USING (match_id)
                  WHERE {where} AND p.champion_key = ? AND p.opponent_key IS NOT NULL"""
        params.append(champion_key(champ))
        if role:
            sql += " AND p.role = ?"
            params.append(role)
        sql += " GROUP BY p.opponent_name, p.role HAVING COUNT(*) >= ?"
        params.append(min_games)
        df = pd.read_sql_query(sql, self.conn, params=params)
        return self._add_rates(df)

    def champion_overview(self, min_games=50):
        """One row per champion+role: games, winrate, CI, pick rate, ban rate, averages."""
        where, params = self._match_filter()
        sql = f"""SELECT COALESCE(c.name, p.champion_name) AS champion, p.champion_id, p.role,
                         COUNT(*) AS games, SUM(p.win) AS wins,
                         COUNT(DISTINCT p.match_id) AS matches_picked,
                         AVG(p.kills) AS kills, AVG(p.deaths) AS deaths, AVG(p.assists) AS assists,
                         AVG(p.cs * 60.0 / m.game_duration) AS cs_per_min,
                         AVG(p.gold * 60.0 / m.game_duration) AS gold_per_min,
                         AVG(p.damage_to_champs * 60.0 / m.game_duration) AS dmg_per_min,
                         AVG(p.vision_score) AS vision
                  FROM participants p JOIN matches m USING (match_id)
                  LEFT JOIN champions c ON c.champion_id = p.champion_id
                  WHERE {where} AND p.role IS NOT NULL
                  GROUP BY p.champion_id, p.role
                  HAVING COUNT(*) >= ?"""
        df = pd.read_sql_query(sql, self.conn, params=params + [min_games])
        total = self.total_matches()
        bans = pd.read_sql_query(f"""SELECT b.champion_id, COUNT(DISTINCT b.match_id) AS bans
                                     FROM bans b JOIN matches m USING (match_id)
                                     WHERE {where} GROUP BY b.champion_id""", self.conn, params=params)
        df = df.merge(bans, on="champion_id", how="left").fillna({"bans": 0})
        df = self._add_rates(df)
        df["pick_rate"] = (df["matches_picked"] / total).round(4) if total else None
        df["ban_rate"] = (df["bans"] / total).round(4) if total else None
        df["kda"] = ((df["kills"] + df["assists"]) / df["deaths"].clip(lower=1)).round(2)
        for c in ["kills", "deaths", "assists", "cs_per_min", "gold_per_min", "dmg_per_min", "vision"]:
            df[c] = df[c].round(2)
        return df.sort_values(["role", "winrate"], ascending=[True, False]).reset_index(drop=True)

    @staticmethod
    def _add_rates(df):
        if df.empty:
            return df.assign(winrate=[], ci_low=[], ci_high=[])
        df["winrate"] = (df["wins"] / df["games"]).round(4)
        ci = [wilson_interval(w, g) for w, g in zip(df["wins"], df["games"])]
        df["ci_low"] = [c[0] for c in ci]
        df["ci_high"] = [c[1] for c in ci]
        return df.sort_values("winrate", ascending=False).reset_index(drop=True)

    def champion_table(self, path="Champion_stats.json", min_games=50, matchup_min_games=20,
                       margin=0.02, top_n=5):
        """
        Writes the Champion.json layout ("Ahri_mid": {...}) with real numbers.
        Counters     = lane opponents this champ beats  (winrate >= 50% + margin)
        Countered_By = lane opponents that beat it      (winrate <= 50% - margin)
        Only matchups with at least matchup_min_games games are used.
        """
        overview = self.champion_overview(min_games=min_games)
        table = {}
        for row in overview.itertuples():
            mu = self.matchups(row.champion, role=row.role, min_games=matchup_min_games)
            counters = mu[mu["winrate"] >= 0.5 + margin].head(top_n)
            countered = mu[mu["winrate"] <= 0.5 - margin].sort_values("winrate").head(top_n)
            table[f"{row.champion}_{row.role}"] = {
                "Winrate": row.winrate,
                "Ban Rate": row.ban_rate,
                "Pick Rate": row.pick_rate,
                "Matches": int(row.games),
                "Winrate_CI": [row.ci_low, row.ci_high],
                "Countered_By": [{"champion": r.opponent, "winrate": r.winrate, "games": int(r.games)}
                                 for r in countered.itertuples()],
                "Counters": [{"champion": r.opponent, "winrate": r.winrate, "games": int(r.games)}
                             for r in counters.itertuples()],
                "Role": row.role,
            }
        with open(path, "w") as f:
            json.dump(table, f, indent=4)
        return table
