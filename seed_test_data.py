"""
Script de un solo uso para inyectar datos de prueba en soloq_history.db
y poder ver el gráfico de Elo del panel expandible sin depender de la
API de Riot ni esperar a que pasen días reales.

Uso:
    python seed_test_data.py <puuid>

Si no pasás un puuid, usa el primero que encuentre en players.json.
Correlo una vez, después arrancá la app normalmente (flask run / python app.py)
y abrí el desplegable de ese jugador: la pestaña "Stats & Elo" va a mostrar
la curva con estos puntos falsos.
"""
import sqlite3
import json
import sys
import random
import datetime
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(base_dir, "soloq_history.db")
json_path = os.path.join(base_dir, "players.json")


def get_puuid_from_args_or_json():
    if len(sys.argv) > 1:
        return sys.argv[1]

    with open(json_path, "r", encoding="utf-8") as f:
        players = json.load(f)

    if not players:
        raise SystemExit("players.json está vacío, pasá un puuid a mano.")

    return players[0]["puuid"]


def ensure_table(conn):
    # Mismo esquema que init_db() en app.py, por si corrés esto contra una DB nueva
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
    conn.commit()


def seed(puuid, days=14, points_per_day=3):
    conn = sqlite3.connect(db_path)
    ensure_table(conn)
    cur = conn.cursor()

    base_sort_value = 2450  # ~ Diamond II con algo de LP, ajustalo si querés
    now = datetime.datetime.now()

    rows = []
    value = base_sort_value

    total_points = days * points_per_day
    for i in range(total_points, 0, -1):
        ts = now - datetime.timedelta(hours=i * (24 / points_per_day))
        value += random.randint(-18, 22)  # sube y baja como una racha real
        rows.append((
            puuid,
            value,
            "DIAMOND",
            "II",
            value % 100,
            ts.strftime("%Y-%m-%d %H:%M:%S"),
        ))

    cur.executemany("""
        INSERT INTO lp_history (puuid, sort_value, tier, rank, lp, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows)

    conn.commit()
    conn.close()

    print(f"Insertados {len(rows)} puntos de prueba para puuid={puuid}")


if __name__ == "__main__":
    seed(get_puuid_from_args_or_json())