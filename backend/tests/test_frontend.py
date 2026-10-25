"""前端页面由 FastAPI 托管：页面能打开、静态文件可访问、不覆盖接口；前端不向服务器发送创作者姓名。"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
client = TestClient(app)


def test_index_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>DealCheck</title>" in response.text
    assert "fonts.googleapis.com" in response.text


def test_static_assets_served():
    assert client.get("/app.js").status_code == 200
    assert "--accent: #C8F066" in client.get("/styles.css").text


def test_api_routes_not_shadowed_by_static_mount():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/status").status_code == 200


def test_frontend_never_sends_creator_name():
    """apiProfile 只挑出接口需要的字段，不包含 name；邮件署名在本地替换。"""
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    api_profile = re.search(r"function apiProfile\(p\) \{(.*?)\n\}", js, re.S).group(1)
    assert "p.name" not in api_profile and "name:" not in api_profile
    assert "SIGNATURE_PLACEHOLDER" in js and "[Your name]" in js


def test_no_send_button_in_frontend():
    js = (FRONTEND / "app.js").read_text(encoding="utf-8").lower()
    assert "send email" not in js and "mailto:" not in js
    assert "never sends emails" in js


def test_no_verdict_while_research_is_running():
    """调查进行中显示中性状态；只有调查完成、预检命中拒绝类规则或仅规则模式时才显示结论。"""
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "Checking the brand…" in js
    assert "isSettled(run, ev) ? decisionCard(ev, idMap) : pendingCard(ev)" in js
    settled = re.search(r"function isSettled\(run, ev\) \{(.*?)\n\}", js, re.S).group(1)
    assert 'run.phase === "done"' in settled and '"reject"' in settled and "agent_unavailable" in settled


def test_readable_adjustment_names():
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'brand_reputation: "Brand reputation", values_fit: "Values fit", peer_rate: "Peer rates"' in js
    assert 'a.item.replace("_", " ")' not in js
