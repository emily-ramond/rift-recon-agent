"""Rift Recon: deep research over 4,044 real League of Legends matches.

A Lead reads the question and either asks one clarifying question (if it's too vague to measure)
or splits it into 2-4 research tasks. It calls `research` once per task in the same turn, so the
Researchers run in parallel, each in a fresh context, writing SQL against the match data. Every
query gets an id (Q1, Q2, ...) that the Researchers cite. The Lead reads their findings and sends
more Researchers if there's a gap. Then a Writer, which sees only the question, the findings and the
query ledger, writes a short cited report. The app checks every citation against the ledger and
appends the SQL behind each one.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from agents import (
    Agent,
    ModelSettings,
    RunContextWrapper,
    Runner,
    StopAtTools,
    function_tool,
    set_tracing_disabled,
)
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import lol_data
from context_panel import AgentContexts, PanelHooks, _arguments, add_context_panel

load_dotenv(Path(__file__).parent / ".env")

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes the call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# Researchers write SQL: the full Flash model makes far fewer broken queries than Flash-Lite.
RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})

# Caps. Prompts ask for restraint; code enforces it.
MAX_RESEARCHERS = 7  # in all, across every round
MAX_QUERIES_PER_RESEARCHER = 7
LEAD_MAX_TURNS = 10
RESEARCHER_MAX_TURNS = 10
RESEARCH_DEADLINE_S = 210  # then the Writer reports whatever has come back (graders wait 5 min)


@dataclass
class Research:
    """One run's state, shared by every agent and tool in it. The model never sees it."""

    hooks: "TraceHooks"
    researchers: int = 0  # how many researchers have started, to number them
    findings: list[str] = field(default_factory=list)  # what each researcher reported, for the Writer
    queries: list[dict] = field(default_factory=list)  # the citation ledger: Q1, Q2, ...
    tool_calls: list[dict] = field(default_factory=list)  # every tool call by every agent, for /chat
    call_agent: dict[str, str] = field(default_factory=dict)  # tool_call_id -> agent name
    queries_by_agent: dict[str, int] = field(default_factory=dict)
    clarification: dict | None = None  # set when the Lead asks the user something instead


# --- Tools ---


def _record_query(ctx: RunContextWrapper[Research], sql: str, purpose: str, result: dict) -> str:
    run = ctx.context
    qid = f"Q{len(run.queries) + 1}"
    agent = run.call_agent.get(getattr(ctx, "tool_call_id", ""), "?")
    run.queries.append(
        {
            "id": qid,
            "agent": agent,
            "purpose": purpose,
            "sql": sql.strip(),
            "columns": result["columns"],
            "rows": result["rows"][:12],
            "row_count": result["row_count"],
        }
    )
    return qid


def _over_budget(ctx: RunContextWrapper[Research]) -> str | None:
    agent = ctx.context.call_agent.get(getattr(ctx, "tool_call_id", ""), "?")
    used = ctx.context.queries_by_agent.get(agent, 0)
    if agent.startswith("Researcher") and used >= MAX_QUERIES_PER_RESEARCHER:
        return "Query budget used up. Write your findings now from the queries you've already run."
    ctx.context.queries_by_agent[agent] = used + 1
    return None


@function_tool
async def run_sql(ctx: RunContextWrapper[Research], sql: str, purpose: str) -> str:
    """Run one read-only DuckDB SELECT over the League match data and get back a cited result.
    The reply starts with the query's citation id, e.g. [Q4]: cite it next to every number it supports.
    At most 60 rows are shown; aggregate in SQL rather than pulling raw rows.

    Args:
        sql: A single SELECT (or WITH ... SELECT) over participants, games, participant_items,
            items or champions. No semicolons.
        purpose: A short phrase saying what this query measures, e.g. 'Jinx win rate in Ranked Solo/Duo'.
    """
    if msg := _over_budget(ctx):
        return msg
    try:
        result = await asyncio.to_thread(lol_data.run_query, sql)
    except Exception as e:
        # The model cannot see an exception. Return something it can fix.
        hint = ""
        if "not found" in str(e).lower() or "does not have a column" in str(e).lower():
            hint = " Check the column list in your instructions; names are snake_case, e.g. champion_name, team_position."
        return f"ERROR (no citation id; fix the query and retry): {type(e).__name__}: {str(e)[:400]}{hint}"
    qid = _record_query(ctx, sql, purpose, result)
    shown = len(result["rows"])
    head = f"[{qid}] {result['row_count']} rows" + (f" (showing first {shown})" if result["truncated"] else "")
    if result["row_count"] == 0:
        head += ". No rows: check spelling (e.g. 'MonkeyKing' for Wukong) and filters before concluding it never happens."
    return f"{head}\n{lol_data.as_table(result)}"


