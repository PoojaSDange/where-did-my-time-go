import json
from datetime import timedelta

import pytest

import database as db
from config import settings
from conftest import dt
from services import agent, groq_client, timeutil
from test_phase4_analytics import add, seed_day

NOW = dt("2026-09-29T06:00:00Z")


def script(*steps):
    """Fake Groq chat: each step is a message dict (tool_calls or final content)."""
    seq = list(steps)
    seen = []
    def chat(messages, tools):
        seen.append({"messages": [dict(m) for m in messages], "tools": tools})
        return seq.pop(0) if seq else {"content": "done"}
    chat.seen = seen
    return chat


def tc(name, args, i="c1"):
    return {"id": i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_agent_calls_tools_then_answers_and_labels_estimated_vs_measured():
    seed_day()
    chat = script({"content": "", "tool_calls": [tc("measured_vs_estimated", {"range": "2026-09-27"})]},
                  {"content": "Most of that day was tracked live."})
    r = agent.ask("How much was measured yesterday?", chat_fn=chat, now=NOW)
    assert r["tools_used"] == ["measured_vs_estimated"] and r["evidence"]
    assert "MEASURED" in r["answer"] and "ESTIMATES" in r["answer"] or "mixes" in r["answer"]   # deterministic disclosure appended
    tool_msg = [m for m in chat.seen[-1]["messages"] if m["role"] == "tool"][0]
    assert "evidence" not in tool_msg["content"] and "1h" in tool_msg["content"] or "measured" in tool_msg["content"]


def test_agent_receives_current_date_and_timezone():
    chat = script({"content": "hi", "tool_calls": []})
    agent.ask("hello", chat_fn=script({"content": "", "tool_calls": [tc("measured_vs_estimated", {"range": "today"})]}, {"content": "ok"}), now=NOW)
    c = script({"content": "", "tool_calls": [tc("measured_vs_estimated", {"range": "today"})]}, {"content": "ok"})
    agent.ask("hello", chat_fn=c, now=NOW)
    sys = c.seen[0]["messages"][0]["content"]
    assert "2026-09-29" in sys and "Asia/Kolkata" in sys and "Tuesday" in sys


def test_agent_forced_to_use_tools_before_answering():
    c = script({"content": "You spent 4 hours."},                       # tries to answer with no evidence
               {"content": "", "tool_calls": [tc("top_distractions", {"range": "today"})]}, {"content": "Nothing flagged."})
    r = agent.ask("distractions?", chat_fn=c, now=NOW)
    assert r["tools_used"] == ["top_distractions"] and "4 hours" not in r["answer"]


def test_agent_step_limit_and_final_call_has_no_tools(monkeypatch):
    monkeypatch.setattr(settings, "agent_max_steps", 3)
    looping = script(*[{"content": "", "tool_calls": [tc("measured_vs_estimated", {"range": "today"}, f"c{i}")]} for i in range(10)])
    r = agent.ask("loop forever", chat_fn=looping, now=NOW)
    assert r["steps"] <= 3 and len(looping.seen) <= 4
    assert looping.seen[2]["tools"] is None                            # last step: tools withdrawn -> must answer


def test_agent_tool_errors_are_returned_not_raised():
    c = script({"content": "", "tool_calls": [tc("get_category_time", {"range": "today", "category": "nope"}),
                                            tc("does_not_exist", {}), tc("get_daily_summary", {"date": "bad"})]},
               {"content": "I could not look that up."})
    r = agent.ask("x", chat_fn=c, now=NOW)
    tools = [m["content"] for m in c.seen[-1]["messages"] if m["role"] == "tool"]
    assert len(tools) == 3 and all("error" in t for t in tools)
    assert r["answer"]


def test_tool_results_are_size_capped(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_result_chars", 600)
    for i in range(60):
        add(i, f"2026-09-27T{4 + i // 30:02d}:{(i % 30) * 2:02d}:00Z", 60 + i, f"site{i}.example.com", "research")
    c = script({"content": "", "tool_calls": [tc("search_activity", {"query": "site", "range": "all", "limit": 15})]}, {"content": "ok"})
    agent.ask("x", chat_fn=c, now=NOW)
    tool = [m["content"] for m in c.seen[-1]["messages"] if m["role"] == "tool"][0]
    assert len(tool) <= 620


def test_agent_over_history_plus_live_and_no_sensitive_leak():
    seed_day()
    add(80, "2026-09-27T11:00:00Z", 500, "hsbc.com", "personal", sensitive=1, title="Statement 99887766")
    c = script({"content": "", "tool_calls": [tc("search_activity", {"query": "hsbc", "range": "all"}),
                                            tc("top_domains", {"range": "2026-09-27", "limit": 20}),
                                            tc("get_category_time", {"range": "all", "category": "entertainment"})]},
               {"content": "ok"})
    agent.ask("x", chat_fn=c, now=NOW)
    blob = " ".join(m["content"] for m in c.seen[-1]["messages"] if m["role"] == "tool")
    assert "hsbc.com" not in blob and "99887766" not in blob and "Statement" not in blob and "[sensitive]" in blob
    assert "estimated_seconds\":600" in blob                            # history rows visible to the agent too


def test_agent_no_data_says_so_and_empty_question():
    c = script({"content": "", "tool_calls": [tc("get_daily_summary", {"date": "2026-01-01"})]}, {"content": "There is no data for that day."})
    r = agent.ask("what happened Jan 1?", chat_fn=c, now=NOW)
    assert "no data" in r["answer"].lower() and "Data note" not in r["answer"]
    assert agent.ask("   ")["tools_used"] == []


def test_agent_budget_exhausted_propagates_for_route_to_report(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "groq_tpd", 1000); monkeypatch.setattr(settings, "groq_supervisor_reserve_tokens", 0)
    db.set_state("groq_daily", {"date": groq_client._today_key(), "used": 999})
    with pytest.raises(groq_client.BudgetExhausted):
        agent.ask("hello")
