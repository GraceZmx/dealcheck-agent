"""运行记录测试：网页评估按 SAVE_WEB_TRACES 开关保存记录，格式和终端版一致，不含创作者姓名或 Handle。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.llm.mock import MockProvider
from app.main import app, get_providers
from app.protection import UsageGuard, load_protection_config
from app.traces import save_trace
from tests.test_pipeline import agent_script, request, search

ROOT = Path(__file__).resolve().parents[2]
NAME, HANDLE = "Maya Secretname", "@maya_secret_handle"


@pytest.fixture
def client():
    original = app.state.guard
    app.state.guard = UsageGuard(load_protection_config())
    app.dependency_overrides[get_providers] = lambda: (MockProvider(agent_script()), search(), None)
    yield TestClient(app)
    app.state.guard = original
    app.dependency_overrides.clear()


def _body():
    raw = request(creator_name=NAME).model_dump(mode="json")
    raw["handle"] = HANDLE  # 接口不认识的字段会被忽略，也不能出现在记录里
    return raw


def _files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json")) if directory.exists() else []


def test_switch_off_writes_nothing(client, isolate_traces):
    assert client.post("/api/evaluate", json=_body()).status_code == 200
    with client.stream("POST", "/api/evaluate/stream", json=_body()) as response:
        "".join(response.iter_text())
    assert _files(isolate_traces) == []


def test_switch_on_saves_web_trace_without_pii(client, isolate_traces, monkeypatch):
    monkeypatch.setenv("SAVE_WEB_TRACES", "true")
    response = client.post("/api/evaluate", json=_body())
    assert response.status_code == 200
    assert NAME in response.json()["email"]["body"]  # 返回给用户的邮件不受影响

    [path] = _files(isolate_traces)
    assert "-web-glow-theory" in path.name
    text = path.read_text(encoding="utf-8")
    assert "Secretname" not in text and "maya_secret_handle" not in text
    saved = json.loads(text)
    assert set(saved) == {"result", "trace"}
    assert "[Your name]" in saved["result"]["email"]["body"]
    assert any(step["type"] == "tool" for step in saved["trace"])


def test_switch_on_stream_endpoint_also_saves(client, isolate_traces, monkeypatch):
    monkeypatch.setenv("SAVE_WEB_TRACES", "true")
    with client.stream("POST", "/api/evaluate/stream", json=_body()) as response:
        assert "event: result" in "".join(response.iter_text())
    [path] = _files(isolate_traces)
    assert "-web-" in path.name and "Secretname" not in path.read_text(encoding="utf-8")


def test_trace_write_failure_does_not_break_evaluation(client, monkeypatch):
    monkeypatch.setenv("SAVE_WEB_TRACES", "true")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("app.main.save_trace", broken)
    assert client.post("/api/evaluate", json=_body()).status_code == 200


def test_cli_trace_has_no_tag_and_same_format(isolate_traces):
    from app.pipeline import run_full_evaluation

    result, trace = run_full_evaluation(request(), MockProvider(agent_script()), search())
    first = save_trace(result, trace, "Glow Theory Skincare")
    second = save_trace(result, trace, "Glow Theory Skincare")
    assert "-web-" not in first.name and first.name.endswith("-glow-theory-skincare.json")
    assert second != first  # 同一秒内不覆盖
    assert set(json.loads(first.read_text(encoding="utf-8"))) == {"result", "trace"}


def test_traces_dir_is_gitignored():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "traces/" in lines
