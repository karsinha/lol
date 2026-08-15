import requests
import sqlite3
import datetime
import json
import os
import time
import secrets
import hmac
import gzip

from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, jsonify, request, make_response, abort
from threading import Thread, Lock, Semaphore
from dotenv import load_dotenv


base_dir = os.path.dirname(os.path.abspath(__file__))

template_dir = os.path.join(base_dir, 'templates')
static_dir   = os.path.join(base_dir, 'static')
env_path     = os.path.join(base_dir, '.env')
db_path      = os.path.join(base_dir, 'soloq_history.db')
json_path    = os.path.join(base_dir, 'players.json')

load_dotenv(env_path)


app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder=static_dir
)


API_KEY = os.getenv("RIOT_API_KEY")

INTERNAL_TOKEN = (
    os.getenv("INTERNAL_TOKEN")
    or secrets.token_hex(32)
)

CACHE_TIMEOUT = 60
GAME_STATUS_CACHE_TIMEOUT = 60
CUTOFF_CACHE_TIMEOUT = 300


LEADERBOARD_REFRESH_INTERVAL = 90

MATCH_HISTORY_CACHE_TIMEOUT = 600  


EXECUTOR = ThreadPoolExecutor(max_workers=10)

RIOT_SEMAPHORE = Semaphore(5)

GAME_STATUS_LOCK = Lock()
CUTOFF_LOCK = Lock()

_players_cache_lock = Lock()

_rate_limit_lock = Lock()

_leaderboard_lock = Lock()

_match_history_lock = Lock()


GAME_STATUS_CACHE = {}

CUTOFF_CACHE = {}

LEADERBOARD_CACHE = {
    "timestamp": 0,
    "data": []
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

MATCH_ROUTING = {
    'na1': 'americas', 'br1': 'americas', 'la1': 'americas',
    'la2': 'americas', 'oc1': 'americas',
    'euw1': 'europe', 'eun1': 'europe', 'tr1': 'europe', 'ru': 'europe',
    'kr': 'asia', 'jp1': 'asia',
    'ph2': 'sea', 'sg2': 'sea', 'th2': 'sea', 'tw2': 'sea', 'vn2': 'sea'
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


def _is_rate_limited(ip: str):
    now = time.time()

    with _rate_limit_lock:
        calls = _rate_limit_store[ip]

        _rate_limit_store[ip] = [
            t for t in calls
            if now - t < RATE_LIMIT_WINDOW
        ]

        if len(_rate_limit_store[ip]) >= RATE_LIMIT_MAX:
            return True

        _rate_limit_store[ip].append(now)

        return False


def _check_token():
    token = request.headers.get('X-Internal-Token', '')

    if not hmac.compare_digest(token, INTERNAL_TOKEN):
        abort(403)


def _check_rate_limit():
    ip = request.headers.get(
        'X-Forwarded-For',
        request.remote_addr or ''
    ).split(',')[0].strip()

    if _is_rate_limited(ip):
        abort(429)


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


def get_lp_change(puuid, current_sort_value, hours=24):

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
                SELECT sort_value
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

            if row:
                return current_sort_value - row[0]

            return None

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


def fetch_match_history(puuid, region, count=7):

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

    with get_db() as conn:

        cursor = conn.cursor()

        for match_id in match_ids:

            cursor.execute("""
                SELECT champion, win, kills, deaths, assists, cs,
                       kill_participation, duration_min, queue_id, game_creation
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
                    "game_creation": cached[9]
                })
                continue

            try:
                mr = riot_get(
                    f"https://{continent}.api.riotgames.com/lol/match/v5/matches/{match_id}",
                    headers,
                    timeout=4
                )

                if mr.status_code != 200:
                    print(f"[match detail] {match_id} -> {mr.status_code}")
                    continue

                match_data = mr.json()
                info = match_data.get('info', {})
                participants = info.get('participants', [])

                me = next(
                    (p for p in participants if p.get('puuid') == puuid),
                    None
                )

                if not me:
                    continue

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

                cursor.execute("""
                    INSERT OR REPLACE INTO match_cache
                    (puuid, match_id, champion, win, kills, deaths, assists,
                     cs, kill_participation, duration_min, queue_id, game_creation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    puuid, match_id, champion, int(win), kills, deaths, assists,
                    cs, kp, duration_min, queue_id, game_creation
                ))

                conn.commit()

                results.append({
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
                    "game_creation": game_creation
                })

            except Exception as e:
                print(f"match detail fetch error {match_id}: {e}")
                continue

    results.sort(key=lambda x: x['game_creation'], reverse=True)

    return results


def riot_get(url, headers, timeout=3):

    with RIOT_SEMAPHORE:
        return session.get(
            url,
            headers=headers,
            timeout=timeout
        )


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

            game_length_minutes = int(
                d.get('gameLength', 0) / 60
            )

            game_status = {
                "is_playing": True,
                "mode": QUEUE_IDS.get(
                    d.get('gameQueueConfigId', 0),
                    "In-Game"
                ),
                "time": f"{game_length_minutes} min"
            }

    except Exception:
        pass

    with GAME_STATUS_LOCK:
        GAME_STATUS_CACHE[cache_key] = {
            'timestamp': now,
            'data': game_status
        }

    return game_status


def fetch_data_from_riot(player_obj):

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

        leagues = []

        try:

            r = riot_get(
                f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}",
                headers
            )

            if r.status_code == 200:
                leagues = r.json()

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
                "is_playing": False
            },
            "opgg_url": (
                manual_url
                or f"https://www.op.gg/summoners/{region}/{display_name}-{tag_line}"
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
            stats['lp_gain_24h'] = get_lp_change(puuid, current_sort_value, hours=24)
            stats['lp_gain_7d']  = get_lp_change(puuid, current_sort_value, hours=168)
        else:
            stats['lp_gain_24h'] = None
            stats['lp_gain_7d'] = None

        stats['game_status'] = get_cached_game_status(
            puuid,
            region,
            headers
        )

        return stats

    except Exception as e:
        print(f"fetch_data_from_riot error {display_name}: {e}")
        return None


def refresh_leaderboard_now():

    players_list = load_players_from_json()

    results = list(
        EXECUTOR.map(
            fetch_data_from_riot,
            players_list
        )
    )

    leaderboard = [x for x in results if x]

    leaderboard.sort(
        key=lambda x: x['sort_value'],
        reverse=True
    )

    with _leaderboard_lock:
        LEADERBOARD_CACHE["timestamp"] = time.time()
        LEADERBOARD_CACHE["data"] = leaderboard

    return leaderboard


def get_leaderboard_data():

    with _leaderboard_lock:
        return list(LEADERBOARD_CACHE["data"])


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

    headers = {
        "X-Riot-Token": API_KEY
    }

    config = REGION_CONFIG.get(region)

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
            "time": (
                p['game_status'].get('time', '')
                if p['game_status']['is_playing']
                else ''
            ),
            "lp_gain_24h": p['lp_gain_24h'],
            "lp_gain_7d": p['lp_gain_7d']
        })

    return jsonify(clean_data)


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

refresh_leaderboard_now()

start_leaderboard_refresher()


if __name__ == '__main__':

    app.run(
        debug=False,
        port=5000,
        threaded=True
    )