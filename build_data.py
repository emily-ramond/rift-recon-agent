"""Offline build: turn the Kaggle spreadsheet + Riot's Data Dragon into the parquet files the agent queries.

    1. Download https://www.kaggle.com/datasets/jakubkrasuski/league-of-legends-match-dataset-2025
       and unzip it so that data/raw/league_data.xlsx exists.
    2. uv run --with pandas --with openpyxl --with pyarrow build_data.py

Writes (all committed, ~6 MB total):
    data/participants.parquet  one row per player per game (the Kaggle data, plus queue_name and patch)
    data/items.parquet         item_id -> name, gold, tags      (Data Dragon, patch 15.1.1)
    data/champions.parquet     champion_id -> name, title, classes, base stats (Data Dragon, patch 15.1.1)
"""

import sys
from pathlib import Path

import httpx
import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw" / "league_data.xlsx"
OUT = HERE / "data"
DDRAGON = "https://ddragon.leagueoflegends.com/cdn/15.1.1/data/en_US/{}.json"

# Riot's queue ids -> what players call them. https://static.developer.riotgames.com/docs/lol/queues.json
QUEUES = {
    400: "Normal Draft",
    420: "Ranked Solo/Duo",
    440: "Ranked Flex",
    450: "ARAM",
    480: "Swiftplay",
    490: "Quickplay",
    700: "Clash",
    870: "Co-op vs AI (Intro)",
    890: "Co-op vs AI (Intermediate)",
}


def build_participants(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    # Excel turns a summoner named '1234' into a number: names are text
    df["summoner_name"] = df["summoner_name"].astype("string")
    df["queue_name"] = df["queue_id"].map(QUEUES).fillna("Other")
    df["patch"] = df["game_version"].str.split(".").str[:2].str.join(".")  # '15.1.649.4112' -> '15.1'
    df["game_duration_min"] = (df["game_duration"] / 60).round(2)
    # The raw epoch-millis column duplicates champion_mastery_lastPlayTime_utc
    return df.drop(columns=["champion_mastery_lastPlayTime"])


def build_items() -> pd.DataFrame:
    data = httpx.get(DDRAGON.format("item"), timeout=60).json()["data"]
    rows = [
        {
            "item_id": int(item_id),
            "item_name": item["name"],
            "gold_total": item["gold"]["total"],
            "is_boots": "Boots" in item.get("tags", []),
            "is_completed": not item.get("into") and item["gold"]["total"] >= 2000,
            "tags": ",".join(item.get("tags", [])),
            "plaintext": item.get("plaintext", ""),
        }
        for item_id, item in data.items()
    ]
    return pd.DataFrame(rows)


def build_champions() -> pd.DataFrame:
    data = httpx.get(DDRAGON.format("champion"), timeout=60).json()["data"]
    rows = [
        {
            "champion_id": int(c["key"]),
            "champion_name": c["id"],  # matches participants.champion_name, e.g. 'MonkeyKing'
            "display_name": c["name"],  # e.g. 'Wukong'
            "title": c["title"],
            "primary_class": c["tags"][0] if c["tags"] else None,
            "classes": ",".join(c["tags"]),
            "resource": c["partype"],
            "attack_range": c["stats"]["attackrange"],
            "difficulty": c["info"]["difficulty"],
        }
        for c in data.values()
    ]
    return pd.DataFrame(rows)


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else RAW
    participants = build_participants(src)
    participants.to_parquet(OUT / "participants.parquet", index=False, compression="zstd")
    build_items().to_parquet(OUT / "items.parquet", index=False)
    build_champions().to_parquet(OUT / "champions.parquet", index=False)
    print(f"{len(participants)} participant rows, {participants['game_id'].nunique()} games")
    for f in sorted(OUT.glob("*.parquet")):
        print(f.name, f.stat().st_size // 1024, "KB")
