"""RPA受限试运行验收测试：覆盖状态机、超时停步、断点重试、输入去重、版本留档与日志/工作台联动。

验收序列 H：
- RPA-0002 第 3 步等待到 18 秒超时，结果只写到第 3 步；
- RPA-0003 的正式执行仍从自己的动作序列开始，不继承试运行状态。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.rpa import service as rpa_service
from app.store import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    store.reset()
    rpa_service.reset_runs()
    yield


def _action(entry_id: int, action: str) -> dict:
    response = client.post(f"/api/rpa/{entry_id}/actions", json={"values": {"action": action}})
    assert response.status_code == 200
    return response.json()


def _trial(entry_id: int, data: dict | None = None) -> dict:
    values = {} if data is None else {"输入数据": data}
    response = client.post(f"/api/rpa/{entry_id}/trial", json={"values": values})
    assert response.status_code == 200
    return response.json()


def _retry(entry_id: int, run_id: int, data: dict | None = None) -> dict:
    values = {} if data is None else {"输入数据": data}
    response = client.post(f"/api/rpa/{entry_id}/runs/{run_id}/retry", json={"values": values})
    assert response.status_code == 200
    return response.json()


def _execute(entry_id: int, data: dict | None = None) -> dict:
    values = {} if data is None else {"输入数据": data}
    response = client.post(f"/api/rpa/{entry_id}/execute", json={"values": values})
    assert response.status_code == 200
    return response.json()


def _log_for(run: dict) -> dict:
    response = client.get("/api/log", params={"keyword": run["日志编号"]})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    return items[0]


def _workbench_item(run: dict) -> dict | None:
    response = client.get("/api/task", params={"size": 200})
    assert response.status_code == 200
    for item in response.json()["items"]:
        if item.get("关联运行编号") == run["运行编号"]:
            return item
    return None


# ------------------------------------------------------------------
# 状态机：只能按 草稿 → 待校验 → 已启用 推进
# ------------------------------------------------------------------
def test_status_flow_only_advances_step_by_step():
    skipped = _action(1, "启用配置")
    assert skipped["ok"] is False
    assert "草稿→待校验→已启用" in skipped["message"]

    assert _action(1, "提交校验")["entry"]["status"] == "待校验"

    blocked = _action(1, "停用配置")
    assert blocked["ok"] is False
    assert "已启用" in blocked["message"]

    enabled = _action(1, "启用配置")
    assert enabled["ok"] is True
    assert enabled["entry"]["status"] == "已启用"
    assert enabled["entry"]["当前版本"] == 1
    assert enabled["entry"]["启用版本快照"]["版本"] == 1
    assert len(enabled["entry"]["启用版本快照"]["步骤配置"]) == 2

    backwards = _action(1, "提交校验")
    assert backwards["ok"] is False


# ------------------------------------------------------------------
# 验收序列 H-1：RPA-0002 第 3 步等待到 18 秒超时，结果只写到第 3 步
# ------------------------------------------------------------------
def test_trial_times_out_at_step3_and_only_writes_up_to_step3():
    result = _trial(2)
    assert result["ok"] is True
    run = result["entry"]
    assert run["类型"] == "试运行"
    assert run["状态"] == "超时待重试"
    assert run["当前步骤"] == 3

    steps = run["步骤结果"]
    assert [step["步骤号"] for step in steps] == [1, 2, 3]  # 结果只写到第 3 步
    assert steps[0]["结果"] == "成功"
    assert steps[1]["结果"] == "成功"
    last = steps[2]
    assert last["结果"] == "超时"
    assert last["超时秒数"] == 18
    assert last["等待秒数"] == 18  # 等待到 18 秒超时
    assert last["等待条件"] == "字段:grid_rows"
    assert "grid_rows" in last["证据"] and "18" in last["证据"]

    # 每一步都回写采集日志：日志里只有 3 条步骤明细
    log = _log_for(run)
    assert log["关联任务"].startswith("RPA-0002·试运行")
    assert log["成功条数"] == "2"
    assert log["失败条数"] == "1"
    assert log["status"] == "部分失败"
    assert log["pending"] is True
    assert log["abnormal"] is True
    assert [step["步骤号"] for step in log["步骤明细"]] == [1, 2, 3]
    assert "grid_rows" in log["错误信息"]

    # 工作台随之生成待处理条目
    item = _workbench_item(run)
    assert item is not None
    assert item["pending"] is True
    assert item["abnormal"] is True
    assert item["待处理步骤"] == 3
    assert "RPA-0002" in item["任务名称"]


def test_every_step_keeps_condition_timeout_field_and_env_evidence():
    run = _trial(2)["entry"]
    for step in run["步骤结果"]:
        for field in ("等待条件", "超时秒数", "结果字段", "运行环境", "证据", "结果"):
            assert step.get(field) not in (None, ""), f"步骤 {step['步骤号']} 缺少证据字段 {field}"
        assert step["证据"] != "步骤失败"  # 不能只回一句步骤失败


# ------------------------------------------------------------------
# 同一输入并发重复试运行只生效一次
# ------------------------------------------------------------------
def test_duplicate_trial_with_same_input_only_effective_once():
    first = _trial(2)["entry"]
    logs_before = client.get("/api/log").json()["total"]

    again = _trial(2)
    assert again["ok"] is True
    assert again["entry"]["运行编号"] == first["运行编号"]
    assert "未重复生效" in again["message"]

    runs = client.get("/api/rpa/2/runs").json()
    assert runs["total"] == 1
    assert client.get("/api/log").json()["total"] == logs_before

    different = _trial(2, {"login_url": "https://oa.internal/login2"})
    assert different["entry"]["运行编号"] != first["运行编号"]


# ------------------------------------------------------------------
# 超时停在当前步骤，从该步断点重试
# ------------------------------------------------------------------
def test_retry_resumes_from_timeout_step_not_from_beginning():
    run = _trial(2)["entry"]

    still_blocked = _retry(2, run["id"])
    assert still_blocked["entry"]["状态"] == "超时待重试"
    assert still_blocked["entry"]["当前步骤"] == 3
    assert still_blocked["entry"]["重试次数"] == 1

    fixed = _retry(2, run["id"], {"grid_rows": ["r1", "r2"]})
    resumed = fixed["entry"]
    assert resumed["状态"] == "已完成"
    assert resumed["重试次数"] == 2
    # 第 1、2 步没有重复执行，仍只有首次尝试的记录
    assert sum(1 for step in resumed["步骤结果"] if step["步骤号"] == 1) == 1
    assert sum(1 for step in resumed["步骤结果"] if step["步骤号"] == 2) == 1
    # 第 3 步留下了每次尝试的证据（两次超时 + 补输入后成功），第 4 步在重试后补跑成功
    assert [step["结果"] for step in resumed["步骤结果"] if step["步骤号"] == 3] == ["超时", "超时", "成功"]
    assert resumed["步骤结果"][-1]["步骤号"] == 4
    assert resumed["步骤结果"][-1]["结果"] == "成功"

    log = _log_for(resumed)
    assert log["status"] == "成功"
    assert log["成功条数"] == "4"
    assert log["失败条数"] == "0"
    assert log["pending"] is False

    item = _workbench_item(resumed)
    assert item["pending"] is False
    assert item["status"] == "已完成"


def test_retry_rejected_when_run_not_waiting():
    run = _trial(1)["entry"]  # RPA-0001 输入样例完整，直接跑完
    assert run["状态"] == "已完成"
    rejected = _retry(1, run["id"])
    assert rejected["ok"] is False
    assert "超时待重试" in rejected["message"]


# ------------------------------------------------------------------
# 验收序列 H-2：RPA-0003 正式执行从自己的动作序列开始，不继承试运行状态
# ------------------------------------------------------------------
def test_formal_execute_starts_from_own_sequence_and_ignores_trial_state():
    trial_run = _trial(3)["entry"]
    assert trial_run["状态"] == "已完成"

    executed = _execute(3)
    assert executed["ok"] is True
    formal = executed["entry"]
    assert formal["类型"] == "正式执行"
    assert formal["运行编号"] != trial_run["运行编号"]
    assert formal["版本号"] == 1
    assert formal["条件快照"]["版本"] == 1  # 按启动时版本留档
    assert formal["状态"] == "已完成"
    # 从自己的动作序列第 1 步开始，不继承试运行的任何状态
    assert formal["步骤结果"][0]["步骤号"] == 1
    assert formal["步骤结果"][0]["动作"] == "打开报表页"
    assert formal["重试次数"] == 0
    assert [step["步骤号"] for step in formal["步骤结果"]] == [1, 2, 3]

    runs = client.get("/api/rpa/3/runs").json()["items"]
    assert {run["运行编号"] for run in runs} == {trial_run["运行编号"], formal["运行编号"]}
    untouched = next(run for run in runs if run["运行编号"] == trial_run["运行编号"])
    assert untouched["类型"] == "试运行"
    assert untouched["状态"] == "已完成"


def test_formal_execute_requires_enabled_flow():
    assert _execute(1)["ok"] is False  # 草稿
    assert _execute(2)["ok"] is False  # 待校验


# ------------------------------------------------------------------
# 冲突时以流程当前启用版本的条件为准
# ------------------------------------------------------------------
def test_enabled_version_wins_when_draft_conflicts():
    entry = client.get("/api/rpa/3").json()
    draft_step3 = entry["步骤配置"][2]
    assert draft_step3["超时秒数"] == 30  # 草稿被改过，和启用版本冲突

    run = _trial(3)["entry"]
    assert run["条件来源"] == "启用版本v1"
    assert run["版本号"] == 1
    step3 = next(step for step in run["步骤结果"] if step["步骤号"] == 3)
    assert step3["超时秒数"] == 12  # 以启用版本 v1 的条件为准，不是草稿的 30


def test_enabled_version_wins_on_retry_after_enable():
    run = _trial(2)["entry"]  # 待校验时用草稿条件试跑到第 3 步超时
    _action(2, "启用配置")  # 待校验 → 已启用，固化 v1 快照
    retried = _retry(2, run["id"], {"grid_rows": ["r1"]})["entry"]
    assert retried["条件来源"] == "启用版本v1"
    assert retried["版本号"] == 1
    assert retried["状态"] == "已完成"


# ------------------------------------------------------------------
# 受限边界
# ------------------------------------------------------------------
def test_trial_rejected_after_flow_stopped():
    assert _action(3, "停用配置")["ok"] is True
    rejected = _trial(3)
    assert rejected["ok"] is False
    assert "已停用" in rejected["message"]


def test_runs_endpoint_lists_trial_and_formal_records():
    _trial(2)
    _execute(3)
    runs2 = client.get("/api/rpa/2/runs").json()
    assert runs2["total"] == 1
    assert runs2["items"][0]["类型"] == "试运行"
    runs3 = client.get("/api/rpa/3/runs").json()
    assert runs3["total"] == 1
    assert runs3["items"][0]["类型"] == "正式执行"
    missing = client.get("/api/rpa/999/runs")
    assert missing.status_code == 404
