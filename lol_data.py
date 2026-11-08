"""The match data, in an in-memory DuckDB, and the read-only queries the agents run against it.

build_data.py makes the parquet files offline. Here we load them once per process, add two
convenience views, and expose run_query(), which only runs a single SELECT and returns a small,
model-sized result.
"""

import re
import threading
from pathlib import Path

import duckdb

DATA = Path(__file__).parent / "data"
MAX_ROWS = 60  # rows shown to the model; the full count is always reported
QUERY_TIMEOUT_S = 20

_con = duckdb.connect(":memory:")
for table in ("participants", "items", "champions"):
    _con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{DATA / table}.parquet')")

_con.execute("""
CREATE VIEW games AS
SELECT game_id,
       any_value(game_start_utc)    AS game_start_utc,
       any_value(game_duration_min) AS game_duration_min,
       any_value(queue_id)          AS queue_id,
       any_value(queue_name)        AS queue_name,
       any_value(patch)             AS patch,
       max(CASE WHEN win THEN team_id END) AS winning_team_id,  -- 100 = blue side, 200 = red side
       count(*)                     AS participant_rows
FROM participants GROUP BY game_id
""")
_con.execute("""
CREATE VIEW participant_items AS
SELECT p.game_id, p.participant_id, p.puuid, p.champion_name, p.team_position, p.queue_name, p.win,
       x.slot, x.item_id, i.item_name, i.gold_total, i.is_boots, i.is_completed, i.tags
FROM participants p,
     LATERAL (SELECT unnest([0,1,2,3,4,5,6]) AS slot,
                     unnest([p.item0,p.item1,p.item2,p.item3,p.item4,p.item5,p.item6]) AS item_id) x
JOIN items i ON i.item_id = x.item_id
WHERE x.item_id <> 0
""")
# Lock it down: no files, no network, no settings changes from model-written SQL
_con.execute("SET enable_external_access = false")
_con.execute("SET lock_configuration = true")


def _scalar(sql: str):
    return _con.execute(sql).fetchone()


_rows, _games, _players = _scalar("SELECT count(*), count(DISTINCT game_id), count(DISTINCT puuid) FROM participants")
_first, _last = _scalar("SELECT min(game_start_utc)::DATE, max(game_start_utc)::DATE FROM participants")
_queues = ", ".join(
    f"{q} ({n:,} games)"
    for q, n in _con.execute("SELECT queue_name, count(*) FROM games GROUP BY 1 ORDER BY 2 DESC").fetchall()
)

STATS = {
    "participant_rows": _rows,
    "games": _games,
    "players": _players,
    "first_game": str(_first),
    "last_game": str(_last),
    "region": "EUN1 (EU Nordic & East)",
}

SCHEMA = f"""\
DATASET: League of Legends match data, Kaggle "league-of-legends-match-dataset-2025" (jakubkrasuski),
joined with Riot's Data Dragon (patch 15.1.1) for item and champion metadata.
{_rows:,} player-rows from {_games:,} games and {_players:,} distinct players, all on the EUN1 server.
Games span {_first} to {_last}, but ~95% are from January 2025 (mostly 12-20 Jan, patch 15.1).
Queues: {_queues}.

TABLES (DuckDB SQL)
participants  -- one row per player per game (10 per game)
  game_id, game_start_utc (TIMESTAMP), game_duration (seconds), game_duration_min, game_mode
  ('CLASSIC','ARAM','SWIFTPLAY'), queue_id, queue_name ('Ranked Solo/Duo','Ranked Flex','ARAM',
  'Normal Draft','Swiftplay',...), game_version, patch ('15.1','14.24',...), platform_id
  participant_id (1-10), puuid (stable player id), summoner_name, summoner_level
  champion_id, champion_name (Data Dragon id: 'MonkeyKing' = Wukong, 'MissFortune', 'KSante', 'Nunu', ...)
  team_id (100 = blue side, 200 = red side), win (BOOLEAN)
  team_position ('TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY'; NULL in ARAM/Swiftplay), individual_position, lane, role
  kills, deaths, assists, baron_kills, dragon_kills (by this player), gold_earned, gold_spent
  total_damage_dealt, total_damage_dealt_to_champions, physical_/magic_/true_damage_dealt_to_champions,
  damage_dealt_to_objectives, damage_dealt_to_turrets, total_damage_taken, physical_/magic_/true_damage_taken,
  time_ccing_others, vision_score, wards_placed, wards_killed, vision_wards_bought_in_game
  item0..item5 (final inventory item ids, 0 = empty), item6 (trinket)
  solo_tier ('IRON'..'CHALLENGER', NULL = unranked), solo_rank ('I'..'IV'), solo_lp, solo_wins, solo_losses,
  flex_tier, flex_rank, flex_lp, flex_wins, flex_losses   -- the player's rank WHEN SCRAPED (~20 Jan 2025)
  champion_mastery_level, champion_mastery_points, champion_mastery_lastPlayTime_utc,
  champion_mastery_pointsSinceLastLevel, champion_mastery_pointsUntilNextLevel, champion_mastery_tokensEarned
      -- the player's mastery on THIS champion when scraped
  final_abilityHaste, final_abilityPower, final_armor, final_attackDamage, final_attackSpeed, final_health,
  final_healthMax, final_magicResist, final_movementSpeed, final_lifesteal, final_omnivamp, final_magicPen,
  final_armorPen, ... -- the champion's stats at the END of the game
items       -- item_id, item_name, gold_total, is_boots, is_completed (full item, >=2000g), tags, plaintext
champions   -- champion_id, champion_name, display_name, title, primary_class ('Fighter','Mage','Marksman',
               'Assassin','Tank','Support'), classes, resource, attack_range, difficulty (1-10)
games (view)              -- one row per game: game_id, game_start_utc, game_duration_min, queue_name,
                             patch, winning_team_id, participant_rows
participant_items (view)  -- one row per item a player ended with: game_id, participant_id, puuid,
                             champion_name, team_position, queue_name, win, slot, item_id, item_name,
                             gold_total, is_boots, is_completed, tags

TIPS
- Filter queue_name: ranked questions usually mean 'Ranked Solo/Duo'; ARAM is a different game mode.
- Win rate = avg(win::INT). Always report the sample size n next to a rate; under ~30 games is anecdotal.
- KDA = (kills + assists) / greatest(deaths, 1). Per-minute stats: divide by game_duration_min.
- Rank is per player at scrape time; to compare ranks, group by solo_tier.

WHAT THIS DATA CANNOT ANSWER (say so plainly when asked)
- Anything outside EUN1, or pro play / esports / tournaments.
- Anything time-within-game: no timeline, so no first blood, gold or CS at 10/15 min, objective timings,
  power spikes, or when items were bought (only the final inventory).
- No minion/CS counts, no runes, no summoner spells, no skill order, no pick/ban or draft order, no
  team-level tower/dragon/herald totals (only each player's own baron_kills/dragon_kills).
- Long-term trends: almost every game is from one week on patch 15.1.
- The rank at the time of each game (rank columns are a snapshot), or the reason anyone won.
- The sample is not random: it was crawled from players' match histories and skews toward high ranks.
"""

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|install|load|export|import|"
    r"set|reset|call|checkpoint|vacuum|use)\b",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"/\*.*?\*/", "", sql, flags=re.S)


