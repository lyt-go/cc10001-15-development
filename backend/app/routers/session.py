"""采集会话接口：按目标站点管理采集会话，覆盖验证、到期扫描、代理失效联动与冲突判定。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.session import SessionService

router = APIRouter(prefix="/api/session", tags=["采集会话"])

service = SessionService()

LIST_FIELDS = ["会话编号", "目标站点", "凭证编号", "反爬策略", "绑定代理", "生效UA", "生效Referer", "生效延迟", "最近验证时间", "追踪编号"]
STATUSES = ["待验证", "有效", "已过期", "已阻断"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按会话编号或目标站点检索"),
    status: str | None = Query(default=None, description="待验证、有效、已过期、已阻断"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按会话编号、目标站点与状态过滤采集会话列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats")
def stats() -> dict[str, int]:
    """会话看板计数：总数、有效、已阻断与待处理告警。"""
    return service.stats()


@router.get("/history", response_model=PageResult[dict])
def list_history(
    keyword: str | None = Query(default=None, description="按追踪编号、会话编号或凭证编号检索"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """验证流水：追加式记录，新的失败或恢复结果只新增不覆盖。"""
    items, total = service.list_history(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/effective")
def effective_config(site: str = Query(description="目标站点域名")) -> dict[str, Any]:
    """冲突判定：凭证与反爬配置冲突时，按最近验证成功快照与当前绑定代理给出生效配置。"""
    result = service.effective_config(site)
    if result is None:
        raise HTTPException(status_code=404, detail=f"目标站点 {site} 尚未建立采集会话")
    return result


@router.get("/usable")
def usable(site: str = Query(description="目标站点域名")) -> dict[str, Any]:
    """任务侧放行检查：只有验证成功的会话才允许继续被采集任务使用。"""
    return service.usable(site)


@router.post("/sweep", response_model=dict)
def sweep() -> dict[str, Any]:
    """到期前扫描：按刷新策略对进入验证窗口的凭证发起验证，本周期已有结果的跳过。"""
    triggered = service.sweep()
    return {"ok": True, "message": f"扫描完成，触发 {len(triggered)} 条验证", "items": triggered}


@router.post("/proxy-down", response_model=ActionResult)
def proxy_down(payload: EntryPayload) -> ActionResult:
    """代理失效通知：阻断绑定该代理的旧会话，凭证回到验证中并生成可追踪告警。"""
    proxy_code = str(payload.values.get("节点编号") or "").strip()
    if not proxy_code:
        return ActionResult(ok=False, message="缺少必填字段：节点编号")
    affected, message = service.proxy_down(proxy_code)
    if affected is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"阻断会话": [row.get("会话编号") for row in affected]})


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集会话清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "session", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集会话明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集会话 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集会话，缺字段或凭证未登记时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少或无效字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="采集会话已登记", entry=entry)


@router.post("/{entry_id}/validate", response_model=ActionResult)
def validate_entry(entry_id: int) -> ActionResult:
    """验证会话关联的登录凭证；同一凭证并发或重复刷新只产生一次有效结果。"""
    entry, message, ok = service.validate_session(entry_id, trigger="手动验证")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=ok, message=message, entry=entry)
