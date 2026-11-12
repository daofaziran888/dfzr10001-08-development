"""RPA流程配置业务规则：状态流转、字段校验与筛选口径都收在这里。

受限试运行的约定：
- 流程只能按 草稿 → 待校验 → 已启用 推进，已启用才能停用，不允许跳段；
- 试运行按输入来源与动作序列逐步展开，等待条件不满足就等到超时秒数，停在当前步骤，
  之后从该步断点重试，而不是从头再来；
- 同一输入的并发重复试运行只生效一次（按流程编号 + 输入指纹去重）；
- 条件冲突时以流程当前启用版本的快照为准；历史正式执行按启动时版本留档；
- 每一步结果都回写采集日志，超时未闭环的运行会在任务工作台生成待处理条目。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "rpa"
REQUIRED_FIELDS = ["流程编号", "流程名称", "输入来源"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]

# 状态机：每个动作只允许从特定前置状态推进，杜绝跳段。
TRANSITIONS: dict[str, dict[str, Any]] = {
    "提交校验": {"from": {"草稿"}, "to": "待校验"},
    "启用配置": {"from": {"待校验"}, "to": "已启用"},
    "停用配置": {"from": {"已启用"}, "to": "已停用"},
}
ACTION_RULES = {action: rule["to"] for action, rule in TRANSITIONS.items()}
NEGATIVE_ACTIONS = ["停用配置"]

RUN_OPEN_STATUSES = {"执行中", "超时待重试"}
LOG_MODULE = "log"
TASK_MODULE = "task"


class RpaService:
    def __init__(self) -> None:
        self._runs: list[dict[str, Any]] = []
        self._run_seq = 0

    # ------------------------------------------------------------------
    # 基础列表与登记
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("流程编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["当前版本"] = 0
        entry["启用版本快照"] = None
        entry["输入样例"] = values.get("输入样例") if isinstance(values.get("输入样例"), dict) else {}
        entry["步骤配置"] = copy.deepcopy(values.get("步骤配置")) if isinstance(values.get("步骤配置"), list) else []
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 状态机：只能按 草稿 → 待校验 → 已启用 推进
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"RPA流程 {entry_id} 不存在或已归档"
        rule = TRANSITIONS.get(action)
        if rule is None:
            return None, f"动作「{action}」不属于RPA流程配置可执行范围"
        current = str(entry.get("status") or "")
        if current not in rule["from"]:
            allowed = "、".join(sorted(rule["from"]))
            return None, (
                f"RPA流程当前状态为「{current}」，只能按草稿→待校验→已启用推进，"
                f"「{action}」要求流程处于{allowed}"
            )
        target = rule["to"]
        entry["status"] = target
        entry["pending"] = target in {"草稿", "待校验"}
        entry["abnormal"] = target == "已停用"
        if action == "启用配置":
            self._snapshot_enabled_version(entry)
        return entry, f"RPA流程已{action}，当前状态「{target}」"

    def _snapshot_enabled_version(self, entry: dict[str, Any]) -> None:
        """启用时把当前步骤配置固化成版本快照，之后的冲突都以它为准。"""
        version = int(entry.get("当前版本") or 0) + 1
        entry["当前版本"] = version
        entry["启用版本快照"] = {
            "版本": version,
            "输入来源": entry.get("输入来源"),
            "步骤配置": copy.deepcopy(entry.get("步骤配置") or []),
            "启用时间": self._now(),
        }

    # ------------------------------------------------------------------
    # 试运行 / 断点重试 / 正式执行
    # ------------------------------------------------------------------
    def start_trial(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"RPA流程 {entry_id} 不存在或已归档"
        if entry.get("status") == "已停用":
            return None, f"RPA流程 {entry.get('流程编号')} 已停用，不能试运行"
        steps, source_label, version = self._resolve_conditions(entry)
        if not steps:
            return None, f"RPA流程 {entry.get('流程编号')} 尚未配置动作序列，无法试运行"
        merged = self._merged_input(entry, values.get("输入数据"))
        fingerprint = self._fingerprint(str(entry.get("流程编号")), merged)
        duplicate = self._find_open_trial(entry["id"], fingerprint)
        if duplicate is not None:
            return duplicate, (
                f"相同输入的试运行 {duplicate['运行编号']} 已存在（{duplicate['状态']}），"
                "本次未重复生效；如需继续请从断点重试"
            )
        run = self._new_run(entry, "试运行", source_label, version, merged, fingerprint)
        context = dict(merged)
        self._execute_steps(entry, run, steps, context, start_no=1)
        run["运行上下文"] = context
        return run, self._run_message(run, steps)

    def retry_run(self, entry_id: int, run_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"RPA流程 {entry_id} 不存在或已归档"
        run = self._find_run(entry["id"], run_id)
        if run is None:
            return None, f"运行记录 {run_id} 不属于RPA流程 {entry.get('流程编号')}"
        if run["类型"] != "试运行":
            return None, f"{run['运行编号']} 是正式执行档案，不支持断点重试，请重新发起正式执行"
        if run["状态"] != "超时待重试":
            return None, f"试运行 {run['运行编号']} 当前状态为「{run['状态']}」，只有超时待重试才能从断点重试"
        # 重试时重新取条件：若期间流程被启用/换版，以当前启用版本为准。
        steps, source_label, version = self._resolve_conditions(entry)
        if not steps:
            return None, f"RPA流程 {entry.get('流程编号')} 尚未配置动作序列，无法继续试运行"
        run["条件来源"] = source_label
        run["版本号"] = version
        run["重试次数"] = int(run.get("重试次数") or 0) + 1
        run["结束时间"] = None
        context = dict(run.get("运行上下文") or {})
        provided = values.get("输入数据")
        if isinstance(provided, dict):
            context.update(provided)
        resume_from = int(run.get("当前步骤") or 1)
        self._execute_steps(entry, run, steps, context, start_no=resume_from)
        run["运行上下文"] = context
        return run, self._run_message(run, steps, prefix=f"从第 {resume_from} 步断点重试")

    def formal_execute(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"RPA流程 {entry_id} 不存在或已归档"
        if entry.get("status") != "已启用":
            return None, (
                f"RPA流程 {entry.get('流程编号')} 当前状态为「{entry.get('status')}」，"
                "只有已启用流程才能正式执行"
            )
        snapshot = entry.get("启用版本快照") or {}
        steps = copy.deepcopy(snapshot.get("步骤配置") or [])
        if not steps:
            return None, f"RPA流程 {entry.get('流程编号')} 缺少启用版本快照，无法正式执行"
        version = int(snapshot.get("版本") or entry.get("当前版本") or 0)
        merged = self._merged_input(entry, values.get("输入数据"))
        # 正式执行始终从自己的动作序列第 1 步开始，不继承任何试运行状态。
        run = self._new_run(entry, "正式执行", f"启用版本v{version}", version, merged, fingerprint=None)
        run["条件快照"] = copy.deepcopy(snapshot)
        context = dict(merged)
        self._execute_steps(entry, run, steps, context, start_no=1)
        run["运行上下文"] = context
        return run, self._run_message(run, steps, prefix=f"按启动时版本 v{version} 留档")

    def list_runs(self, entry_id: int) -> list[dict[str, Any]]:
        runs = [run for run in self._runs if run["流程id"] == entry_id]
        return sorted(runs, key=lambda run: int(run["id"]), reverse=True)

    def reset_runs(self) -> None:
        """清空运行记录，测试复位用。"""
        self._runs = []
        self._run_seq = 0

    # ------------------------------------------------------------------
    # 运行引擎：逐步展开、超时停步、逐步回写
    # ------------------------------------------------------------------
    def _execute_steps(
        self,
        entry: dict[str, Any],
        run: dict[str, Any],
        steps: list[dict[str, Any]],
        context: dict[str, Any],
        *,
        start_no: int,
    ) -> None:
        run["状态"] = "执行中"
        attempt = int(run.get("重试次数") or 0) + 1
        for step in steps:
            step_no = int(step.get("步骤号") or 0)
            if step_no < start_no:
                continue
            evidence: dict[str, Any] = {
                "步骤号": step_no,
                "动作": step.get("动作"),
                "等待条件": step.get("等待条件"),
                "超时秒数": step.get("超时秒数"),
                "结果字段": step.get("结果字段"),
                "运行环境": step.get("运行环境"),
                "尝试": attempt,
            }
            ok, detail = self._check_condition(step.get("等待条件"), context, step.get("超时秒数"))
            run["当前步骤"] = step_no
            if ok:
                produced = f"步骤{step_no}产出·{step.get('结果字段')}"
                context[str(step.get("结果字段"))] = produced
                evidence.update({"结果": "成功", "等待秒数": 0, "证据": detail, "产出": produced})
                run["步骤结果"].append(evidence)
                self._writeback_log(run)  # 每一步结果都回写采集日志
                continue
            evidence.update({"结果": "超时", "等待秒数": step.get("超时秒数"), "证据": detail})
            run["步骤结果"].append(evidence)
            run["状态"] = "超时待重试"
            run["结束时间"] = self._now()
            self._writeback_log(run)
            self._workbench_block(entry, run, step)
            return
        run["状态"] = "已完成"
        run["结束时间"] = self._now()
        self._writeback_log(run)
        self._workbench_resolve(run)

    def _check_condition(
        self,
        condition: Any,
        context: dict[str, Any],
        timeout: Any,
    ) -> tuple[bool, str]:
        text = str(condition or "").strip()
        available = "、".join(sorted(str(key) for key in context)) or "（空）"
        if text.startswith("字段:"):
            name = text.split(":", 1)[1].strip()
            if name and context.get(name) not in (None, "", [], {}):
                return True, f"等待条件「{text}」已满足：运行上下文提供字段「{name}」，无需等待"
            return False, (
                f"等待条件「{text}」在 {timeout} 秒内未满足：运行上下文缺少字段「{name}」"
                f"（当前可用字段：{available}），等待 {timeout} 秒后判定超时"
            )
        return False, (
            f"等待条件「{text or '（空）'}」不是可校验的字段表达式，"
            f"等待 {timeout} 秒后判定超时（当前可用字段：{available}）"
        )

    # ------------------------------------------------------------------
    # 采集日志回写与任务工作台联动
    # ------------------------------------------------------------------
    def _writeback_log(self, run: dict[str, Any]) -> None:
        rows = store.rows(LOG_MODULE)
        entry = next((row for row in rows if row.get("日志编号") == run["日志编号"]), None)
        latest = self._latest_steps(run)
        success = sum(1 for step in latest if step.get("结果") == "成功")
        failed = sum(1 for step in latest if step.get("结果") == "超时")
        last_error = next((step["证据"] for step in reversed(latest) if step.get("结果") == "超时"), "")
        if failed == 0:
            status = "成功"
        elif success == 0:
            status = "完全失败"
        else:
            status = "部分失败"
        values = {
            "关联任务": f"{run['流程编号']}·{run['类型']}·{run['运行编号']}",
            "开始时间": run["开始时间"],
            "结束时间": self._now(),
            "采集行数": str(len(latest)),
            "成功条数": str(success),
            "失败条数": str(failed),
            "错误信息": last_error,
            "来源模块": "rpa",
            "步骤明细": copy.deepcopy(latest),
        }
        if entry is None:
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry["日志编号"] = run["日志编号"]
            rows.append(entry)
        entry.update(values)
        entry["status"] = status
        entry["pending"] = run["状态"] in RUN_OPEN_STATUSES
        entry["abnormal"] = failed > 0

    def _workbench_block(self, entry: dict[str, Any], run: dict[str, Any], step: dict[str, Any]) -> None:
        rows = store.rows(TASK_MODULE)
        item = next((row for row in rows if row.get("关联运行编号") == run["运行编号"]), None)
        if item is None:
            item = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            item["任务编号"] = self._next_code(TASK_MODULE, "任务编号", "TASK")
            rows.append(item)
        item.update({
            "任务名称": f"{run['流程编号']} {run['类型']}第{step.get('步骤号')}步超时待处理",
            "关联源端": entry.get("输入来源"),
            "采集频率": "手动触发",
            "增量字段": "—",
            "起始水位": "—",
            "批次大小": "—",
            "执行窗口": "随时",
            "关联流程编号": run["流程编号"],
            "关联运行编号": run["运行编号"],
            "待处理步骤": step.get("步骤号"),
            "处理建议": f"检查等待条件「{step.get('等待条件')}」与超时秒数 {step.get('超时秒数')}，修正后从断点重试",
            "status": "待执行",
            "pending": True,
            "abnormal": True,
        })

    def _workbench_resolve(self, run: dict[str, Any]) -> None:
        for row in store.rows(TASK_MODULE):
            if row.get("关联运行编号") == run["运行编号"] and row.get("pending"):
                row["status"] = "已完成"
                row["pending"] = False
                row["abnormal"] = False
                row["处理结果"] = f"{run['运行编号']} 已完成，待处理关闭"

    # ------------------------------------------------------------------
    # 条件解析、输入指纹与编号
    # ------------------------------------------------------------------
    def _resolve_conditions(self, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str, int]:
        """冲突时以流程当前启用版本的条件为准；没有启用版本才用当前草稿。"""
        snapshot = entry.get("启用版本快照") or {}
        steps = snapshot.get("步骤配置")
        if steps:
            version = int(snapshot.get("版本") or entry.get("当前版本") or 0)
            return copy.deepcopy(steps), f"启用版本v{version}", version
        return copy.deepcopy(entry.get("步骤配置") or []), "当前草稿（未启用）", 0

    def _merged_input(self, entry: dict[str, Any], provided: Any) -> dict[str, Any]:
        merged = dict(entry.get("输入样例") or {})
        if isinstance(provided, dict):
            merged.update(provided)
        return merged

    def _fingerprint(self, flow_code: str, merged: dict[str, Any]) -> str:
        raw = json.dumps(merged, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(f"{flow_code}|{raw}".encode("utf-8")).hexdigest()[:16]

    def _find_open_trial(self, entry_id: int, fingerprint: str) -> dict[str, Any] | None:
        for run in self._runs:
            if (
                run["流程id"] == entry_id
                and run["类型"] == "试运行"
                and run.get("输入指纹") == fingerprint
                and run["状态"] in RUN_OPEN_STATUSES
            ):
                return run
        return None

    def _find_run(self, entry_id: int, run_id: int) -> dict[str, Any] | None:
        for run in self._runs:
            if run["流程id"] == entry_id and int(run["id"]) == run_id:
                return run
        return None

    def _new_run(
        self,
        entry: dict[str, Any],
        run_type: str,
        source_label: str,
        version: int,
        merged: dict[str, Any],
        fingerprint: str | None,
    ) -> dict[str, Any]:
        self._run_seq += 1
        run = {
            "id": self._run_seq,
            "运行编号": f"RUN-{self._run_seq:04d}",
            "流程id": entry["id"],
            "流程编号": entry.get("流程编号"),
            "类型": run_type,
            "状态": "执行中",
            "当前步骤": 0,
            "版本号": version,
            "条件来源": source_label,
            "输入指纹": fingerprint,
            "输入数据": copy.deepcopy(merged),
            "运行上下文": {},
            "步骤结果": [],
            "日志编号": self._next_code(LOG_MODULE, "日志编号", "LOG"),
            "开始时间": self._now(),
            "结束时间": None,
            "重试次数": 0,
        }
        self._runs.append(run)
        return run

    def _latest_steps(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        """每一步只取最新一次尝试的结果，用于日志回写与统计。"""
        latest: dict[int, dict[str, Any]] = {}
        for step in run["步骤结果"]:
            no = int(step.get("步骤号") or 0)
            if no not in latest or int(step.get("尝试") or 1) >= int(latest[no].get("尝试") or 1):
                latest[no] = step
        return [latest[no] for no in sorted(latest)]

    def _next_code(self, module: str, field: str, prefix: str) -> str:
        top = 0
        for row in store.rows(module):
            code = str(row.get(field, ""))
            if code.startswith(f"{prefix}-"):
                try:
                    top = max(top, int(code.split("-", 1)[1]))
                except ValueError:
                    continue
        return f"{prefix}-{top + 1:04d}"

    def _run_message(self, run: dict[str, Any], steps: list[dict[str, Any]], prefix: str = "") -> str:
        total = len(steps)
        done = len(self._latest_steps(run))
        head = f"{run['类型']} {run['运行编号']}"
        if prefix:
            head = f"{head}（{prefix}）"
        if run["状态"] == "超时待重试":
            current = run["当前步骤"]
            timeout = next(
                (step.get("超时秒数") for step in reversed(run["步骤结果"]) if step.get("步骤号") == current),
                None,
            )
            return f"{head} 在第 {current} 步等待 {timeout} 秒超时，已停在当前步骤，可从该步重试（{done}/{total} 步）"
        return f"{head} 已完成，{total} 步全部成功"

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
