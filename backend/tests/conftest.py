"""全部测试共用的设置。"""

import pytest

import app.traces


@pytest.fixture(autouse=True)
def isolate_traces(monkeypatch, tmp_path):
    """测试不受本机 .env 里 SAVE_WEB_TRACES 的影响，也绝不往真实的 traces/ 写文件。"""
    monkeypatch.setenv("SAVE_WEB_TRACES", "false")
    monkeypatch.setattr(app.traces, "TRACES_DIR", tmp_path / "traces")
    return tmp_path / "traces"