WIN_RATE_TABLES = {"participants", "participant_items"}


@function_tool
async def win_rate(
    ctx: RunContextWrapper[Research],
    group_by: str,
    where: str = "",
    table: str = "participants",
    min_games: int = 20,
    limit: int = 25,
) -> str:
    """Win rates with sample sizes and 95% confidence intervals (Wilson), grouped however you like.
    Use this instead of hand-written SQL whenever you compare win rates: it tells you which
    differences are real and which are noise. The reply starts with a citation id like [Q7].

    Args:
        group_by: Column(s) or expressions to group by, e.g. 'champion_name' or
            'team_position, solo_tier' or "CASE WHEN vision_score >= 40 THEN 'high' ELSE 'low' END AS vision".
        where: Optional SQL filter, e.g. "queue_name = 'Ranked Solo/Duo' AND team_position = 'JUNGLE'".
        table: 'participants' (one row per player per game) or 'participant_items' (one row per final item).
        min_games: Hide groups with fewer games than this.
        limit: Max groups to return, largest samples first.
    """
    if table not in WIN_RATE_TABLES:
        return f"ERROR: table must be one of {sorted(WIN_RATE_TABLES)}."
    if msg := _over_budget(ctx):
        return msg
    min_games, limit = max(1, int(min_games)), max(1, min(int(limit), 60))
    sql = f"""SELECT *, round(win_rate, 4) AS win_rate,
       round((win_rate + 1.9208/n - 1.96*sqrt(win_rate*(1-win_rate)/n + 0.9604/(n*n))) / (1 + 3.8416/n), 4) AS ci95_low,
       round((win_rate + 1.9208/n + 1.96*sqrt(win_rate*(1-win_rate)/n + 0.9604/(n*n))) / (1 + 3.8416/n), 4) AS ci95_high
FROM (SELECT {group_by}, count(*) AS n, sum(win::INT) AS wins, avg(win::INT) AS win_rate
      FROM {table} {"WHERE " + where if where.strip() else ""}
      GROUP BY ALL HAVING count(*) >= {min_games})
ORDER BY n DESC LIMIT {limit}"""
    # SELECT * then win_rate again would duplicate the column: name the raw one away
    sql = sql.replace("SELECT *, round(win_rate, 4) AS win_rate,", "SELECT * EXCLUDE (win_rate), round(win_rate, 4) AS win_rate,")
    try:
        result = await asyncio.to_thread(lol_data.run_query, sql)
    except Exception as e:
        return f"ERROR (no citation id; fix group_by/where and retry): {type(e).__name__}: {str(e)[:400]}"
    qid = _record_query(ctx, sql, f"win rate by {group_by}" + (f" where {where}" if where else ""), result)
    note = "" if result["row_count"] else f". No group has {min_games}+ games: lower min_games or widen the filter."
    return f"[{qid}] {result['row_count']} groups (n = games, ci95 = 95% interval for the win rate){note}\n{lol_data.as_table(result)}"


@function_tool
async def request_clarification(ctx: RunContextWrapper[Research], question: str, options: list[str]) -> str:
    """Ask the user one short clarifying question instead of researching, because the request is too
    vague to measure. This ends your turn; the user's answer comes back in the next message.

    Args:
        question: One short question, e.g. 'Which role do you play, and in which queue?'
        options: 2-4 concrete answers the user can click, e.g. ['Jungle, Ranked Solo/Duo', 'Support, ARAM'].
    """
    ctx.context.clarification = {"question": question, "options": [str(o) for o in options][:4]}
    return "Asked the user."


# --- The researcher: one task, by querying the match data ---

RESEARCHER_INSTRUCTIONS = f"""\
You are a League of Legends data analyst. You research ONE task by querying real match data with SQL.

{lol_data.SCHEMA}

How to work:
1. Plan the 2-5 queries that answer your task. Use win_rate for any win-rate comparison (it adds
   sample sizes and confidence intervals); use run_sql for everything else. At most {MAX_QUERIES_PER_RESEARCHER} queries.
2. If a query errors, read the error and fix it. If a name returns nothing, look it up first
   (e.g. SELECT DISTINCT champion_name FROM champions WHERE display_name ILIKE '%wukong%').
3. Reply with the facts that answer your task and nothing else, at most 8 bullets:
   - End every number with the citation id of the query it came from, e.g.
     "Jinx won 49.9% of 411 Ranked Solo/Duo games (95% CI 45-55%) [Q4]". Never cite an id you didn't get back.
   - Always give the sample size. Call a difference real only if the confidence intervals don't overlap;
     flag groups under 30 games as anecdotal.
   - If the data can't answer part of the task, say so in a last bullet, and why (see WHAT THIS DATA CANNOT ANSWER).
   - Items are the FINAL inventory only. Say "finished with", never "rushed", "first item", "build order"
     or "bought before". Expensive late items (tier-3 boots, 6th items) show up in long games and in games
     that were already won, so their win rates are inflated: say so when you report them."""

