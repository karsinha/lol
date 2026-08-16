import requests
import sqlite3
import datetime
import json
import os
import time
import secrets
import hmac
import gzip
import hashlib

from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, jsonify, request, make_response, abort
from threading import Thread, Lock, Semaphore
from dotenv import load_dotenv


base_dir = os.path.dirname(os.path.abspath(__file__))

data_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", base_dir)


template_dir = os.path.join(base_dir, 'templates')
static_dir   = os.path.join(base_dir, 'static')
env_path     = os.path.join(base_dir, '.env')
db_path      = os.path.join(data_dir, 'soloq_history.db')
json_path    = os.path.join(base_dir, 'players.json')
leaderboard_cache_path = os.path.join(data_dir, 'leaderboard_cache.json')
load_dotenv(env_path)


app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder=static_dir
)


API_KEY = os.getenv("RIOT_API_KEY")


INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN")

if not INTERNAL_TOKEN:
    raise RuntimeError(
        "Falta INTERNAL_TOKEN en el archivo .env. "
        "Generá uno con: python3 -c \"import secrets; print(secrets.token_hex(32))\" "
        "y agregalo a tu .env como INTERNAL_TOKEN=<valor>"
    )

CACHE_TIMEOUT = 60
GAME_STATUS_CACHE_TIMEOUT = 60
CUTOFF_CACHE_TIMEOUT = 300


LEADERBOARD_REFRESH_INTERVAL = 90

MATCH_HISTORY_CACHE_TIMEOUT = 600


RIOT_BATCH_SIZE  = 5     
RIOT_BATCH_DELAY = 1.2    



EXECUTOR = ThreadPoolExecutor(max_workers=10)

RIOT_SEMAPHORE = Semaphore(5)

GAME_STATUS_LOCK = Lock()
CUTOFF_LOCK = Lock()
ACCOUNT_LOCK = Lock()

_players_cache_lock = Lock()

_rate_limit_lock = Lock()

_leaderboard_lock = Lock()

_match_history_lock = Lock()


GAME_STATUS_CACHE = {}

CUTOFF_CACHE = {}


ACCOUNT_CACHE = {}
ACCOUNT_CACHE_TIMEOUT = 24 * 60 * 60  

LEADERBOARD_CACHE = {
    "timestamp": 0,
    "data": [],
    "etag": ""
}

MATCH_HISTORY_CACHE = {}

_players_cache = {
    "data": [],
    "mtime": 0.0
}


_rate_limit_store = defaultdict(list)

RATE_LIMIT_MAX = 60
RATE_LIMIT_WINDOW = 60


session = requests.Session()

retries = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(['GET']),
    respect_retry_after_header=True,
    raise_on_status=False
)

session.mount(
    'https://',
    HTTPAdapter(max_retries=retries)
)


QUEUE_IDS = {
    420: "Ranked Solo",
    440: "Ranked Flex",
    450: "ARAM",
    1900: "URF",
    1700: "Arena",
    490: "Quickplay"
}

TIER_WEIGHTS = {
    "IRON": 0,
    "BRONZE": 400,
    "SILVER": 800,
    "GOLD": 1200,
    "PLATINUM": 1600,
    "EMERALD": 2000,
    "DIAMOND": 2400,
    "MASTER": 2800,
    "GRANDMASTER": 2800,
    "CHALLENGER": 2800,
    "UNRANKED": -999999
}

DIVISION_WEIGHTS = {
    "IV": 0,
    "III": 100,
    "II": 200,
    "I": 300,
    "": 0
}

REGION_NAMES = {
    'br1': 'BR',
    'euw1': 'EUW',
    'eun1': 'EUN',
    'jp1': 'JP',
    'kr': 'KR',
    'la1': 'LAN',
    'la2': 'LAS',
    'na1': 'NA',
    'oc1': 'OCE',
    'ph2': 'PH',
    'ru': 'RU',
    'sg2': 'SG',
    'th2': 'TH',
    'tr1': 'TR',
    'tw2': 'TW',
    'vn2': 'VN'
}

REGION_CONFIG = {
    'kr':   {'challenger': 300, 'grandmaster': 700},
    'euw1': {'challenger': 300, 'grandmaster': 700},
    'na1':  {'challenger': 300, 'grandmaster': 700},
    'br1':  {'challenger': 200, 'grandmaster': 500},
    'la1':  {'challenger': 200, 'grandmaster': 500},
    'la2':  {'challenger': 200, 'grandmaster': 500},
    'eun1': {'challenger': 200, 'grandmaster': 500},
    'tr1':  {'challenger': 200, 'grandmaster': 500},
    'oc1':  {'challenger': 50,  'grandmaster': 100},
    'jp1':  {'challenger': 50,  'grandmaster': 100},
    'ru':   {'challenger': 50,  'grandmaster': 100},
    'ph2':  {'challenger': 50,  'grandmaster': 100},
    'sg2':  {'challenger': 50,  'grandmaster': 100},
    'th2':  {'challenger': 50,  'grandmaster': 100},
    'tw2':  {'challenger': 50,  'grandmaster': 100},
    'vn2':  {'challenger': 50,  'grandmaster': 100}
}

