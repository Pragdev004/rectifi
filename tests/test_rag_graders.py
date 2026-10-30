"""The prompted graders: DocGrade (Pydantic), GroundCheck (TypedDict + Annotated), UseCheck and RouteDecision (JSON schema)."""

import pytest
from langchain_ollama import ChatOllama

from rag_support import RuleModel
from rectifi.rag import graders
from rectifi.rag.graders import DocGrade, GroundCheck, ROUTE_SCHEMA, USE_CHECK_SCHEMA


def test_docgrade_is_a_pydantic_model_and_normalises():
    llm = RuleModel(structured={"DocGrade": lambda t: {"relevance": "yes", "reason": "has the answer"}})
    assert graders.grade_document(llm, "q", "passage") == {"grade": "yes", "reason": "has the answer"}
    bad = RuleModel(structured={"DocGrade": lambda t: {"relevance": "maybe"}})        # pydantic rejects it -> unsure, never a silent pass or fail
    assert graders.grade_document(bad, "q", "p")["grade"] == "partial"
    boom = RuleModel(structured={"DocGrade": lambda t: RuntimeError("model down")})
    out = graders.grade_document(boom, "q", "p")
    assert out["grade"] == "partial" and "unavailable" in out["reason"]
    with pytest.raises(Exception):
        DocGrade(relevance="maybe")


def test_groundcheck_typeddict():
    assert set(GroundCheck.__annotations__) == {"supported", "unsupported_claims"}
    ok = RuleModel(structured={"GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": []}})
    assert graders.check_groundedness(ok, "q", "a", "ctx") == {"supported": "fully", "unsupported": []}
    part = RuleModel(structured={"GroundCheck": lambda t: {"supported": "partially", "unsupported_claims": ["claim one", " "]}})
    assert graders.check_groundedness(part, "q", "a", "ctx")["unsupported"] == ["claim one"]
    contradict = RuleModel(structured={"GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": ["but this"]}})
    assert graders.check_groundedness(contradict, "q", "a", "c")["supported"] == "partially"
    junk = RuleModel(structured={"GroundCheck": lambda t: {"supported": "banana", "unsupported_claims": []}})
    assert graders.check_groundedness(junk, "q", "a", "c")["supported"] == "partially"
    boom = RuleModel(structured={"GroundCheck": lambda t: RuntimeError("x")})
    assert graders.check_groundedness(boom, "q", "a", "c")["supported"] == "partially"


def test_usecheck_json_schema():
    assert USE_CHECK_SCHEMA["required"] == ["useful", "score"] and ROUTE_SCHEMA["properties"]["route"]["enum"]
    llm = RuleModel(structured={"UseCheck": lambda t: {"useful": False, "score": 2}})
    assert graders.check_usefulness(llm, "q", "a") == {"useful": False, "score": 2}
    unsure = RuleModel(structured={"UseCheck": lambda t: {"score": 3}})
    assert graders.check_usefulness(unsure, "q", "a")["useful"] is None


def test_graders_work_with_a_real_chat_model_over_http(fake_ollama):
    """with_structured_output on ChatOllama: a Pydantic class, a TypedDict and a JSON-schema dict all reach the (fake) server."""
    model = ChatOllama(model="qwen2.5:7b", base_url=fake_ollama.base_url)
    fake_ollama.reply_json({"relevance": "yes", "reason": "on topic"})
    assert graders.grade_document(model, "q", "p")["grade"] == "yes"
    fake_ollama.reply_json({"supported": "fully", "unsupported_claims": []})
    assert graders.check_groundedness(model, "q", "a", "c")["supported"] == "fully"
    fake_ollama.reply_json({"useful": True, "score": 5})
    assert graders.check_usefulness(model, "q", "a") == {"useful": True, "score": 5}
    fake_ollama.reply_json({"route": "retrieve", "reason": "general"})
    assert graders.decide_route(model, "tell me about index funds")["route"] == "retrieve"
    assert all(b.get("format") for b in fake_ollama.bodies("/api/chat"))           # a JSON schema was sent every time


@pytest.mark.parametrize("question,expected", [
    ("", "decline"), ("thanks!", "direct"), ("hello", "direct"),
    ("how much did I spend on food last month?", "tools"), ("what did I pay for rent", "tools"), ("am I over my budget this month", "tools"),
    ("how should I budget my salary?", None), ("what is a SIP?", None), ("how do I stay safe on UPI", None), ("explain minimum due", None),
])
def test_pre_router_rules(question, expected):
    assert graders.pre_route(question) == expected


def test_decide_route_rules_first_then_model_then_fallback():
    llm = RuleModel(structured={"RouteDecision": lambda t: {"route": "decline", "reason": "not about money"}})
    assert graders.decide_route(llm, "what is the capital of France") == {"route": "decline", "reason": "not about money", "by": "model"}
    assert graders.decide_route(llm, "how much did I spend on food")["by"] == "rule"
    assert len(llm.calls("structured", "RouteDecision")) == 1                       # the rule answered the second one; the model ran once
    weird = RuleModel(structured={"RouteDecision": lambda t: {"route": "teleport"}})
    assert graders.decide_route(weird, "zxcv")["route"] == "retrieve"
    down = RuleModel(structured={"RouteDecision": lambda t: RuntimeError("down")})
    out = graders.decide_route(down, "something unclear")
    assert out["route"] == "retrieve" and out["by"] == "fallback"
