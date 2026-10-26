"""评测脚本测试：读取案例、单次打分、汇总、报告和仅规则端到端运行。只用虚构品牌，不调用模型。"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.evalset import CaseError, grade_run, group_grades, load_cases, render_report, request_body, select_cases, summarize

SAMPLE = Path(__file__).resolve().parents[2] / "eval" / "sample_case.json"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_eval.py"


def make_case(case_id="F90", split="dev", category="negotiate", decision="negotiate", **expected) -> dict:
    case = json.loads(SAMPLE.read_text(encoding="utf-8"))
    case["case"].update(id=case_id, split=split, category=category)
    case["expected"].update(decision=decision, **expected)
    return case


def make_result(decision="negotiate", score=60, rules=(), lights=None, position="p25_to_p50",
                findings=None, cost=0.04, elapsed=10.0, status="completed") -> dict:
    lights = lights or {}
    terms = [{"term": t, "light": lights.get(t, "green")} for t in ("exclusivity", "usage_rights", "kill_fee", "revisions")]
    return {
        "evaluation": {
            "decision": decision,
            "total_score": score,
            "hard_rules": [{"rule_id": r, "kind": "cap"} for r in rules],
            "contract": {"terms": terms},
            "pricing": {"position": position},
        },
        "findings": findings,
        "agent": {"status": status, "usage": {"cost_cny": cost, "elapsed_seconds": elapsed}},
    }


def findings(confirmed=True, flags=(), categories=()) -> dict:
    return {
        "brand_entity_confirmed": confirmed,
        "severe_flags": [{"type": f} for f in flags],
        "detected_categories": [{"category": c} for c in categories],
    }


# ---------- 读取案例 ----------


def write(folder: Path, case: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{case['case']['id']}.json").write_text(json.dumps(case), encoding="utf-8")


def test_load_cases_merges_directories_sorted(tmp_path):
    write(tmp_path / "a", make_case("F92"))
    write(tmp_path / "b", make_case("F91"))
    assert [c["case"]["id"] for c in load_cases([tmp_path / "a", tmp_path / "b"])] == ["F91", "F92"]


def test_load_cases_rejects_duplicates(tmp_path):
    write(tmp_path / "a", make_case("F91"))
    write(tmp_path / "b", make_case("F91"))
    with pytest.raises(CaseError, match="重复"):
        load_cases([tmp_path / "a", tmp_path / "b"])


def test_load_cases_rejects_unlabeled(tmp_path):
    case = make_case("F91")
    case["expected"]["decision"] = None
    write(tmp_path, case)
    with pytest.raises(CaseError, match="标准答案"):
        load_cases([tmp_path])


def test_select_by_split_and_ids():
    cases = [make_case("F91", split="dev"), make_case("F92", split="test")]
    assert [c["case"]["id"] for c in select_cases(cases, "test")] == ["F92"]
    assert [c["case"]["id"] for c in select_cases(cases, "all", ["F91"])] == ["F91"]
    with pytest.raises(CaseError, match="F99"):
        select_cases(cases, ids=["F99"])


def test_request_body_hides_answers():
    body = request_body(make_case())
    assert set(body) == {"profile", "deal"}


# ---------- 单次打分 ----------


def test_decision_and_severe_misjudgment():
    case = make_case(decision="decline", hard_rules={"must_hit": [], "must_not_hit": []})
    assert grade_run(case, make_result("decline", 30))["decision_ok"] is True
    g = grade_run(case, make_result("accept", 80))
    assert g["decision_ok"] is False and g["severe_misjudgment"] is True
    assert grade_run(case, make_result("negotiate", 60))["severe_misjudgment"] is False


def test_score_range_checked_only_without_must_hit():
    free = make_case(decision="negotiate", hard_rules={"must_hit": [], "must_not_hit": []})
    assert grade_run(free, make_result(score=74))["score_ok"] is True
    assert grade_run(free, make_result(score=75))["score_ok"] is False
    capped = make_case(decision="negotiate", hard_rules={"must_hit": ["HR-4"], "must_not_hit": []})
    assert grade_run(capped, make_result(score=90, rules=["HR-4"]))["score_ok"] is None


def test_missed_and_false_hard_rules():
    case = make_case(hard_rules={"must_hit": ["HR-5", "HR-7"], "must_not_hit": ["HR-2"]})
    g = grade_run(case, make_result(rules=["HR-5", "HR-2"]))
    assert g["missed_rules"] == ["HR-7"] and g["false_rules"] == ["HR-2"]


def test_entity_check_skipped_without_agent():
    case = make_case(brand_entity={"should_confirm": True, "confusable_with": []})
    assert grade_run(case, make_result(findings=None))["entity_ok"] is None
    assert grade_run(case, make_result(findings=findings(confirmed=True)))["entity_ok"] is True
    assert grade_run(case, make_result(findings=findings(confirmed=False)))["entity_ok"] is False
    unset = make_case(brand_entity={"should_confirm": None, "confusable_with": []})
    assert grade_run(unset, make_result(findings=findings()))["entity_ok"] is None


@pytest.mark.parametrize("check, result, ok", [
    ({"type": "hard_rule", "rule": "HR-4"}, make_result(rules=["HR-4"]), True),
    ({"type": "hard_rule", "rule": "HR-4"}, make_result(), False),
    ({"type": "term_light", "term": "kill_fee", "light": "red"}, make_result(lights={"kill_fee": "red"}), True),
    ({"type": "term_light", "term": "kill_fee", "light": "red"}, make_result(lights={"kill_fee": "yellow"}), False),
    ({"type": "pricing_position", "position": "below_p25"}, make_result(position="below_p25"), True),
    ({"type": "pricing_position", "position": "below_p25"}, make_result(position="p25_to_p50"), False),
    ({"type": "severe_flag", "flag": "safety"}, make_result(findings=findings(flags=["safety"])), True),
    ({"type": "severe_flag", "flag": "safety"}, make_result(findings=None), False),
    ({"type": "detected_category", "category": "gambling"}, make_result(findings=findings(categories=["gambling"])), True),
    ({"type": "judge"}, make_result(), None),
])
def test_key_risk_checks(check, result, ok):
    case = make_case(key_risks=[{"id": "r", "desc": "x", "check": check}])
    assert grade_run(case, result)["risks"] == [{"id": "r", "type": check["type"], "ok": ok}]


def test_unknown_check_type_raises():
    case = make_case(key_risks=[{"id": "r", "desc": "x", "check": {"type": "vibes"}}])
    with pytest.raises(CaseError):
        grade_run(case, make_result())


# ---------- 汇总和报告 ----------


def test_summarize_counts_runs_and_stability():
    a = make_case("F91", split="dev", decision="negotiate", hard_rules={"must_hit": ["HR-7"], "must_not_hit": []})
    b = make_case("F92", split="test", category="decline", decision="decline",
                  hard_rules={"must_hit": [], "must_not_hit": []})
    grades = group_grades([
        ("F91", grade_run(a, make_result("negotiate", rules=["HR-7"], cost=0.04, elapsed=10))),
        ("F91", grade_run(a, make_result("decline", rules=[], cost=0.06, elapsed=20))),
        ("F92", grade_run(b, make_result("accept", 80, cost=0.0, status="skipped"))),
    ])
    s = summarize([a, b], grades)
    assert s["runs"] == 3 and s["cases"] == 2
    assert s["decision_accuracy"] == {"passed": 1, "total": 3, "rate": 0.3333}
    assert s["decision_accuracy_by_split"]["test"]["passed"] == 0
    assert s["severe_misjudgments"] == 1
    assert s["hard_rule_recall"] == {"passed": 1, "total": 2, "rate": 0.5}  # 第二次运行没出现 HR-7
    assert s["hard_rule_interception"] == {"passed": 2, "total": 2, "rate": 1.0}  # 但两次决策都没到"接受"
    assert s["stability"] == {"passed": 0, "total": 1, "rate": 0.0}  # 只有 F91 跑了多次
    assert s["cost_cny"]["total"] == 0.1
    assert s["latency_seconds"] == {"p50": 10, "p90": 20}  # 跳过 Agent 的运行不计时延


def test_empty_metrics_are_none_not_zero():
    case = make_case(hard_rules={"must_hit": [], "must_not_hit": []})
    s = summarize([case], group_grades([("F90", grade_run(case, make_result()))]))
    assert s["hard_rule_interception"]["rate"] is None
    assert s["hard_rule_recall"]["rate"] is None
    assert s["stability"]["rate"] is None


def test_report_lists_cases_without_brand_names():
    case = make_case("F91")
    grades = group_grades([("F91", grade_run(case, make_result()))])
    meta = {"config": "rules_only", "started_at": "2026-10-27T10:00:00", "repeats": 1, "selection": "split=all"}
    report = render_report(meta, summarize([case], grades), [case], grades)
    assert "| F91 |" in report
    assert case["deal"]["brand_name"] not in report


# ---------- 命令行：仅规则端到端 ----------


@pytest.fixture
def run_eval(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases_dir = tmp_path / "cases"
    write(cases_dir, make_case("F91", hard_rules={"must_hit": [], "must_not_hit": []}))
    monkeypatch.setattr(module, "CASE_DIRS", [cases_dir])
    monkeypatch.setattr(module, "RUNS_DIR", tmp_path / "runs")
    return module


def test_rules_only_run_writes_results_and_report(run_eval, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["run_eval.py", "--config", "rules_only", "--repeats", "2"])
    run_eval.main()
    (out,) = (tmp_path / "runs").iterdir()
    assert sorted(p.name for p in (out / "results").iterdir()) == ["F91-1.json", "F91-2.json"]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["summary"]["runs"] == 2
    assert summary["summary"]["stability"]["rate"] == 1.0  # 规则是确定性的
    assert (out / "report.md").exists()


def test_rescore_uses_saved_results(run_eval, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["run_eval.py"])
    run_eval.main()
    (out,) = (tmp_path / "runs").iterdir()
    got = json.loads((out / "results" / "F91-1.json").read_text(encoding="utf-8"))["result"]["evaluation"]["decision"]
    path = run_eval.CASE_DIRS[0] / "F91.json"

    def rescore_with(decision: str) -> int:
        """改标准答案后重新打分：保存的结果不变，一致率跟着标准答案变。"""
        case = json.loads(path.read_text(encoding="utf-8"))
        case["expected"]["decision"] = decision
        path.write_text(json.dumps(case), encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["run_eval.py", "--rescore", str(out)])
        run_eval.main()
        return json.loads((out / "summary.json").read_text(encoding="utf-8"))["summary"]["decision_accuracy"]["passed"]

    assert rescore_with(got) == 1
    assert rescore_with(next(d for d in ("accept", "negotiate", "decline") if d != got)) == 0


def test_hybrid_requires_confirmation(run_eval, monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_eval.py", "--config", "hybrid"])
    with pytest.raises(SystemExit, match="--yes"):
        run_eval.main()


def test_case_copy_unchanged_by_grading():
    case = make_case(key_risks=[{"id": "r", "desc": "x", "check": {"type": "judge"}}])
    before = copy.deepcopy(case)
    grade_run(case, make_result())
    assert case == before