OPGG_REGION_SLUGS = {
    'na1': 'na', 'euw1': 'euw', 'eun1': 'eune', 'kr': 'kr', 'jp1': 'jp',
    'la1': 'lan', 'la2': 'las', 'br1': 'br', 'oc1': 'oce', 'tr1': 'tr',
    'ru': 'ru', 'ph2': 'ph', 'sg2': 'sg', 'th2': 'th', 'tw2': 'tw', 'vn2': 'vn'
}

MATCH_ROUTING = {
    'na1': 'americas', 'br1': 'americas', 'la1': 'americas', 'la2': 'americas',
    'euw1': 'europe', 'eun1': 'europe', 'tr1': 'europe', 'ru': 'europe',
    'kr': 'asia', 'jp1': 'asia',
    'oc1': 'sea', 'ph2': 'sea', 'sg2': 'sea', 'th2': 'sea', 'tw2': 'sea', 'vn2': 'sea'
}


def get_db():
    return sqlite3.connect(db_path, timeout=10)


def cleanup_cache(cache, timeout, lock):
    now = time.time()

    with lock:
        keys_to_delete = [
            k for k, v in cache.items()
            if now - v['timestamp'] > timeout
        ]

        for k in keys_to_delete:
            del cache[k]


def calculate_sort_score(tier, rank, lp):
    t = tier.upper()
    r = rank.upper()

    score = TIER_WEIGHTS.get(t, 0)

    if t not in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
        score += DIVISION_WEIGHTS.get(r, 0)

    score += lp

    return score


def _client_ip():

    xff = request.headers.get('X-Forwarded-For', '')

    if xff:
        parts = [p.strip() for p in xff.split(',') if p.strip()]
        if parts:
            return parts[-1]

    return request.remote_addr or 'unknown'


def _is_rate_limited(ip: str):
    now = time.time()

    with _rate_limit_lock:
        calls = _rate_limit_store[ip]

        calls = [
            t for t in calls
            if now - t < RATE_LIMIT_WINDOW
        ]

        if len(calls) >= RATE_LIMIT_MAX:
            _rate_limit_store[ip] = calls
            return True

        calls.append(now)
        _rate_limit_store[ip] = calls

        return False


def _prune_rate_limit_store():

    now = time.time()

    with _rate_limit_lock:
        empty_keys = []

        for ip, calls in _rate_limit_store.items():
            fresh = [t for t in calls if now - t < RATE_LIMIT_WINDOW]

            if fresh:
                _rate_limit_store[ip] = fresh
            else:
                empty_keys.append(ip)

        for ip in empty_keys:
            del _rate_limit_store[ip]

        if empty_keys:
            print(f"[rate limit] podadas {len(empty_keys)} IPs inactivas")


def _check_token():
    token = request.headers.get('X-Internal-Token', '')

    if not hmac.compare_digest(token, INTERNAL_TOKEN):
        abort(403)


def _check_rate_limit():
    ip = _client_ip()

    if _is_rate_limited(ip):
        abort(429)


def _run_in_paced_batches(items, fn, batch_size=RIOT_BATCH_SIZE, delay=RIOT_BATCH_DELAY):
    
    results = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        results.extend(EXECUTOR.map(fn, batch))
        if i + batch_size < len(items):
            time.sleep(delay)
    return results