researcher = Agent(
    name="Researcher",
    instructions=RESEARCHER_INSTRUCTIONS,
    model=RESEARCHER_MODEL,
    model_settings=SETTINGS,
    tools=[run_sql, win_rate],
)


@function_tool
async def research(ctx: RunContextWrapper[Research], task: str) -> str:
    """Send a researcher to answer one task with SQL over the match data, in a fresh context.
    Returns its findings, each number cited with the query it came from.

    Args:
        task: One specific, measurable thing to find out. Say which queue, role or champions to
            filter on and which metric to use, e.g. "In Ranked Solo/Duo, compare win rate and KDA of
            junglers by primary_class (Fighter, Assassin, Tank...), with sample sizes."
    """
    if ctx.context.researchers >= MAX_RESEARCHERS:
        return "No researchers left. Reply DONE."
    # What researcher.as_tool() does: run the researcher in a fresh context and return its answer.
    # Written out so each run gets a numbered copy, and its own lane in the UI.
    ctx.context.researchers += 1
    agent = researcher.clone(name=f"Researcher {ctx.context.researchers}")
    try:
        result = await Runner.run(
            agent, f"Task: {task}", context=ctx.context, hooks=ctx.context.hooks, max_turns=RESEARCHER_MAX_TURNS
        )
        findings = str(result.final_output)
    except Exception as e:
        findings = f"(This researcher failed: {type(e).__name__}: {str(e)[:200]}. Treat the task as unanswered.)"
    ctx.context.findings.append(f"### {agent.name}: {task}\n\n{findings}")
    return findings


# --- The lead: clarifies, plans, fans out to researchers, and checks for gaps ---

LEAD_INSTRUCTIONS = f"""\
You lead a team that answers questions about League of Legends from real match data. The people asking
are players, coaches and analysts who want evidence, not received wisdom.

{lol_data.SCHEMA}

Work in this order:
0. Is the request clear enough to measure? It is VAGUE if it has no measurable target, or if the
   answer would change a lot depending on something the user didn't say. Examples:
   "who's the best?", "is my champ good?", "how do I climb?", "what should I build?" (on whom? which role?),
   "what's broken right now?". If it's vague, call request_clarification with one short question and
   2-4 concrete options, and stop. Do NOT ask when there's a sensible default you can state instead
   (e.g. "ranked" means Ranked Solo/Duo; "win rate" needs no definition), and don't ask more than once:
   if the conversation shows you already asked, go with the user's answer plus stated assumptions.
1. If NOTHING close to the question can be answered from this data (see WHAT THIS DATA CANNOT ANSWER),
   don't research: reply "CANNOT: " followed by why, and what nearby question the data could answer.
   If part of it can be answered, research that part; the writer will say what's missing.
2. Split the question into 2-4 independent research tasks. Call research once per task, all in the
   same turn, so the researchers work in parallel. Each task must be specific: which queue, role or
   champions, which metric, and what to compare. Researchers can't see the question or each other.
   You may run_sql once or twice first to check a name or a count, but leave the analysis to them.
3. Read the findings. If part of the question is still unanswered, two findings disagree, or a key
   claim rests on a tiny sample, call research again for just that gap. Do this at most twice.
4. When the question is covered, reply DONE and nothing else: a writer writes the report."""

lead = Agent(
    name="Lead",
    instructions=LEAD_INSTRUCTIONS,
    model=MODEL,
    model_settings=SETTINGS,
    tools=[request_clarification, run_sql, research],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["request_clarification"]),
)
# After one clarifying question, the Lead must commit: same agent, no clarification tool.
lead_committed = lead.clone(tools=[run_sql, research], tool_use_behavior="run_llm_again")

# --- The writer: one call, after the research ends ---

