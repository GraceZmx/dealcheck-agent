"""提交前检查测试：品牌名单收集、匹配规则、禁止提交的路径、暂存区和提交信息扫描。只用虚构品牌名。"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_brands.py"
spec = importlib.util.spec_from_file_location("check_brands", SCRIPT)
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def write_case(local: Path, sub: str, name: str, brand: str, website: str | None = None) -> None:
    folder = local / sub
    folder.mkdir(parents=True, exist_ok=True)
    deal = {"brand_name": brand, **({"brand_website": website} if website else {})}
    (folder / name).write_text(json.dumps({"deal": deal}), encoding="utf-8")


def labels(brands) -> set[str]:
    return {b.label for b in brands}


def hits(text: str, *names: str) -> bool:
    brands = [cb.make_brand(n) for n in names]
    return bool(cb.scan_text(text, brands, "x"))


# ---------- 品牌名单 ----------


def test_load_brands_from_cases_blocklist_and_domains(tmp_path):
    local = tmp_path / "local"
    write_case(local, "eval/cases", "R01.json", "Fernwick Loop", "https://www.fernwick-shop.example/about")
    write_case(local, "real_deals", "1_case.json", "Quillberry Foods")
    (local / "brand_blocklist.txt").write_text("# 注释\n\nOrchard Vale Holdings\ncase:Tuesday\nQx\n", encoding="utf-8")
    brands, notes = cb.load_brands(local)
    assert labels(brands) == {"Fernwick Loop", "fernwick-shop", "Quillberry Foods", "Orchard Vale Holdings", "Tuesday"}
    assert any("Qx" in n for n in notes)  # 太短的名字忽略并提示


def test_empty_local_dir_gives_empty_list(tmp_path):
    assert cb.load_brands(tmp_path / "local") == ([], [])


# ---------- 匹配规则 ----------


@pytest.mark.parametrize("text", [
    "Fernwick Loop", "fernwick loop", "FERNWICK-LOOP", "fernwick_loop", "FernwickLoop",
    "docs/3_fernwick_loop.json", "traces/20261026-web-fernwick-loop.json", "(Fernwick Loop) had 3 sources",
])
def test_brand_variants_are_caught(text):
    assert hits(text, "Fernwick Loop")


@pytest.mark.parametrize("text", ["Fernwickloopers", "glowing skin", "Gaming and Fashion niches", "fernwick"])
def test_whole_words_only_and_generic_words_pass(text):
    assert not hits(text, "Fernwick Loop", "Glow")


def test_case_sensitive_entry():
    brand = cb.make_brand("Tuesday", case_sensitive=True)
    assert cb.scan_text("Brand Tuesday paid late", [brand], "x")
    assert not cb.scan_text("posted on tuesday", [brand], "x")


# ---------- 禁止提交的路径 ----------


@pytest.mark.parametrize("path", [".env", ".env.local", "backend/.env", ".venv/lib/x.py",
                                  "local/eval/cases/R01.json", "traces/x.json"])
def test_forbidden_paths(path):
    assert cb.forbidden_path(path)


@pytest.mark.parametrize("path", [".env.example", "backend/app/settings.py", "docs/local-notes.md", ".envrc"])
def test_allowed_paths(path):
    assert cb.forbidden_path(path) is None


def test_parse_added_lines_tracks_line_numbers():
    diff = ("diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -3,0 +4,2 @@\n+line four\n+line five\n"
            "diff --git a/b.md b/b.md\n--- /dev/null\n+++ b/b.md\n@@ -0,0 +1 @@\n+first\n")
    assert cb.parse_added_lines(diff) == [("a.md", 4, "line four"), ("a.md", 5, "line five"), ("b.md", 1, "first")]


# ---------- 在临时 git 仓库里端到端运行 ----------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("local/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def stage(repo: Path, path: str, text: str, force: bool = False) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", *(["-f"] if force else []), path], cwd=repo, check=True)


def test_staged_brand_name_blocks_commit(repo, capsys):
    write_case(repo / "local", "eval/cases", "R01.json", "Fernwick Loop")
    stage(repo, "docs/report.md", "intro\nThe brand Fernwick Loop had 3 sources.\n")
    assert cb.main(["pre-commit"]) == 1
    err = capsys.readouterr().err
    assert "docs/report.md 第 2 行" in err and "Fernwick Loop" in err


def test_brand_in_staged_filename_blocks_commit(repo, capsys):
    write_case(repo / "local", "eval/cases", "R01.json", "Fernwick Loop")
    stage(repo, "examples/2_fernwick_loop.json", "{}\n")
    assert cb.main(["pre-commit"]) == 1
    assert "文件名 examples/2_fernwick_loop.json" in capsys.readouterr().err


def test_clean_commit_passes(repo):
    write_case(repo / "local", "eval/cases", "R01.json", "Fernwick Loop")
    stage(repo, "docs/report.md", "An alcohol beverage brand (R01) had 3 sources.\n")
    assert cb.main(["pre-commit"]) == 0


def test_env_and_forced_local_files_block_commit(repo, capsys):
    stage(repo, ".env", "X=1\n")
    stage(repo, "local/notes.md", "hi\n", force=True)
    assert cb.main(["pre-commit"]) == 1
    err = capsys.readouterr().err
    assert ".env：" in err and "local/notes.md：" in err


def test_empty_list_warns_but_does_not_block(repo, capsys):
    stage(repo, "docs/report.md", "Fernwick Loop\n")
    assert cb.main(["pre-commit"]) == 0
    assert "品牌名单为空" in capsys.readouterr().err


def test_commit_message_scanned_except_comments(repo, tmp_path):
    write_case(repo / "local", "eval/cases", "R01.json", "Fernwick Loop")
    msg = tmp_path / "MSG"
    msg.write_text("docs: notes\n# Fernwick Loop in a git comment line\n", encoding="utf-8")
    assert cb.main(["commit-msg", str(msg)]) == 0
    msg.write_text("docs: add Fernwick Loop case\n", encoding="utf-8")
    assert cb.main(["commit-msg", str(msg)]) == 1


def test_full_scan_checks_tracked_files(repo, capsys):
    stage(repo, "docs/old.md", "Old note about Fernwick Loop\n")
    write_case(repo / "local", "eval/cases", "R01.json", "Fernwick Loop")  # 名单后加的品牌
    assert cb.main(["--all"]) == 1
    assert "docs/old.md 第 1 行" in capsys.readouterr().err
