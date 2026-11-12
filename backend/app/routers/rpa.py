"""RPA流程配置接口：维护RPA流程，覆盖提交校验、启用配置、停用配置等动作。

受限试运行相关端点：
- POST /api/rpa/{entry_id}/trial 发起试运行（同一输入并发重复只生效一次）
- POST /api/rpa/{entry_id}/runs/{run_id}/retry 超时后从断点步骤重试
- POST /api/rpa/{entry_id}/execute 正式执行（按启动时版本留档）
- GET  /api/rpa/{entry_id}/runs 查看该流程的试运行与正式执行记录
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rpa import RpaService

router = APIRouter(prefix="/api/rpa", tags=["RPA流程配置"])

service = RpaService()

LIST_FIELDS = ["流程编号", "流程名称", "输入来源", "动作序列", "等待条件", "超时秒数", "结果字段", "运行环境"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按流程编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按流程编号与状态过滤RPA流程配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条RPA流程明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"RPA流程 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条RPA流程，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="RPA流程已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条RPA流程执行提交校验、启用配置、停用配置；只能按草稿→待校验→已启用推进。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/trial", response_model=ActionResult)
def start_trial(entry_id: int, payload: EntryPayload) -> ActionResult:
    """发起受限试运行：逐步展开动作序列，超时停在当前步骤；同一输入重复提交只生效一次。"""
    run, message = service.start_trial(entry_id, payload.values)
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=run)


@router.post("/{entry_id}/runs/{run_id}/retry", response_model=ActionResult)
def retry_run(entry_id: int, run_id: int, payload: EntryPayload) -> ActionResult:
    """超时试运行从断点步骤重试；可携带新的输入数据补齐缺失字段。"""
    run, message = service.retry_run(entry_id, run_id, payload.values)
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=run)


@router.post("/{entry_id}/execute", response_model=ActionResult)
def formal_execute(entry_id: int, payload: EntryPayload) -> ActionResult:
    """正式执行：仅限已启用流程，从自己的动作序列第 1 步开始，按启动时版本留档。"""
    run, message = service.formal_execute(entry_id, payload.values)
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=run)


@router.get("/{entry_id}/runs", response_model=dict)
def list_runs(entry_id: int) -> dict[str, Any]:
    """查看单条RPA流程的全部试运行与正式执行记录，含每步证据。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"RPA流程 {entry_id} 不存在或已归档")
    runs = service.list_runs(entry_id)
    return {"items": runs, "total": len(runs)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出RPA流程配置清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rpa", "total": total, "items": items}