writer = Agent(
    name="Writer",
    instructions="""\
You write a short research report in markdown for a League of Legends player or coach, using ONLY the
findings and query ledger you're given.

- Start with "## Answer": 2-4 sentences that answer the question directly, with the key numbers.
- Then one "##" section per part of the question. Prefer a small markdown table when comparing groups.
- Cite every number with the query id(s) it came from, exactly as in the findings, e.g. [Q4] or [Q4][Q7].
  In a table, don't cite inside cells: put one line under the table, e.g. "Source: [Q4][Q7]".
  Cite each sentence once, at its end, not after every number.
  Never invent a number or a citation id; if a number isn't in the findings, don't state it.
- Give sample sizes, and don't call a difference meaningful if the findings say the intervals overlap
  or the sample is small.
- End with "## What this data can't tell us": the parts of the question the findings couldn't answer,
  and the dataset's relevant limits (one region, EUN1; mostly one week on patch 15.1; final inventory only,
  no timeline; ranks are a snapshot). Keep it to the limits that matter for this question.
- If the lead's note starts with CANNOT, write only a short explanation of why this data can't answer it
  and suggest 2 questions it can answer.
- No LaTeX or math markup: write n=411, not $n$.
- Items are the final inventory only: never describe build order, a "rush" or a "first item". Treat
  item win rates as correlation (late items appear in long and already-won games), not advice to buy them.
- Plain, specific sentences. No hype: no "dominant", "crucial", "pivotal", "it is worth noting".""",
    model=MODEL,
    model_settings=SETTINGS,
)


# --- Hooks: record every tool call with its agent, args and result, and stream them to the page ---


class TraceHooks(PanelHooks):
    def __init__(self, contexts: AgentContexts, emit, run: "Research | None" = None):
        super().__init__(contexts, emit)
        self.run = run

    async def on_tool_start(self, context, agent, tool):
        call_id = getattr(context, "tool_call_id", None) or str(uuid.uuid4())
        record = {
            "id": call_id,
            "agent": agent.name,
            "name": tool.name,
            "args": _arguments(getattr(context, "tool_arguments", None)),
            "result": None,
        }
        self.run.tool_calls.append(record)
        self.run.call_agent[call_id] = agent.name
        self.emit({"type": "tool_start", **record})

    async def on_tool_end(self, context, agent, tool, result):
        call_id = getattr(context, "tool_call_id", None)
        text = str(result)
        for record in reversed(self.run.tool_calls):
            if record["id"] == call_id or (call_id is None and record["name"] == tool.name and record["result"] is None):
                record["result"] = text
                call_id = record["id"]
                break
        self.emit({"type": "tool_end", "id": call_id, "agent": agent.name, "name": tool.name, "result": text[:12000]})


# --- One turn of the conversation ---

CITE = re.compile(r"\[((?:Q\d+\s*[,;]?\s*)+)\]")


def check_citations(report: str, ledger: dict[str, dict]) -> tuple[str, list[str], list[str]]:
    """Normalise [Q1, Q2] to [Q1][Q2], mark ids that aren't in the ledger, and list what was cited."""
    cited, unknown = [], []

    def fix(m):
        out = []
        for qid in re.findall(r"Q\d+", m.group(1)):
            if qid in ledger:
                out.append(f"[{qid}]")
                if qid not in cited:
                    cited.append(qid)
            else:
                out.append("[unverified]")
                unknown.append(qid)
        return "".join(out)

    return CITE.sub(fix, report), cited, unknown


def evidence_markdown(evidence: list[dict]) -> str:
    if not evidence:
        return ""
    parts = ["\n\n---\n## Evidence (the query behind each citation)\n"]
    for q in evidence:
        parts.append(f"**[{q['id']}]** {q['purpose']} ({q['row_count']} rows, by {q['agent']})\n```sql\n{q['sql']}\n```")
    return "\n".join(parts)


@dataclass
class Session:
    history: list[dict] = field(default_factory=list)  # {"role": "user"|"assistant", "text": ...}
    asked: bool = False  # the Lead asked a clarifying question and is waiting on the answer


def lead_prompt(session: Session, message: str) -> str:
    if not session.history:
        return f"Question: {message}"
    lines = []
    for turn in session.history[-6:]:
        text = turn["text"] if len(turn["text"]) < 1500 else turn["text"][:1500] + " [...]"
        lines.append(f"{'User' if turn['role'] == 'user' else 'You'}: {text}")
    return "Conversation so far:\n" + "\n\n".join(lines) + f"\n\nLatest message from the user: {message}"


