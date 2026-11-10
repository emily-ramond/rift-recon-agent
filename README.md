# Rift Recon: deep research over real League of Legends matches

**Path: Deep Research.** Rift Recon answers League of Legends questions with evidence from **4,044 real
games** (40,410 player-rows) played on the EU Nordic & East server, mostly in January 2025 on patch 15.1.
A **Lead** agent plans the research, several **Researcher** agents query the data with SQL in parallel,
and a **Writer** agent turns their findings into a short report where every number is cited to the query
that produced it.

**Who it's for:** League players, coaches and analysts who want to test a belief ("control wards win
games", "blue side is favored", "Jinx is overrated") against real games instead of tier-list vibes. Riot
doesn't publish win rates by role, rank, item or build for a specific region and week, and the model can't
know what happened in these 4,044 particular games, so the agent has to query the data. It also says
plainly when the data can't answer something, like timelines, pro play or other regions.

**Deployed:** https://rift-recon-368907527390.europe-west1.run.app (Cloud Run in europe-west1, Columbia sign-in via IAP). Every push to `main` rebuilds and redeploys it through Developer Connect and Cloud Build.

## How it works

```
                 ┌──────────── research(task) ×2-4, same turn ────────────┐
User ─► Lead ────┤  Researcher 1 (fresh context) ─ run_sql / win_rate ─► Q1..Qn  │──► Lead: gaps? ──► (more researchers, ≤2 rounds)
   ▲     │       │  Researcher 2 (fresh context) ─ run_sql / win_rate ─► ...     │            │
   │     │       └──────────────────────────────────────────────────────────┘            ▼
   │     └─ request_clarification (vague question) ─► back to the user           Writer ─► cited report
   │                                                                                       │
   └──────────────────────── app checks every [Q#] against the query ledger ◄──────────────┘
```

