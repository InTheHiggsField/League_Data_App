"""
Rate-limited client for the Riot Games API.

Riot enforces two kinds of limits, and both are counted separately for each
routing host (na1.api.riotgames.com and americas.api.riotgames.com each have
their own budget):

  * App limits    - for your API key as a whole   (dev key: 20 / 1s and 100 / 2min)
  * Method limits - for each endpoint separately   (e.g. match-v5 by-id)

Every response includes headers describing those limits, e.g.
    X-App-Rate-Limit:          20:1,100:120
    X-App-Rate-Limit-Count:    3:1,57:120
    X-Method-Rate-Limit:       2000:10
The client reads those headers, so switching to a personal or production key
needs no code changes. It also waits before a request whenever a window is
full, so a 429 should be rare. If one still happens, it waits for Retry-After
and tries again.
"""

import os
import time
from collections import deque

import requests

# Dev-key defaults, used until the first response gives the real numbers.
DEFAULT_APP_LIMITS = [(20, 1), (100, 120)]

PLATFORM_HOST = "https://{platform}.api.riotgames.com"   # na1, euw1, kr ...
REGIONAL_HOST = "https://{region}.api.riotgames.com"     # americas, europe, asia


class RiotAPIError(Exception):
    def __init__(self, status, url, body=""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


class RiotAuthError(RiotAPIError):
    """401/403: the key is missing, wrong or expired (dev keys expire every 24h)."""


def parse_limit_header(value):
    """'20:1,100:120' -> [(20, 1), (100, 120)]"""
    limits = []
    for part in value.split(","):
        part = part.strip()
        if ":" in part:
            count, seconds = part.split(":")
            limits.append((int(count), int(seconds)))
    return limits


class RateLimiter:
    """
    Sliding-window limiter that can enforce several windows at once.
    It keeps the time of every recent request. Before a new request it checks
    each window ("no more than N requests in the last S seconds") and sleeps
    until the oldest request in a full window has aged out.
    """

    def __init__(self, limits, safety=0.05):
        self.safety = safety      # small extra wait so clock drift doesn't push us over
        self.limits = []
        self.history = deque()    # monotonic timestamps of past requests
        self.set_limits(limits)

    def set_limits(self, limits):
        self.limits = sorted(limits, key=lambda x: x[1])
        if self.limits:
            longest = self.limits[-1][1]
            while len(self.history) > max(c for c, _ in self.limits):
                self.history.popleft()
            self._longest = longest
        else:
            self._longest = 0

    def wait_time(self, now=None):
        """How long to wait before the next request is allowed (0 if allowed now)."""
        now = time.monotonic() if now is None else now
        while self.history and now - self.history[0] > self._longest:
            self.history.popleft()
        wait = 0.0
        for count, seconds in self.limits:
            # requests that are still inside this window
            in_window = [t for t in self.history if now - t < seconds]
            if len(in_window) >= count:
                # oldest request that has to leave the window before we can go
                oldest = in_window[len(in_window) - count]
                wait = max(wait, oldest + seconds - now + self.safety)
        return wait

    def acquire(self):
        while True:
            wait = self.wait_time()
            if wait <= 0:
                self.history.append(time.monotonic())
                return
            if wait > 5:
                print(f"  [rate limit] waiting {wait:.0f}s")
            time.sleep(wait)

    def sync_counts(self, count_header):
        """
        Riot reports how many requests it has counted in each window. If Riot's
        count is higher than ours (e.g. the script was restarted 30s ago), add
        placeholder timestamps so we don't go over the limit.
        """
        now = time.monotonic()
        for count, seconds in parse_limit_header(count_header):
            ours = sum(1 for t in self.history if now - t < seconds)
            for _ in range(count - ours):
                self.history.append(now)
        self.history = deque(sorted(self.history))


def load_api_key(path="api_key.txt"):
    """Reads the key from the RIOT_API_KEY environment variable or api_key.txt (whitespace stripped)."""
    key = os.environ.get("RIOT_API_KEY")
    if not key and os.path.exists(path):
        with open(path) as f:
            key = f.read().strip()
    if not key:
        raise RiotAuthError(0, "", "No API key found in RIOT_API_KEY or api_key.txt")
    return key


class RiotClient:
    def __init__(self, api_key=None, platform="na1", region="americas",
                 max_retries=5, timeout=10):
        self.api_key = api_key or load_api_key()
        self.platform = platform
        self.region = region
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        # Sending the key in a header keeps it out of URLs and logs.
        self.session.headers["X-Riot-Token"] = self.api_key
        self.app_limiters = {}     # host -> RateLimiter
        self.method_limiters = {}  # (host, method) -> RateLimiter
        self.request_count = 0

    # ------------------------------------------------------------------ core
    def _limiters_for(self, host, method):
        if host not in self.app_limiters:
            self.app_limiters[host] = RateLimiter(DEFAULT_APP_LIMITS)
        key = (host, method)
        if key not in self.method_limiters:
            self.method_limiters[key] = RateLimiter([])  # filled in from headers
        return self.app_limiters[host], self.method_limiters[key]

    def _update_from_headers(self, app, meth, headers):
        if "X-App-Rate-Limit" in headers:
            new = parse_limit_header(headers["X-App-Rate-Limit"])
            if new != app.limits:
                app.set_limits(new)
        if "X-App-Rate-Limit-Count" in headers:
            app.sync_counts(headers["X-App-Rate-Limit-Count"])
        if "X-Method-Rate-Limit" in headers:
            new = parse_limit_header(headers["X-Method-Rate-Limit"])
            if new != meth.limits:
                meth.set_limits(new)
        if "X-Method-Rate-Limit-Count" in headers:
            meth.sync_counts(headers["X-Method-Rate-Limit-Count"])

    def get(self, host, path, method, params=None):
        """
        GET host+path while staying under both rate limits.
        Returns the parsed JSON, or None for a 404.
        Raises RiotAuthError on 401/403 and RiotAPIError on other errors once retries are used up.
        """
        url = host + path
        app, meth = self._limiters_for(host, method)
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            app.acquire()
            meth.acquire()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                print(f"  [network] {e}; retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            self.request_count += 1
            self._update_from_headers(app, meth, r.headers)

            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                raise RiotAuthError(r.status_code, url, r.text)
            if r.status_code == 429:
                # Retry-After is present when *we* went over. If it's missing,
                # Riot's backend service is throttling, so wait a bit.
                retry_after = float(r.headers.get("Retry-After", 5))
                limit_type = r.headers.get("X-Rate-Limit-Type", "service")
                print(f"  [429 {limit_type}] waiting {retry_after:.0f}s")
                time.sleep(retry_after)
                continue
            if r.status_code >= 500:
                print(f"  [HTTP {r.status_code}] retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise RiotAPIError(r.status_code, url, r.text)
        raise RiotAPIError(-1, url, "max retries exceeded")

    # ------------------------------------------------------------- endpoints
    def get_league(self, tier, queue="RANKED_SOLO_5x5"):
        """tier: 'challenger', 'grandmaster' or 'master'"""
        host = PLATFORM_HOST.format(platform=self.platform)
        path = f"/lol/league/v4/{tier}leagues/by-queue/{queue}"
        return self.get(host, path, method=f"league-{tier}")

    def get_league_entries(self, tier, division, page=1, queue="RANKED_SOLO_5x5"):
        """
        One page (~205 players) of a non-apex division, e.g. ('GOLD', 'II').
        tier: IRON BRONZE SILVER GOLD PLATINUM EMERALD DIAMOND; division: I II III IV.
        Returns [] once you go past the last page.
        """
        host = PLATFORM_HOST.format(platform=self.platform)
        path = f"/lol/league/v4/entries/{queue}/{tier.upper()}/{division.upper()}"
        return self.get(host, path, method="league-entries", params={"page": page}) or []

    def get_match_ids(self, puuid, start=0, count=100, queue=420, start_time=None):
        """queue=420 is ranked solo/duo (440 is flex). start_time is epoch seconds."""
        host = REGIONAL_HOST.format(region=self.region)
        path = f"/lol/match/v5/matches/by-puuid/{puuid.strip()}/ids"
        params = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        if start_time is not None:
            params["startTime"] = int(start_time)
        return self.get(host, path, method="match-ids", params=params) or []

    def get_match(self, match_id):
        host = REGIONAL_HOST.format(region=self.region)
        return self.get(host, f"/lol/match/v5/matches/{match_id}", method="match")

    def get_timeline(self, match_id):
        host = REGIONAL_HOST.format(region=self.region)
        return self.get(host, f"/lol/match/v5/matches/{match_id}/timeline", method="timeline")