async def deep_research(session: Session, message: str, hooks: TraceHooks) -> dict:
    run = Research(hooks=hooks)
    hooks.run = run

    # 1. Research: clarify or plan, fan out, check for gaps
    agent = lead_committed if session.asked else lead
    lead_note = ""
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, lead_prompt(session, message), context=run, hooks=hooks, max_turns=LEAD_MAX_TURNS),
            timeout=RESEARCH_DEADLINE_S,
        )
        lead_note = str(result.final_output or "")
    except asyncio.TimeoutError:
        hooks.activity("Lead", f"hit the {RESEARCH_DEADLINE_S}s research deadline: writing up what came back")
    except Exception as e:  # e.g. MaxTurnsExceeded: report what we have rather than nothing
        if not run.findings:
            raise
        hooks.activity("Lead", f"stopped early ({type(e).__name__}): writing up what came back")

    session.history.append({"role": "user", "text": message})
    if run.clarification and not run.findings:
        session.asked = True
        q = run.clarification
        text = q["question"] + ("\n\nFor example: " + "; ".join(q["options"]) if q["options"] else "")
        session.history.append({"role": "assistant", "text": f"(clarifying question) {text}"})
        return {"response": text, "clarification": q, "evidence": [], "tool_calls": run.tool_calls}
    session.asked = False

    # 2. Write: one call, outside the loop. The Writer sees only the question, findings and ledger.
    findings = "\n\n---\n\n".join(run.findings) or "(no findings)"
    ledger = "\n".join(f"{q['id']}: {q['purpose']} ({q['row_count']} rows)" for q in run.queries) or "(none)"
    question = lead_prompt(Session(history=session.history[:-1]), message)
    note = f"\n\nLead's note: {lead_note}" if lead_note.strip().upper().startswith("CANNOT") else ""
    report = await Runner.run(
        writer, f"{question}{note}\n\nQuery ledger:\n{ledger}\n\nFindings:\n\n{findings}", hooks=hooks
    )

    # 3. Check: every citation must point at a query that actually ran
    by_id = {q["id"]: q for q in run.queries}
    body, cited, unknown = check_citations(str(report.final_output), by_id)
    if unknown:
        hooks.activity("Writer", f"citation check: {len(unknown)} citation(s) not in the ledger, marked [unverified]")
    evidence = [by_id[qid] for qid in cited]
    session.history.append({"role": "assistant", "text": body})
    return {
        "response": body + evidence_markdown(evidence),
        "report": body,
        "clarification": None,
        "evidence": evidence,
        "queries": run.queries,
        "tool_calls": run.tool_calls,
    }


# --- FastAPI App ---

HERE = Path(__file__).parent

sessions: dict[str, Session] = {}  # session_id -> conversation, in memory
runs: dict[str, AgentContexts] = {}  # run_id -> every agent's context, for the context panel

app = FastAPI(title="Rift Recon")
add_context_panel(app, runs)


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
def info():
    return lol_data.STATS


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/chat")
async def chat(req: ChatRequest):
    """Project 1's response shape: response, session_id, and every tool call (with the agent that made it)."""
    session_id = req.session_id or str(uuid.uuid4())
    session = sessions.setdefault(session_id, Session())
    run_id = str(uuid.uuid4())
    runs[run_id] = AgentContexts()
    hooks = TraceHooks(runs[run_id], lambda event: None)
    out = await deep_research(session, req.message, hooks)
    return {
        "response": out["response"],
        "session_id": session_id,
        "needs_clarification": out["clarification"] is not None,
        "tool_calls": [{k: c[k] for k in ("agent", "name", "args", "result")} for c in out["tool_calls"]],
    }


@app.get("/run")
async def run(message: str, session_id: str = ""):
    """Same as /chat, but streams every agent step and tool call as Server-Sent Events."""
    session_id = session_id or str(uuid.uuid4())
    session = sessions.setdefault(session_id, Session())
    run_id = str(uuid.uuid4())
    runs[run_id] = AgentContexts()
    queue: asyncio.Queue = asyncio.Queue()
    hooks = TraceHooks(runs[run_id], queue.put_nowait)

    async def work():
        try:
            out = await deep_research(session, message, hooks)
            if out["clarification"]:
                queue.put_nowait({"type": "clarify", **out["clarification"]})
            else:
                queue.put_nowait(
                    {"type": "done", "markdown": out["report"], "evidence": out["evidence"], "queries": out["queries"]}
                )
        except Exception as e:
            # Auth, quota, a model that won't stop: show it on the page, not as a dropped stream.
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id, 'session_id': session_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # proxies drop silent streams
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error", "clarify"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