def init_db():

    try:
        with get_db() as conn:

            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS lp_history (
                    puuid TEXT NOT NULL,
                    sort_value INTEGER NOT NULL,
                    tier TEXT,
                    rank TEXT,
                    lp INTEGER,
                    timestamp TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_lp_puuid_ts
                ON lp_history(puuid, timestamp DESC)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS match_cache (
                    puuid TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    champion TEXT,
                    win INTEGER,
                    kills INTEGER,
                    deaths INTEGER,
                    assists INTEGER,
                    cs INTEGER,
                    kill_participation INTEGER,
                    duration_min INTEGER,
                    queue_id INTEGER,
                    game_creation INTEGER,
                    PRIMARY KEY (puuid, match_id)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_match_puuid_creation
                ON match_cache(puuid, game_creation DESC)
            """)

            _ensure_match_cache_columns(conn)

    except Exception as e:
        print(f"DB Init Error: {e}")


def load_players_from_json():
    try:
        if not os.path.exists(json_path):
            return []

        mtime = os.path.getmtime(json_path)

        with _players_cache_lock:

            if mtime != _players_cache["mtime"]:

                with open(json_path, "r", encoding="utf-8") as f:
                    _players_cache["data"] = json.load(f)
                    _players_cache["mtime"] = mtime

            return list(_players_cache["data"])

    except json.JSONDecodeError:
        print("players.json invalid JSON")
        return []

    except Exception as e:
        print(f"Error loading players: {e}")
        return []


def save_lp_snapshot(puuid, sort_value, tier, rank, lp):

    try:
        now = datetime.datetime.now()

        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as conn:

            cursor = conn.cursor()

            cursor.execute("""
                SELECT timestamp, sort_value
                FROM lp_history
                WHERE puuid = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (puuid,))

            last = cursor.fetchone()

            should_save = True

            if last:
                last_time_str, last_val = last

                last_date = datetime.datetime.strptime(
                    last_time_str,
                    "%Y-%m-%d %H:%M:%S"
                )

                diff_seconds = (
                    now - last_date
                ).total_seconds()

                if diff_seconds < 1800 and last_val == sort_value:
                    should_save = False

            if should_save:

                cursor.execute("""
                    INSERT INTO lp_history (puuid, sort_value, tier, rank, lp, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    puuid,
                    sort_value,
                    tier,
                    rank,
                    lp,
                    now_str
                ))

                old_date = (
                    now - datetime.timedelta(days=30)
                ).strftime("%Y-%m-%d %H:%M:%S")

                cursor.execute("""
                    DELETE FROM lp_history
                    WHERE puuid = ?
                    AND timestamp < ?
                """, (
                    puuid,
                    old_date
                ))

                conn.commit()

    except Exception as e:
        print(f"save_lp_snapshot error: {e}")


def get_lp_change(puuid, current_sort_value, current_tier,current_rank, current_lp, hours=24):
    
    try:
        with get_db() as conn:

            cursor = conn.cursor()

            limit_time = (
                datetime.datetime.now()
                - datetime.timedelta(hours=hours)
            )

            limit_str = limit_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            cursor.execute("""
                SELECT sort_value, tier, rank, lp
                FROM lp_history
                WHERE puuid = ?
                AND timestamp <= ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (
                puuid,
                limit_str
            ))

            row = cursor.fetchone()

            if not row:
                return None

            old_sort_value, old_tier, old_rank, old_lp = row

            if old_tier == current_tier and old_rank == current_rank:
                return current_lp - old_lp

            return current_sort_value - old_sort_value

    except Exception as e:
        print(f"get_lp_change error: {e}")
        return None


def get_elo_history(puuid, days=21, max_points=150):

    try:
        with get_db() as conn:

            cursor = conn.cursor()

            limit_time = (
                datetime.datetime.now()
                - datetime.timedelta(days=days)
            )

            limit_str = limit_time.strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("""
                SELECT sort_value, tier, rank, lp, timestamp
                FROM lp_history
                WHERE puuid = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (puuid, limit_str))

            rows = cursor.fetchall()

        points = [
            {"v": r[0], "tier": r[1], "rank": r[2], "lp": r[3], "t": r[4]}
            for r in rows
        ]

        n = len(points)

        if n <= max_points:
            return points

        step = n / max_points

        return [points[int(i * step)] for i in range(max_points)]

    except Exception as e:
        print(f"get_elo_history error: {e}")
        return []


def estimate_match_lp_change(puuid, game_creation_ms, duration_min):
    
    try:
        start_dt = datetime.datetime.fromtimestamp(game_creation_ms / 1000)
        end_dt = start_dt + datetime.timedelta(minutes=(duration_min or 0) + 2)
        window_end = end_dt + datetime.timedelta(hours=3)

        start_str  = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str    = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        window_str = window_end.strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT sort_value FROM lp_history
                WHERE puuid = ? AND timestamp <= ?
                ORDER BY timestamp DESC LIMIT 1
            """, (puuid, start_str))
            before = cursor.fetchone()

            cursor.execute("""
                SELECT sort_value FROM lp_history
                WHERE puuid = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC LIMIT 1
            """, (puuid, end_str, window_str))
            after = cursor.fetchone()

        if before and after:
            return after[0] - before[0]

        return None

    except Exception as e:
        print(f"estimate_match_lp_change error: {e}")
        return None


def _fetch_single_match_detail(match_id, puuid, continent, headers):
    """Trae y procesa el detalle de UNA partida. Separado en su propia
    función para poder correrlo en paralelo (en tandas espaciadas, ver
    fetch_match_history) con el ThreadPoolExecutor."""
    try:
        mr = riot_get(
            f"https://{continent}.api.riotgames.com/lol/match/v5/matches/{match_id}",
            headers,
            timeout=4
        )

        if mr.status_code != 200:
            print(f"[match detail] {match_id} -> {mr.status_code}")
            return None

        match_data = mr.json()
        info = match_data.get('info', {})
        participants = info.get('participants', [])

        me = next(
            (p for p in participants if p.get('puuid') == puuid),
            None
        )

        if not me:
            return None

        team_id = me.get('teamId')

        team_kills = sum(
            p.get('kills', 0)
            for p in participants
            if p.get('teamId') == team_id
        ) or 1

        kp = int(
            ((me.get('kills', 0) + me.get('assists', 0)) / team_kills) * 100
        )

        cs = (
            me.get('totalMinionsKilled', 0)
            + me.get('neutralMinionsKilled', 0)
        )

        duration_min = int(info.get('gameDuration', 0) / 60)
        queue_id = info.get('queueId')
        game_creation = info.get('gameCreation', 0)
        champion = me.get('championName', '?')
        win = bool(me.get('win', False))
        kills = me.get('kills', 0)
        deaths = me.get('deaths', 0)
        assists = me.get('assists', 0)

        items = [me.get(f'item{i}', 0) or 0 for i in range(7)]
        spell1 = me.get('summoner1Id', 0)
        spell2 = me.get('summoner2Id', 0)

        perk_primary = None
        perk_sub_style = None
        styles = me.get('perks', {}).get('styles', [])
        if styles:
            primary_selections = styles[0].get('selections', [])
            if primary_selections:
                perk_primary = primary_selections[0].get('perk')
            if len(styles) > 1:
                perk_sub_style = styles[1].get('style')

        lp_change = estimate_match_lp_change(puuid, game_creation, duration_min)

        return {
            "match_id": match_id,
            "champion": champion,
            "win": win,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "cs": cs,
            "kp": kp,
            "duration_min": duration_min,
            "queue": QUEUE_IDS.get(queue_id, "Otro"),
            "queue_id": queue_id,
            "game_creation": game_creation,
            "items": items,
            "spell1": spell1,
            "spell2": spell2,
            "rune_primary": perk_primary,
            "rune_sub": perk_sub_style,
            "lp_change": lp_change,
        }

    except Exception as e:
        print(f"match detail fetch error {match_id}: {e}")
        return None


def fetch_match_history(puuid, region, count=7):
    """Trae las últimas `count` partidas. Las que ya están en cache local
    se leen directo de la DB; las que faltan se piden a Riot en tandas
    espaciadas (antes se disparaban todas juntas con EXECUTOR.map, lo que
    sumaba una ráfaga extra de hasta ~7 pedidos por encima de lo que ya
    esté haciendo el refresh del leaderboard en simultáneo)."""

    if not API_KEY:
        return []

    continent = MATCH_ROUTING.get(region, 'americas')
    headers = {"X-Riot-Token": API_KEY}

    try:
        r = riot_get(
            f"https://{continent}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}",
            headers,
            timeout=4
        )

        if r.status_code != 200:
            print(f"[match ids] {puuid} -> {r.status_code}")
            return []

        match_ids = r.json()

    except Exception as e:
        print(f"match ids fetch error {puuid}: {e}")
        return []

    results = []
    missing_ids = []

    with get_db() as conn:
        cursor = conn.cursor()

        for match_id in match_ids:
            cursor.execute("""
                SELECT champion, win, kills, deaths, assists, cs,
                       kill_participation, duration_min, queue_id, game_creation,
                       item0, item1, item2, item3, item4, item5, item6,
                       spell1, spell2, perk_primary, perk_sub_style, lp_change
                FROM match_cache
                WHERE puuid = ? AND match_id = ?
            """, (puuid, match_id))

            cached = cursor.fetchone()

            if cached:
                results.append({
                    "match_id": match_id,
                    "champion": cached[0],
                    "win": bool(cached[1]),
                    "kills": cached[2],
                    "deaths": cached[3],
                    "assists": cached[4],
                    "cs": cached[5],
                    "kp": cached[6],
                    "duration_min": cached[7],
                    "queue": QUEUE_IDS.get(cached[8], "Otro"),
                    "queue_id": cached[8],
                    "game_creation": cached[9],
                    "items": [cached[10], cached[11], cached[12], cached[13], cached[14], cached[15], cached[16]],
                    "spell1": cached[17],
                    "spell2": cached[18],
                    "rune_primary": cached[19],
                    "rune_sub": cached[20],
                    "lp_change": cached[21],
                })
            else:
                missing_ids.append(match_id)

    if missing_ids:
        # FIX (rate limit): antes esto era EXECUTOR.map directo sobre TODOS
        # los missing_ids -> podían ser hasta ~7 pedidos disparados juntos,
        # sumándose a lo que ya esté haciendo el refresh del leaderboard en
        # paralelo. Ahora usa el mismo pacing que el resto de los pedidos
        # masivos a Riot (tandas de RIOT_BATCH_SIZE con pausa entre ellas).
        fetched = _run_in_paced_batches(
            missing_ids,
            lambda mid: _fetch_single_match_detail(mid, puuid, continent, headers)
        )

        new_rows = [f for f in fetched if f is not None]

        if new_rows:
            with get_db() as conn:
                cursor = conn.cursor()

                for f in new_rows:
                    items = f["items"]
                    cursor.execute("""
                        INSERT OR REPLACE INTO match_cache
                        (puuid, match_id, champion, win, kills, deaths, assists,
                         cs, kill_participation, duration_min, queue_id, game_creation,
                         item0, item1, item2, item3, item4, item5, item6,
                         spell1, spell2, perk_primary, perk_sub_style, lp_change)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        puuid, f["match_id"], f["champion"], int(f["win"]),
                        f["kills"], f["deaths"], f["assists"], f["cs"], f["kp"],
                        f["duration_min"], f["queue_id"], f["game_creation"],
                        items[0], items[1], items[2], items[3], items[4], items[5], items[6],
                        f["spell1"], f["spell2"], f["rune_primary"], f["rune_sub"], f["lp_change"]
                    ))

                conn.commit()

        results.extend(new_rows)

    results.sort(key=lambda x: x['game_creation'], reverse=True)

    return results


def riot_get(url, headers, timeout=3):

    with RIOT_SEMAPHORE:
        return session.get(
            url,
            headers=headers,
            timeout=timeout
        )

def get_cached_account_info(puuid, region, headers, fallback_name, fallback_tag):

    now = time.time()

    with ACCOUNT_LOCK:
        if puuid in ACCOUNT_CACHE:
            cache_entry = ACCOUNT_CACHE[puuid]
            if (now - cache_entry['timestamp']) < ACCOUNT_CACHE_TIMEOUT:
                return cache_entry['data']

    continent = MATCH_ROUTING.get(region, 'americas')

    account_info = {
        "game_name": fallback_name,
        "tag_line": fallback_tag
    }

    try:
        r = riot_get(
            f"https://{continent}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}",
            headers,
            timeout=3
        )

        if r.status_code == 200:
            d = r.json()
            game_name = d.get('gameName')
            tag_line = d.get('tagLine')

            if game_name and tag_line:
                account_info = {
                    "game_name": game_name,
                    "tag_line": tag_line
                }
        else:
            print(f"[account] {puuid} -> {r.status_code}")

    except Exception as e:
        print(f"account fetch error {puuid}: {e}")

    with ACCOUNT_LOCK:
        ACCOUNT_CACHE[puuid] = {
            'timestamp': now,
            'data': account_info
        }

    return account_info


def get_cached_game_status(puuid, region, headers):

    now = time.time()

    cache_key = f"{puuid}_game"

    with GAME_STATUS_LOCK:

        if cache_key in GAME_STATUS_CACHE:

            cache_entry = GAME_STATUS_CACHE[cache_key]

            if (
                now - cache_entry['timestamp']
            ) < GAME_STATUS_CACHE_TIMEOUT:

                return cache_entry['data']

    game_status = {
        "is_playing": False
    }

    try:

        r = riot_get(
            f"https://{region}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}",
            headers,
            timeout=2.5
        )

        if r.status_code == 200:

            d = r.json()

            game_length_seconds = d.get('gameLength', 0)

            game_start_epoch_ms = int((now - game_length_seconds) * 1000)

            game_status = {
                "is_playing": True,
                "mode": QUEUE_IDS.get(
                    d.get('gameQueueConfigId', 0),
                    "In-Game"
                ),
                "game_start_epoch_ms": game_start_epoch_ms
            }

    except Exception:
        pass

    with GAME_STATUS_LOCK:
        GAME_STATUS_CACHE[cache_key] = {
            'timestamp': now,
            'data': game_status
        }

    return game_status



def fetch_core_stats(player_obj):
    """PASADA PRIORITARIA del refresh: cuenta + LP/tier/rank/W-L. Esto es
    lo que arma el leaderboard y el orden -- lo que la gente realmente
    mira. Deliberadamente NO pide el estado in-game acá: eso es
    attach_game_status, la pasada secundaria de menor prioridad, que
    corre después y no bloquea que el leaderboard ya esté publicado."""

    display_name = player_obj.get('name', 'Unknown')
    tag_line     = player_obj.get('tag', '')
    region       = player_obj.get('region', 'euw1')
    puuid        = player_obj.get('puuid')
    manual_url   = player_obj.get('manual_url')

    if not API_KEY or not puuid:
        return None

    headers = {
        "X-Riot-Token": API_KEY
    }

    try:

        final_icon = (
            f"/static/img/avatars/{player_obj['custom_image']}"
            if player_obj.get('custom_image')
            else "/static/img/avatars/default.png"
        )

        account_info = get_cached_account_info(
            puuid, region, headers, display_name, tag_line
        )
        display_name = account_info['game_name']
        tag_line     = account_info['tag_line']


        leagues = []

        try:

            r = riot_get(
                f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}",
                headers
            )

            if r.status_code == 200:
                leagues = r.json()

            elif r.status_code in (401, 403):
                # FIX: antes esto caía en el mismo log genérico que
                # cualquier otro error. Una key vencida/inválida hace que
                # TODO el leaderboard se vea "UNRANKED" sin ninguna pista
                # visible de qué pasó. Lo marcamos fuerte en el log.
                print(f"[leagues] ⚠️  API KEY INVÁLIDA O VENCIDA (status {r.status_code}) para {display_name}")

            else:
                print(f"[leagues] {display_name} -> {r.status_code}")

        except Exception as e:
            print(f"league fetch error {display_name}: {e}")

        stats = {
            "puuid": puuid,
            "game_name": display_name,
            "tag_line": tag_line,
            "region": region.upper(),
            "person_name": player_obj.get(
                'person_name',
                display_name
            ),
            "tier": "UNRANKED",
            "rank": "",
            "lp": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0,
            "icon_url": final_icon,
            "game_status": {
                # Se completa después en attach_game_status (pasada
                # secundaria). Hasta entonces se muestra como offline,
                # que se corrige solo apenas termina esa segunda pasada.
                "is_playing": False
            },
            "opgg_url": (
                manual_url
                or f"https://www.op.gg/summoners/{OPGG_REGION_SLUGS.get(region, region)}/{display_name}-{tag_line}"
            ),
            "emblem_url": "/static/img/ranks/unranked.png",
            "hot_streak": False
        }

        for q in leagues:

            if q.get('queueType') == 'RANKED_SOLO_5x5':

                stats.update({
                    "tier": q.get('tier'),
                    "rank": q.get('rank'),
                    "lp": q.get('leaguePoints'),
                    "wins": q.get('wins'),
                    "losses": q.get('losses'),
                    "hot_streak": q.get('hotStreak', False)
                })

                total_games = (
                    stats['wins']
                    + stats['losses']
                )

                if total_games > 0:
                    stats['winrate'] = int(
                        (stats['wins'] / total_games) * 100
                    )

                stats['emblem_url'] = (
                    f"/static/img/ranks/{stats['tier'].lower()}.png"
                )

                break

        current_sort_value = calculate_sort_score(
            stats['tier'],
            stats['rank'],
            stats['lp']
        )

        stats['sort_value'] = current_sort_value

        if stats['tier'] != 'UNRANKED':
            save_lp_snapshot(
                puuid,
                current_sort_value,
                stats['tier'],
                stats['rank'],
                stats['lp']
            )
            stats['lp_gain_24h'] = get_lp_change(
                puuid,
                current_sort_value,
                stats['tier'],
                stats['rank'],
                stats['lp'],
                hours=24
            )
        else:
            stats['lp_gain_24h'] = None

        return stats

    except Exception as e:
        print(f"fetch_core_stats error {display_name}: {e}")
        return None


def attach_game_status(stats):
    """PASADA SECUNDARIA del refresh, de menor prioridad: sólo actualiza
    si el jugador está en partida ahora mismo. Corre después de que el
    leaderboard con LP/rank ya se publicó -- si tarda unos segundos más
    en reflejarse, no se nota (a diferencia del elo, que sí importa que
    esté siempre al día)."""

    if not stats or not API_KEY:
        return stats

    headers = {
        "X-Riot-Token": API_KEY
    }

    stats['game_status'] = get_cached_game_status(
        stats['puuid'],
        stats['region'].lower(),
        headers
    )

    return stats


def _save_leaderboard_to_disk(leaderboard):
    """Guarda el último leaderboard publicado en disco, para poder servirlo
    de entrada la próxima vez que el proceso arranque (ej: tras un restart
    de PythonAnywhere por inactividad), en vez de mostrar 'Cargando...'
    otra vez por los ~10-20s que tarda la primera pasada a Riot."""
    try:
        tmp_path = leaderboard_cache_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(leaderboard, f)
        os.replace(tmp_path, leaderboard_cache_path)
    except Exception as e:
        print(f"leaderboard disk cache save error: {e}")


def _load_leaderboard_from_disk():
    try:
        if not os.path.exists(leaderboard_cache_path):
            return []
        with open(leaderboard_cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"leaderboard disk cache load error: {e}")
        return []

def _publish_leaderboard(leaderboard):
    # FIX (rendimiento): calculamos un etag simple a partir del contenido.
    # Así /update_data puede devolver "304 no cambió nada" en vez de
    # reenviar el JSON completo cuando no hubo cambios reales.
    fingerprint = json.dumps(
        [(p['puuid'], p['sort_value'], p['game_status']['is_playing']) for p in leaderboard],
        sort_keys=True
    )
    etag = hashlib.md5(fingerprint.encode()).hexdigest()

    with _leaderboard_lock:
        LEADERBOARD_CACHE["timestamp"] = time.time()
        LEADERBOARD_CACHE["data"] = leaderboard
        LEADERBOARD_CACHE["etag"] = etag

    _save_leaderboard_to_disk(leaderboard)   

def refresh_leaderboard_now():
    """Refresca todo el leaderboard en DOS pasadas, cada una en tandas
    espaciadas para no ráfaguear el rate limit de Riot (ver
    _run_in_paced_batches):

    1) Prioritaria: cuenta + LP/tier/rank/W-L. Arma el orden del
       leaderboard y se publica apenas termina -- es el dato que
       realmente importa que esté fresco.
    2) Secundaria: estado in-game ("jugando ahora"). Corre después,
       sobre el resultado ya publicado, y lo vuelve a publicar al
       terminar. Es cosmético, así que puede tardar unos segundos más
       sin que se note."""

    players_list = load_players_from_json()

    core_results = _run_in_paced_batches(players_list, fetch_core_stats)

    leaderboard = [x for x in core_results if x]

    leaderboard.sort(
        key=lambda x: x['sort_value'],
        reverse=True
    )

    _publish_leaderboard(leaderboard)

    _run_in_paced_batches(leaderboard, attach_game_status)

    _publish_leaderboard(leaderboard)

    return leaderboard


def get_leaderboard_data():

    with _leaderboard_lock:
        return list(LEADERBOARD_CACHE["data"])


def get_leaderboard_etag():

    with _leaderboard_lock:
        return LEADERBOARD_CACHE["etag"]


def get_available_regions():

    players_list = load_players_from_json()

    regions = set()

    for player in players_list:

        region = player.get(
            'region',
            'euw1'
        )

        regions.add(region)

    return sorted(list(regions))


def get_cutoffs_for_region(region):

    now = time.time()

    with CUTOFF_LOCK:

        if region in CUTOFF_CACHE:

            cache_entry = CUTOFF_CACHE[region]

            if (
                now - cache_entry['timestamp']
            ) < CUTOFF_CACHE_TIMEOUT:

                return cache_entry['data']

    if not API_KEY:
        return {
            "challenger": 500,
            "grandmaster": 200
        }

    # FIX: antes esto hacía REGION_CONFIG.get(region) y usaba config['challenger']
    # directo. Si la región no estaba en REGION_CONFIG (aunque sí en REGION_NAMES,
    # que es lo único que se valida en la ruta /cutoffs/<region>), tiraba un
    # KeyError sin capturar -> 500 feo en vez de una respuesta prolija.
    config = REGION_CONFIG.get(region)

    if not config:
        return {
            "challenger": 500,
            "grandmaster": 200
        }

    headers = {
        "X-Riot-Token": API_KEY
    }

    chall_slots = config['challenger']
    gm_slots = config['grandmaster']

    total_elite_slots = chall_slots + gm_slots

    cutoffs = {
        "challenger": 500,
        "grandmaster": 200
    }

    def fetch_league(tier):

        try:

            r = riot_get(
                f"https://{region}.api.riotgames.com/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5",
                headers,
                timeout=5
            )

            if r.status_code == 200:
                return r.json().get('entries', [])

            print(f"[cutoffs] {tier} {region} -> {r.status_code}")

        except Exception as e:
            print(f"cutoff fetch error {tier}: {e}")

        return []

    try:

        future_chall = EXECUTOR.submit(
            fetch_league,
            "challenger"
        )

        future_gm = EXECUTOR.submit(
            fetch_league,
            "grandmaster"
        )

        future_master = EXECUTOR.submit(
            fetch_league,
            "master"
        )

        chall_entries = future_chall.result()
        gm_entries = future_gm.result()
        master_entries = future_master.result()

        all_players = (
            chall_entries
            + gm_entries
            + master_entries
        )

        if not all_players:
            return cutoffs

        all_players.sort(
            key=lambda x: x.get('leaguePoints', 0),
            reverse=True
        )

        if len(all_players) >= chall_slots:
            cutoffs["challenger"] = max(
                500,
                all_players[chall_slots - 1].get(
                    'leaguePoints',
                    0
                )
            )

        if len(all_players) >= total_elite_slots:
            cutoffs["grandmaster"] = max(
                200,
                all_players[total_elite_slots - 1].get(
                    'leaguePoints',
                    0
                )
            )

        with CUTOFF_LOCK:
            CUTOFF_CACHE[region] = {
                'timestamp': now,
                'data': cutoffs
            }

        return cutoffs

    except Exception as e:
        print(f"cutoffs error: {e}")

        return {
            "challenger": 500,
            "grandmaster": 200
        }


def cleanup_old_lp_data():

    try:

        with get_db() as conn:

            cursor = conn.cursor()

            cutoff_date = (
                datetime.datetime.now()
                - datetime.timedelta(days=30)
            ).strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("""
                DELETE FROM lp_history
                WHERE timestamp < ?
            """, (cutoff_date,))

            deleted = cursor.rowcount

            conn.commit()

            if deleted > 0:
                print(f"DB cleanup: {deleted}")

    except Exception as e:
        print(f"cleanup_old_lp_data error: {e}")


def start_cleanup_scheduler():

    def run_cleanup():

        time.sleep(3600)

        while True:

            cleanup_old_lp_data()

            cleanup_cache(
                GAME_STATUS_CACHE,
                GAME_STATUS_CACHE_TIMEOUT * 5,
                GAME_STATUS_LOCK
            )

            cleanup_cache(
                CUTOFF_CACHE,
                CUTOFF_CACHE_TIMEOUT * 5,
                CUTOFF_LOCK
            )

            cleanup_cache(
                ACCOUNT_CACHE,
                ACCOUNT_CACHE_TIMEOUT * 5,
                ACCOUNT_LOCK
            )

            _prune_rate_limit_store()

            with _match_history_lock:
                now = time.time()
                stale = [
                    k for k, v in MATCH_HISTORY_CACHE.items()
                    if now - v['timestamp'] > MATCH_HISTORY_CACHE_TIMEOUT * 5
                ]
                for k in stale:
                    del MATCH_HISTORY_CACHE[k]

            time.sleep(86400)

    thread = Thread(
        target=run_cleanup,
        daemon=True
    )

    thread.start()


def start_leaderboard_refresher():
    """Refresca el leaderboard cada LEADERBOARD_REFRESH_INTERVAL segundos,
    siempre en background. home() y /update_data sólo leen el cache."""

    def run_refresh_loop():

        while True:

            try:
                refresh_leaderboard_now()

            except Exception as e:
                print(f"leaderboard refresh error: {e}")

            time.sleep(LEADERBOARD_REFRESH_INTERVAL)

    thread = Thread(
        target=run_refresh_loop,
        daemon=True
    )

    thread.start()


def _ensure_match_cache_columns(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(match_cache)").fetchall()}

    new_columns = {
        "item0": "INTEGER", "item1": "INTEGER", "item2": "INTEGER",
        "item3": "INTEGER", "item4": "INTEGER", "item5": "INTEGER", "item6": "INTEGER",
        "spell1": "INTEGER", "spell2": "INTEGER",
        "perk_primary": "INTEGER", "perk_sub_style": "INTEGER",
        "lp_change": "INTEGER"
    }

    for col, col_type in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE match_cache ADD COLUMN {col} {col_type}")


@app.route('/')
def home():

    data = get_leaderboard_data()

    regions = get_available_regions()

    default_region = 'euw1'

    if regions:

        region_counts = {}

        players_list = load_players_from_json()

        for player in players_list:

            region = player.get(
                'region',
                'euw1'
            )

            region_counts[region] = (
                region_counts.get(region, 0)
                + 1
            )

        default_region = max(
            region_counts,
            key=region_counts.get
        )

    response = make_response(render_template(
        'index.html',
        players=data,
        regions=regions,
        default_region=default_region,
        region_names=REGION_NAMES,
        internal_token=INTERNAL_TOKEN
    ))

    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'

    return response


@app.route('/update_data')
def update_data():

    _check_token()

    _check_rate_limit()

    # FIX (rendimiento): si el cliente ya tiene la última versión (mismo
    # etag), le devolvemos 304 sin cuerpo en vez de reenviar todo el JSON.
    # El leaderboard se refresca cada 90s en el server pero el front hace
    # polling cada 60s, así que la mayoría de esos polls no traen nada nuevo.
    current_etag = get_leaderboard_etag()
    client_etag = request.headers.get('If-None-Match', '')

    if current_etag and client_etag == current_etag:
        return '', 304

    data = get_leaderboard_data()

    clean_data = []

    for p in data:

        clean_data.append({
            "puuid": p['puuid'],
            "person_name": p['person_name'],
            "game_name": p['game_name'],
            "tag_line": p['tag_line'],
            "region": p['region'],
            "icon_url": p['icon_url'],
            "tier": p['tier'],
            "rank": p['rank'],
            "lp": p['lp'],
            "wins": p['wins'],
            "losses": p['losses'],
            "winrate": p['winrate'],
            "emblem_url": p['emblem_url'],
            "hot_streak": p['hot_streak'],
            "opgg_url": p['opgg_url'],
            "is_playing": p['game_status']['is_playing'],
            "mode": (
                p['game_status'].get('mode', '')
                if p['game_status']['is_playing']
                else ''
            ),
            "game_start_epoch_ms": (
                p['game_status'].get('game_start_epoch_ms')
                if p['game_status']['is_playing']
                else None

            ),
            "lp_gain_24h": p['lp_gain_24h']
            })

    response = jsonify(clean_data)

    if current_etag:
        response.headers['ETag'] = current_etag

    return response


@app.route('/player_detail/<puuid>')
def player_detail(puuid):
    """Panel expandible: evolución de elo (siempre, de la DB local) +
    últimas 7 partidas (Match-V5, cacheadas en DB y en memoria por 10 min)."""

    _check_token()

    _check_rate_limit()

    players_list = load_players_from_json()

    player_obj = next(
        (p for p in players_list if p.get('puuid') == puuid),
        None
    )

    if not player_obj:
        return jsonify({"error": "unknown puuid"}), 404

    region = player_obj.get('region', 'euw1')

    now = time.time()

    with _match_history_lock:
        cached = MATCH_HISTORY_CACHE.get(puuid)
        if cached and (now - cached['timestamp']) < MATCH_HISTORY_CACHE_TIMEOUT:
            matches = cached['data']
        else:
            matches = None

    if matches is None:
        matches = fetch_match_history(puuid, region, count=7)

        with _match_history_lock:
            MATCH_HISTORY_CACHE[puuid] = {
                'timestamp': now,
                'data': matches
            }

    elo_history = get_elo_history(puuid)

    return jsonify({
        "matches": matches,
        "elo_history": elo_history
    })


@app.route('/cutoffs/<region>')
def get_cutoffs(region):

    _check_token()

    _check_rate_limit()

    region = region.lower()

    if region not in REGION_NAMES:
        return jsonify({
            "error": "Invalid region"
        }), 400

    return jsonify(
        get_cutoffs_for_region(region)
    )


@app.after_request
def add_headers(response):

    if request.path.startswith('/static/'):

        response.headers['Cache-Control'] = (
            'public, max-age=31536000, immutable'
        )

    elif (
        request.path == '/update_data'
        or request.path.startswith('/cutoffs/')
        or request.path.startswith('/player_detail/')
    ):

        response.headers['Cache-Control'] = (
            'no-cache, no-store, must-revalidate'
        )

        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'

    elif request.path == '/':

        response.headers['Cache-Control'] = (
            'public, max-age=60'
        )

    response.headers['Vary'] = 'Accept-Encoding'

    return response


COMPRESSIBLE_TYPES = (
    'text/html',
    'application/json',
    'text/css',
    'application/javascript',
    'text/javascript',
)


@app.after_request
def compress_response(response):

    accept_encoding = request.headers.get('Accept-Encoding', '')

    if 'gzip' not in accept_encoding.lower():
        return response

    if response.direct_passthrough:
        return response

    if 'Content-Encoding' in response.headers:
        return response

    content_type = response.headers.get('Content-Type', '')

    if not any(t in content_type for t in COMPRESSIBLE_TYPES):
        return response

    data = response.get_data()

    if len(data) < 500:
        return response

    compressed = gzip.compress(data, compresslevel=6)

    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = len(compressed)

    return response


init_db()

start_cleanup_scheduler()


LEADERBOARD_CACHE["data"] = _load_leaderboard_from_disk()


start_leaderboard_refresher()





if __name__ == '__main__':

    app.run(
        debug=False,
        port=5000,
        threaded=True
    )