| Agent | Model | Job | Tools |
|---|---|---|---|
| **Lead** | gemini-3.5-flash | Asks one clarifying question if the request is too vague to measure. Otherwise splits it into 2-4 tasks and fans them out **in one turn**, reads the findings, and sends more researchers for any gap (max 2 extra rounds, 7 researchers total). Replies `CANNOT:` when nothing nearby is answerable. | `request_clarification`, `research`, `run_sql` |
| **Researcher** ×N | gemini-3.5-flash | One task each, in a **fresh context** (it can't see the question or the other researchers). Writes SQL, fixes its own errors, and reports at most 8 bullets, each number cited `[Q#]` with its sample size. | `run_sql`, `win_rate` |
| **Writer** | gemini-3.5-flash | Sees only the question, the findings and the query ledger. Writes the report and a "What this data can't tell us" section. | none |

**Tools** ([app.py](app.py), [lol_data.py](lol_data.py))
- `run_sql(sql, purpose)` runs one read-only DuckDB `SELECT`. Writes, file access and multiple statements are blocked, and queries time out after 20 s. Every successful query gets a citation id (`[Q7]`) and goes into the run's ledger.
- `win_rate(group_by, where, table, min_games)` returns win rates with sample size and a **95% Wilson confidence interval**, so the agents can tell a real gap from noise.
- `research(task)` runs a numbered Researcher in a fresh context, in parallel with its siblings.
- `request_clarification(question, options)` stops the Lead's run (`StopAtTools`) and returns clickable options. After one clarification the Lead is swapped for a copy without this tool, so it can't ask twice in a row.

**Accurate citations.** Citation ids come from the tool, not the model. After the Writer finishes, the app
checks every `[Q#]` against the ledger, marks any id that never ran as `[unverified]`, and appends the
exact SQL behind each citation (an "Evidence" section). In the UI, click a citation chip to open its query
and highlight the tool call that produced it.

**Show your work.** `POST /chat` keeps Project 1's shape: `response`, `session_id` and `tool_calls`, with
`agent`, `name`, `args` and `result` for **every** call, the Researchers' included. The page uses `GET /run`, which streams
the same events live into the "war room" (one lane per agent, each tool call expandable). "Raw contexts"
opens the template's context panel, which shows the exact messages each agent sent to the model.

**Caps:** Lead 10 turns; Researchers 10 turns and 7 queries each; 7 researchers per question; 210 s
research deadline, after which the Writer reports what came back. A typical question takes 40-150 s.

## Data

| Source | What | How it's loaded |
|---|---|---|
| [Kaggle: League of Legends Match Dataset 2025](https://www.kaggle.com/datasets/jakubkrasuski/league-of-legends-match-dataset-2025) (jakubkrasuski) | 40,410 rows, one per player per game: 4,044 games and 28,098 players on EUN1. Queues: Ranked Solo/Duo (2,191 games), ARAM (973), Ranked Flex (399), Normal Draft (342), Swiftplay (125), other (14). About 160 columns: KDA, gold, damage by type, vision, wards, final items, end-of-game champion stats, each player's solo/flex rank and champion mastery. | `data/participants.parquet` |
| [Riot Data Dragon 15.1.1](https://ddragon.leagueoflegends.com/cdn/15.1.1/data/en_US/item.json) | Item names, costs and tags; champion names, titles, classes and difficulty. 100% of item and champion ids in the Kaggle data join. | `data/items.parquet`, `data/champions.parquet` |

**Offline build** ([build_data.py](build_data.py)): download the Kaggle zip, unzip so `data/raw/league_data.xlsx`
exists, then run `uv run --with pandas --with openpyxl --with pyarrow build_data.py`. It adds `queue_name`,
`patch` and `game_duration_min`, fixes summoner names that Excel turned into numbers, and fetches Data Dragon. The
parquet files (about 7 MB) are committed, so the deploy needs nothing else. At startup, [lol_data.py](lol_data.py) loads them into
in-memory DuckDB and adds two views: `games` (one row per game, with the winning side) and
`participant_items` (one row per final item, joined to item names).

**What the data can't answer.** The agents get this list and say so when it applies:
- Anything outside EUN1, and pro play or esports.
- Anything that happens at a point in time within a game. There's no timeline, so no first blood, gold or CS at 15 minutes, objective timings, or build order (only the final inventory).
- CS, runes, summoner spells, skill order, pick/ban and draft order, and team totals for towers or dragons.
- Long-term trends: almost all games are from one week on patch 15.1.
- Each player's rank at the time of a game: rank columns are a snapshot taken around 20 Jan 2025.
- Ladder-wide rates: the sample was crawled from match histories and skews high-elo.

## Run locally

```bash
gcloud auth application-default login
uv run app.py           # http://localhost:8000
uv run lol_data.py      # data smoke test
```

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"Does blue side win more in ranked solo queue?"}' | jq '.response, (.tool_calls[] | [.agent,.name])'
```

## Sample queries for graders

Expected answers come from direct SQL over the committed parquet files. "No tools" is the same model
(`gemini-3.5-flash`) with no tools and no system prompt.

### 1. "In ranked solo queue, do junglers who buy more control wards win more often?"

**Expected** (`participants`, `queue_name = 'Ranked Solo/Duo' AND team_position = 'JUNGLE'`, grouped by `vision_wards_bought_in_game`):
yes, up to a point. 0 wards: 44.7% (n=1,863). 1-2 wards: 53.1% (n=1,497). 3+ wards: 55.1% (n=1,021).
2 wards peak at 57.7% (n=626), and 5+ dips to 53.2% (n=380).

| No tools | Rift Recon |
|---|---|
| Yes, with diminishing returns. It cites "statistical sites (LeagueofGraphs, U.GG)" and makes up per-tier averages ("Diamond+ junglers buy 3 to 5 per game") and a drop-off at "7 or more". None of it is from data it can show. | 0 wards 44.71% (n=1,863) vs. 2 wards **57.67%** (n=626) [Q2][Q3]. 0 vs. 1-2 is significant (CIs don't overlap), 3+ plateaus, and 5+ falls to 53.16%. It warns that this is correlation: winning junglers have more gold, and games with more wards run longer (27.2 → 31.2 min) [Q2]. |

### 2. "Does blue side or red side win more in Ranked Solo/Duo, and does it depend on game length?"

**Expected** (`games`, `queue_name = 'Ranked Solo/Duo'`, `winning_team_id`):
blue wins 1,151 of 2,191 games (52.5%). By length, blue wins 54.2% under 25 min, 52.8% at 25-35 min, and 48.5% at 35+ min.

| No tools | Rift Recon |
|---|---|
| "Blue side wins more", about 50.5-52% "historically". Its length explanation (first pick, voidgrubs, a "strong blue side advantage" early) is generic reasoning, not a measurement. | Blue **52.53%** (1,151 of 2,191), 95% CI 50.4-54.6%, so significant [Q6][Q8]. By length, blue peaks at 55.4% at 15-20 min and drops below 50% after 35 min. It says the late-game flip isn't significant because every bucket's CI overlaps 50% [Q10][Q11]. |

### 3. "Which marksman champions have the best win rate in Ranked Solo/Duo with at least 100 games, and is the gap real?"

**Expected** (`participants` joined to `champions`, `primary_class = 'Marksman'`, `count(*) >= 100`):
Teemo 56.9% (n=116), Vayne 56.1% (n=196), Jhin 54.2% (n=358), Miss Fortune 52.7% (n=423). The lowest is Smolder at 42.0% (n=119).
The gaps aren't significant: every 95% CI overlaps.

| No tools | Rift Recon |
|---|---|
| A generic Season 14 tier list (Ashe, Miss Fortune, Jinx, Kog'Maw, Nilah at "51.5-53.5%"). It can't apply the 100-game filter or test the gap. | Teemo 56.90% (n=116), Vayne 56.12% (196), Senna 54.35% (138), Jhin 54.19% (358), MF 52.72% (423), with Wilson CIs in a table [Q5][Q7]. "Not statistically real": the intervals overlap heavily, even Teemo vs. last-place Smolder (42.0%) [Q4][Q5]. |

The no-tools model sounds confident but answers from general knowledge and invented numbers. The agent's numbers come from these 4,044 games, each cited to a query, and it says which gaps are noise.

**Also try:**
- **Vague:** "Who's the best?" or "What should I build?" The Lead asks one clarifying question with clickable options, then researches your answer in the same session.
- **Out of scope:** "What was T1's win rate at Worlds 2024?" The agent says the data is EUN1 solo queue, not pro play, and suggests questions it can answer.

## Files

| File | |
|---|---|
| `app.py` | Agents, tools, citation checking, `/chat` (JSON) and `/run` (SSE) |
| `lol_data.py` | DuckDB loading, schema doc and limits given to the agents, read-only query runner |
| `build_data.py` | Offline build: Kaggle xlsx + Data Dragon to parquet |
| `index.html` | Themed front end: intro, research desk, live war room, citation chips |
| `context_panel.py`, `context-panel.js` | The template's per-agent context timeline (the "Raw contexts" button) |

Built from the course's `wikipedia-deep-research` template (see the first commit).
Rift Recon isn't endorsed by Riot Games.
