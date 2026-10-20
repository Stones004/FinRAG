"""Step 2: the OpenAI-compatible client (mocked HTTP), the schema checker and the fake."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import yaml

from finrag.core.config import load_settings
from finrag.llm.base import LLMInvalidOutput, LLMUnavailable
from finrag.llm.fake import FakeLLM, prompt_key
from finrag.llm.openai_compat import OpenAICompatClient
from finrag.llm.schema import schema_errors

SCHEMA = {
    "type": "object",
    "properties": {"entity": {"type": "string"}, "confidence": {"enum": ["high", "low"]}},
    "required": ["entity", "confidence"],
    "additionalProperties": False,
}


def settings_with_model(isolated_repo, model: str = "qwen3-4b", retries: int = 1):
    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["llm"]["model"] = model
    data["llm"]["retries"] = retries
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_settings(path, root=isolated_repo).llm


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class Server:
    """A scripted server: a list of handlers, one per request."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append({"path": request.url.path, "body": body})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def client(self, settings) -> OpenAICompatClient:
        return OpenAICompatClient(settings, transport=httpx.MockTransport(self))


def test_plain_generate_sends_openai_chat_body(isolated_repo) -> None:
    server = Server(reply("hello"))
    out = server.client(settings_with_model(isolated_repo)).generate("sys", "hi")
    assert out.text == "hello" and out.model == "qwen3-4b" and out.data is None
    sent = server.requests[0]
    assert sent["path"].endswith("/chat/completions")
    assert sent["body"]["stream"] is False and sent["body"]["temperature"] == 0
    assert [m["role"] for m in sent["body"]["messages"]] == ["system", "user"]
    assert "num_ctx" not in sent["body"] and "response_format" not in sent["body"]


def test_schema_request_uses_response_format(isolated_repo) -> None:
    server = Server(reply('{"entity": "GABRIEL", "confidence": "high"}'))
    out = server.client(settings_with_model(isolated_repo)).generate("s", "p", SCHEMA)
    assert out.data == {"entity": "GABRIEL", "confidence": "high"}
    fmt = server.requests[0]["body"]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["schema"] == SCHEMA


def test_response_format_rejected_retries_once_with_schema_in_prompt(isolated_repo) -> None:
    server = Server(
        httpx.Response(400, json={"error": "response_format not supported"}),
        reply('{"entity": "NETWEB", "confidence": "low"}'),
    )
    out = server.client(settings_with_model(isolated_repo)).generate("s", "p", SCHEMA)
    assert out.data["entity"] == "NETWEB"
    second = server.requests[1]["body"]
    assert "response_format" not in second
    assert '"required"' in second["messages"][1]["content"]
    assert len(server.requests) == 2


def test_invalid_json_twice_is_discarded(isolated_repo) -> None:
    server = Server(reply("not json"), reply('{"entity": 3}'))
    with pytest.raises(LLMInvalidOutput):
        server.client(settings_with_model(isolated_repo)).generate("s", "p", SCHEMA)
    assert len(server.requests) == 2


def test_invalid_then_valid_json_succeeds(isolated_repo) -> None:
    server = Server(reply("oops"), reply('{"entity": "x", "confidence": "high"}'))
    out = server.client(settings_with_model(isolated_repo)).generate("s", "p", SCHEMA)
    assert out.data["entity"] == "x"


def test_connection_refused_is_unavailable_after_retries(isolated_repo) -> None:
    server = Server(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    with pytest.raises(LLMUnavailable, match="not reachable"):
        server.client(settings_with_model(isolated_repo, retries=1)).generate("s", "p")
    assert len(server.requests) == 2  # 1 + retries


def test_server_error_retried_then_succeeds(isolated_repo) -> None:
    server = Server(httpx.Response(503), reply("ok"))
    assert server.client(settings_with_model(isolated_repo)).generate("s", "p").text == "ok"


def test_blank_model_is_unavailable_without_a_request(isolated_repo) -> None:
    server = Server()
    blank = settings_with_model(isolated_repo, model="")  # force blank whatever settings name
    client = OpenAICompatClient(blank, transport=httpx.MockTransport(server))
    assert blank.model == ""
    with pytest.raises(LLMUnavailable, match="blank"):
        client.generate("s", "p")
    assert client.ping() is False
    assert server.requests == []


def test_ping_checks_the_model_is_listed(isolated_repo) -> None:
    listing = httpx.Response(200, json={"data": [{"id": "qwen3-4b"}, {"id": "other"}]})
    assert Server(listing).client(settings_with_model(isolated_repo)).ping() is True
    listing = httpx.Response(200, json={"data": [{"id": "other"}]})
    assert Server(listing).client(settings_with_model(isolated_repo)).ping() is False
    down = Server(httpx.ConnectError("refused"))
    assert down.client(settings_with_model(isolated_repo)).ping() is False


def test_schema_errors() -> None:
    assert schema_errors({"entity": "a", "confidence": "high"}, SCHEMA) == []
    assert schema_errors({"entity": "a"}, SCHEMA)
    assert schema_errors({"entity": "a", "confidence": "mid"}, SCHEMA)
    assert schema_errors({"entity": "a", "confidence": "low", "x": 1}, SCHEMA)
    assert schema_errors([], SCHEMA)
    assert schema_errors({"entity": True, "confidence": "low"}, SCHEMA)
    assert schema_errors({"n": True}, {"type": "object", "properties": {"n": {"type": "integer"}}})


def test_fake_llm_lookup_order_and_failure_modes() -> None:
    fake = FakeLLM(
        canned={prompt_key("s", "p"): "by hash"}, rules=[("needle", "by rule")], default="dflt"
    )
    assert fake.generate("s", "p").text == "by hash"
    assert fake.generate("s", "has needle inside").text == "by rule"
    assert fake.generate("s", "other").text == "dflt"
    assert len(fake.calls) == 3
    with pytest.raises(LLMUnavailable):
        FakeLLM().generate("s", "p")
    down = FakeLLM(default="x", available=False)
    assert down.ping() is False
    with pytest.raises(LLMUnavailable):
        down.generate("s", "p")
    with pytest.raises(LLMInvalidOutput):
        FakeLLM(default="not json").generate("s", "p", SCHEMA)
    ok = FakeLLM(default='{"entity": "a", "confidence": "low"}').generate("s", "p", SCHEMA)
    assert ok.data["confidence"] == "low"