def check_sql(sql: str) -> str:
    """Return the cleaned single SELECT statement, or raise ValueError with a reason the model can act on."""
    body = _strip_comments(sql).strip().rstrip(";").strip()
    if not body:
        raise ValueError("Empty query.")
    if ";" in body:
        raise ValueError("Send one statement at a time (no ';').")
    if not re.match(r"^(select|with|from)\b", body, re.IGNORECASE):
        raise ValueError("Only read-only SELECT (or WITH ... SELECT) queries are allowed.")
    # Ignore keywords inside string literals, e.g. WHERE item_name = 'Set ...'
    if _FORBIDDEN.search(re.sub(r"'(?:[^']|'')*'", "''", body)):
        raise ValueError("Only read-only SELECT queries are allowed; this one contains a write or settings keyword.")
    return body


def run_query(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Run one SELECT. Returns {columns, rows, row_count, truncated} or raises."""
    body = check_sql(sql)
    cur = _con.cursor()  # one cursor per call: researchers run in parallel threads
    timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
    timer.start()
    try:
        rel = cur.execute(body)
        columns = [d[0] for d in rel.description]
        rows = rel.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        if truncated:
            row_count = cur.execute(f"SELECT count(*) FROM ({body})").fetchone()[0]
        else:
            row_count = len(rows)
    finally:
        timer.cancel()
        cur.close()
    return {"columns": columns, "rows": [list(map(_plain, r)) for r in rows], "row_count": row_count, "truncated": truncated}


def _plain(v):
    if isinstance(v, float):
        return round(v, 4)
    if v is None or isinstance(v, (int, str, bool)):
        return v
    return str(v)


def as_table(result: dict) -> str:
    """A compact pipe table for the model."""
    lines = [" | ".join(result["columns"])]
    lines += [" | ".join("" if v is None else str(v) for v in row) for row in result["rows"]]
    return "\n".join(lines)


if __name__ == "__main__":
    # Smoke test: uv run lol_data.py
    print(STATS)
    r = run_query("SELECT champion_name, count(*) n, round(avg(win::INT),3) wr FROM participants "
                  "WHERE queue_name='Ranked Solo/Duo' GROUP BY 1 ORDER BY n DESC LIMIT 5")
    print(as_table(r))
    print(as_table(run_query("SELECT item_name, count(*) n FROM participant_items WHERE is_boots GROUP BY 1 ORDER BY n DESC LIMIT 3")))
    for bad in ("DROP TABLE items", "SELECT 1; SELECT 2", "SELECT * FROM read_csv('/etc/passwd')"):
        try:
            run_query(bad)
            print("NOT BLOCKED:", bad)
        except Exception as e:
            print("blocked:", type(e).__name__, str(e)[:80])
