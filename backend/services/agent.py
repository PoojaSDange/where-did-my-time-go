"""Supervisor agent: a Groq model with DIRECT tool calling (no LangChain / LangGraph).

Tools are deterministic analytics functions over BOTH history and live data. They return compact
evidence with estimated vs measured labelled. The model must not invent numbers; every answer
states when data is estimated (history) vs measured (extension) - enforced deterministically at the
end, not left to the model's memory. Loops are bounded (AGENT_MAX_STEPS) and tool results are
size-capped (AGENT_TOOL_RESULT_CHARS) to protect the token budget.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from config import settings
from models import CATEGORIES
from services import activity_storage as storage
from services import analytics_tools as at
from services import groq_client, timeutil

log = logging.getLogger("wdmt.agent")

RANGE_HELP = (
    "range: one of today, yesterday, this_week, last_week (weeks start Monday), last_7_days, last_30_days, "
    "this_month, last_month, all, a single date YYYY-MM-DD, or 'YYYY-MM-DD..YYYY-MM-DD' (local dates, inclusive)"
)


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties, "required": required}}}


_R = {"type": "string", "description": RANGE_HELP}
TOOLS = [
    _fn("get_category_time", "Total time in one category over a range, split measured vs estimated, with per-day values.",
        {"range": _R, "category": {"type": "string", "enum": list(CATEGORIES)}}, ["range", "category"]),
    _fn("top_distractions", "Biggest distractions (sessions flagged wasted) over a range, plus other social/entertainment time.",
        {"range": _R, "limit": {"type": "integer"}}, ["range"]),
    _fn("top_domains", "Most-used websites in a range, optionally within one category.",
        {"range": _R, "category": {"type": "string", "enum": list(CATEGORIES)}, "limit": {"type": "integer"}}, ["range"]),
    _fn("compare_periods", "Compare two ranges (e.g. this_week vs last_week): totals, wasted and per-category change.",
        {"range_a": _R, "range_b": _R}, ["range_a", "range_b"]),
    _fn("get_daily_summary", "Metrics and AI insight for one local day.", {"date": {"type": "string", "description": "YYYY-MM-DD"}}, ["date"]),
    _fn("get_monthly_summary", "Metrics and AI narrative for one month.", {"month": {"type": "string", "description": "YYYY-MM"}}, ["month"]),
    _fn("search_activity", "Find time spent on a website or keyword (matches domain or page title).",
        {"query": {"type": "string"}, "range": _R, "limit": {"type": "integer"}}, ["query"]),
    _fn("measured_vs_estimated", "How much of a range is measured live vs estimated from history, and classification coverage.",
        {"range": _R}, ["range"]),
]

# tool-argument name -> python keyword
_ARG_MAP = {"date": "date_str"}
_ALLOWED = {
    "get_category_time": {"range", "category"}, "top_distractions": {"range", "limit"},
    "top_domains": {"range", "category", "limit"}, "compare_periods": {"range_a", "range_b"},
    "get_daily_summary": {"date"}, "get_monthly_summary": {"month"},
    "search_activity": {"query", "range", "limit"}, "measured_vs_estimated": {"range"},
}


def system_prompt(tz_name: str, now: datetime) -> str:
    local = now.astimezone(timeutil.get_tz(tz_name))
    return (
        "You are the analyst inside 'Where Did My Time Go?', a private browsing-time tool. "
        f"Current local date/time: {local.strftime('%A %Y-%m-%d %H:%M')} (timezone {tz_name}). "
        "Answer ONLY from tool results: ALWAYS call tools first, never guess or invent numbers, and copy durations "
        "exactly as given (do not add up or convert them yourself). "
        "Two kinds of data exist: MEASURED (live extension tracking, accurate) and ESTIMATED (reconstructed from browser "
        "history before tracking began, approximate). State clearly which your answer rests on. "
        "'Wasted' means social/entertainment sessions flagged as distracting with enough evidence; a category alone is "
        "not waste, and news/shopping/YouTube learning are not automatically wasted. "
        "If data is missing or unclassified, say so plainly. Be concise: 2-5 sentences, plain language, second person. "
        "Suggest at most one small, specific change when the question is about improving."
    )


def truncate_result(obj: Any, limit: int) -> str:
    """JSON-serialise, shrinking the longest lists until it fits (protects the token budget)."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    guard = 0
    while len(s) > limit and guard < 40:
        guard += 1
        best = _longest_list(obj)
        if best is None or len(best) <= 1:
            break
        del best[len(best) // 2:]
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= limit else s[: limit - 20] + '..."truncated"'


def _longest_list(o: Any) -> Optional[list]:
    best: Optional[list] = None
    stack = [o]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            if best is None or len(cur) > len(best):
                best = cur
            stack.extend(cur)
    return best


def run_tool(name: str, args: dict, tz, now: datetime) -> dict:
    fn = at.TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    allowed = _ALLOWED.get(name, set())
    kwargs = {_ARG_MAP.get(k, k): v for k, v in (args or {}).items() if k in allowed}
    try:
        return fn(tz=tz, now=now, **kwargs)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception:  # noqa: BLE001 - a tool bug must not kill the conversation
        log.exception("tool %s failed", name)
        return {"error": "tool failed"}


def _default_chat(messages: list[dict], tools: Optional[list]) -> dict:
    resp = groq_client.chat(
        messages, priority=groq_client.PRIORITY_SUPERVISOR, model=settings.groq_agent_model,
        max_tokens=settings.agent_max_tokens, temperature=0.2, tools=tools,
    )
    try:
        return resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise groq_client.GroqError("empty response from Groq")


def _data_note(used_estimated: bool, used_measured: bool) -> str:
    if used_estimated and used_measured:
        return ("Data note: this mixes MEASURED time (live extension tracking) with ESTIMATED time reconstructed from "
                "your browser history before tracking started.")
    if used_estimated:
        return "Data note: these figures are ESTIMATES reconstructed from your browser history (not live-measured)."
    if used_measured:
        return "Data note: these figures are MEASURED live by the extension."
    return ""


def _mentions_kind(text: str) -> bool:
    t = text.lower()
    return "estimate" in t or "measured" in t


def _scan_kinds(result: dict, flags: dict) -> None:
    """Tools report which data kinds contributed (computed from real seconds, not text sniffing)."""
    kinds = result.get("kinds") if isinstance(result, dict) else None
    if isinstance(kinds, dict):
        flags["estimated"] = flags["estimated"] or bool(kinds.get("estimated"))
        flags["measured"] = flags["measured"] or bool(kinds.get("measured"))


def ask(question: str, history: Optional[list[dict]] = None, *, chat_fn: Optional[Callable] = None,
        now: Optional[datetime] = None) -> dict:
    """Answer a question about the user's browsing. Returns {answer, evidence, tools_used, steps}."""
    question = (question or "").strip()[:500]
    if not question:
        return {"answer": "Ask me something about your browsing time.", "evidence": [], "tools_used": [], "steps": 0}
    now = now or timeutil.utcnow()
    tz_name = storage.get_tz_name()
    tz = timeutil.get_tz(tz_name)
    chat_fn = chat_fn or _default_chat

    messages: list[dict] = [{"role": "system", "content": system_prompt(tz_name, now)}]
    for h in (history or [])[-4:]:
        role = h.get("role")
        if role in ("user", "assistant") and isinstance(h.get("content"), str):
            messages.append({"role": role, "content": h["content"][:400]})
    messages.append({"role": "user", "content": question})

    evidence: list[str] = []
    tools_used: list[str] = []
    flags = {"estimated": False, "measured": False}
    nudged = False
    final_text: Optional[str] = None
    steps = 0

    for steps in range(1, settings.agent_max_steps + 1):
        last_step = steps == settings.agent_max_steps
        msg = chat_fn(messages, None if last_step else TOOLS)
        calls = msg.get("tool_calls") or []
        if not calls or last_step:
            text = (msg.get("content") or "").strip()
            if not calls and not tools_used and not nudged and not last_step:
                nudged = True  # answering without evidence: force it to look at the data first
                messages.append({"role": "assistant", "content": text or "(no answer)"})
                messages.append({"role": "user", "content": "You must call the tools to get real data before answering."})
                continue
            final_text = text
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls[:3]:  # cap parallel calls per step
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = run_tool(name, args if isinstance(args, dict) else {}, tz, now)
            tools_used.append(name)
            for line in result.get("evidence", []) if isinstance(result, dict) else []:
                if line not in evidence:
                    evidence.append(line)
            if isinstance(result, dict):
                _scan_kinds(result, flags)
            payload = {k: v for k, v in result.items() if k not in ("evidence", "kinds")} if isinstance(result, dict) else result
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name,
                             "content": truncate_result(payload, settings.agent_tool_result_chars)})
    if final_text is None:  # ran out of steps while still calling tools
        messages.append({"role": "user", "content": "Answer now using only the data you already have."})
        final_text = (chat_fn(messages, None).get("content") or "").strip()

    answer = final_text or "I couldn't put together an answer from your data."
    note = _data_note(flags["estimated"], flags["measured"])
    if note and not _mentions_kind(answer):
        answer = f"{answer}\n\n{note}"
    return {"answer": answer, "evidence": evidence[:8], "tools_used": tools_used, "steps": steps}
