"""The fake Ollama server speaks enough of the real API for ChatOllama, OllamaEmbeddings and with_structured_output."""

import json
import urllib.error
import urllib.request

import httpx
import ollama
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama, OllamaEmbeddings
from pydantic import BaseModel

from rectifi.agent import llm
from support.fakes import bow_vector


class Pick(BaseModel):
    category: str
    confidence: int


@tool
def ping(x: int) -> str:
    """Ping."""
    return str(x)


def get(fake, path):
    return json.load(urllib.request.urlopen(fake.base_url + path, timeout=5))


def test_version_tags_show_ps(fake_ollama):
    assert get(fake_ollama, "/api/version")["version"]
    names = [m["name"] for m in get(fake_ollama, "/api/tags")["models"]]
    assert "qwen2.5:7b" in names and "nomic-embed-text:latest" in names
    req = urllib.request.Request(fake_ollama.base_url + "/api/show", data=json.dumps({"model": "qwen2.5:7b"}).encode(),
                                 headers={"Content-Type": "application/json"})
    assert "tools" in json.load(urllib.request.urlopen(req, timeout=5))["capabilities"]
    fake_ollama.loaded = ["qwen2.5:7b"]
    assert get(fake_ollama, "/api/ps")["models"][0]["name"] == "qwen2.5:7b"


def test_chat_ollama_invoke_and_stream(fake_ollama):
    model = ChatOllama(model="qwen2.5:7b", base_url=fake_ollama.base_url)
    fake_ollama.reply("Hello there friend")
    assert model.invoke("hi").content == "Hello there friend"
    fake_ollama.reply("one two three")
    assert "".join(c.content for c in model.stream("hi")) == "one two three"
    assert fake_ollama.queue == []
    fake_ollama.reply("not scripted next")
    fake_ollama.queue.clear()
    assert model.invoke("hi").content == "OK"          # an empty queue answers OK


def test_tool_calls_come_back(fake_ollama):
    model = ChatOllama(model="qwen2.5:7b", base_url=fake_ollama.base_url).bind_tools([ping])
    fake_ollama.reply_tool_call("ping", {"x": 1})
    reply = model.invoke("call ping with x=1")
    assert reply.tool_calls and reply.tool_calls[0]["name"] == "ping" and reply.tool_calls[0]["args"] == {"x": 1}
    assert fake_ollama.bodies("/api/chat")[-1]["tools"]


def test_with_structured_output_sends_a_schema_and_parses_the_json(fake_ollama):
    model = ChatOllama(model="qwen2.5:3b", base_url=fake_ollama.base_url).with_structured_output(Pick)
    fake_ollama.reply_json({"category": "Fuel", "confidence": 91})
    result = model.invoke([HumanMessage("HP petrol pump")])
    assert result == Pick(category="Fuel", confidence=91)
    sent = fake_ollama.bodies("/api/chat")[-1]
    assert isinstance(sent["format"], dict) and "category" in sent["format"]["properties"]


def test_embeddings_are_bag_of_words(fake_ollama):
    emb = OllamaEmbeddings(model="nomic-embed-text", base_url=fake_ollama.base_url)
    vectors = emb.embed_documents(["rent paid to landlord", "coffee at the cafe"])
    assert vectors[0] == pytest.approx(bow_vector("rent paid to landlord"))
    query = emb.embed_query("landlord rent")
    cos = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert cos(query, vectors[0]) > cos(query, vectors[1])
    assert fake_ollama.bodies("/api/embed")[0]["input"] == ["rent paid to landlord", "coffee at the cafe"]


def test_the_rectifi_factories_work_against_it(fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "qwen2.5:3b")
    fake_ollama.reply("fine")
    assert llm.get_llm("classify").invoke("hi").content == "fine"
    assert fake_ollama.bodies("/api/chat")[-1]["model"] == "qwen2.5:3b"
    assert len(llm.get_embeddings().embed_query("hello")) == 64
    ok, _why = llm.llm_status()
    assert ok


def test_unknown_model_is_a_404(fake_ollama):
    with pytest.raises(ollama.ResponseError) as err:
        ChatOllama(model="nope:1b", base_url=fake_ollama.base_url).invoke("hi")
    assert err.value.status_code == 404 and "not found" in str(err.value)


def test_a_model_without_tools_is_a_400(fake_ollama):
    fake_ollama.add_model("tiny:1b", ["completion"])
    with pytest.raises(ollama.ResponseError) as err:
        ChatOllama(model="tiny:1b", base_url=fake_ollama.base_url).bind_tools([ping]).invoke("hi")
    assert err.value.status_code == 400 and "does not support tools" in str(err.value)
    fake_ollama.reply("plain works")                        # the same model is fine without tools
    assert ChatOllama(model="tiny:1b", base_url=fake_ollama.base_url).invoke("hi").content == "plain works"


def test_injected_errors_and_delays(fake_ollama):
    fake_ollama.fail_next(500, "boom")
    with pytest.raises(ollama.ResponseError) as err:
        ChatOllama(model="qwen2.5:7b", base_url=fake_ollama.base_url).invoke("hi")
    assert err.value.status_code == 500 and "boom" in str(err.value)
    fake_ollama.delay = 1.0
    slow = ChatOllama(model="qwen2.5:7b", base_url=fake_ollama.base_url, client_kwargs={"timeout": 0.3})
    with pytest.raises(httpx.TimeoutException):
        slow.invoke("hi")


def test_the_server_is_local_and_stops_after_the_test(fake_ollama):
    assert fake_ollama.base_url.startswith("http://127.0.0.1:")
    assert fake_ollama.calls == [] or fake_ollama.calls[0][0] in ("GET", "POST")


def test_server_is_down_after_stop(fake_ollama):
    url = fake_ollama.base_url
    fake_ollama.stop()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url + "/api/version", timeout=2)


@pytest.mark.ollama
def test_live_ollama_is_reachable():
    """Skipped unless you run `pytest -m ollama` with a real Ollama running."""
    ok, why = llm.llm_status()
    assert ok, why
