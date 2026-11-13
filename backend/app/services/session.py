"""采集会话业务规则：按目标站点把登录凭证、反爬策略、代理节点串成一条可验证的会话。

联动口径（状态流转只允许在本模块改，路由层不做业务判断）：

- 凭证在到期前按刷新策略触发验证，验证成功才允许任务继续使用该站点会话；
- 验证失败把凭证标记为「已过期」，并向采集告警队列追加一条带追踪编号的异常；
- 反爬策略按目标域名取「已启用」配置的 UA 模拟、Referer、随机延迟作为会话生效快照；
- 绑定代理失效时阻断旧会话、凭证回到「验证中」，提示重新验证；
- 凭证与反爬配置冲突时，按最近一次验证成功的会话快照和当前绑定代理判定；
- 同一凭证的并发验证或重复刷新只产生一次有效结果（每凭证一把锁 + 刷新周期幂等键）；
- 验证流水只追加不修改，历史凭证与已完成的采集日志保持原值。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.store import store

MODULE = "session"
LOG_MODULE = "sessionlog"
REQUIRED_FIELDS = ["目标站点", "凭证编号", "反爬策略", "绑定代理"]
STATUS_ORDER = ["待验证", "有效", "已过期", "已阻断"]

_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")


def _now() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M")


def _parse_time(text: Any) -> datetime | None:
    """解析有效期至；只有日期时按当天 23:59 计，解析不了返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            parsed = parsed.replace(hour=23, minute=59)
        return parsed
    return None


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class SessionService:
    """采集会话的验证、阻断、冲突判定与追加式流水。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---------- 基础查询 ----------

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
            rows = [
                row for row in rows
                if keyword in str(row.get("会话编号", "")) or keyword in str(row.get("目标站点", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def stats(self) -> dict[str, int]:
        rows = store.rows(MODULE)
        return {
            "会话总数": len(rows),
            "有效会话": sum(1 for row in rows if row.get("status") == "有效"),
            "已阻断会话": sum(1 for row in rows if row.get("status") == "已阻断"),
            "待处理告警": sum(1 for row in store.rows("alertrule") if row.get("abnormal")),
        }

    def list_history(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """验证流水只读视图：追加式记录，不提供任何修改入口。"""
        rows = list(store.rows(LOG_MODULE))
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("追踪编号", ""))
                or keyword in str(row.get("会话编号", ""))
                or keyword in str(row.get("凭证编号", ""))
            ]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        if self._credential(str(values.get("凭证编号"))) is None:
            return None, ["凭证编号（需在登录态管理中已登记）"]
        rows = store.rows(MODULE)
        entry = {"id": _next_id(rows)}
        entry.update({field: str(values.get(field)).strip() for field in REQUIRED_FIELDS})
        entry["会话编号"] = f"SES-{entry['id']:04d}"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["生效UA"] = "-"
        entry["生效Referer"] = "-"
        entry["生效延迟"] = "-"
        entry["最近验证时间"] = "-"
        entry["追踪编号"] = "-"
        rows.append(entry)
        return entry, []

    # ---------- 核心：验证 ----------

    def validate_session(self, entry_id: int, *, trigger: str = "手动验证") -> tuple[dict[str, Any] | None, str, bool]:
        """验证会话关联的登录凭证；同一凭证并发或重复刷新只产生一次有效结果。"""
        session = store.find(MODULE, entry_id)
        if session is None:
            return None, f"采集会话 {entry_id} 不存在或已归档", False
        credential_code = str(session.get("凭证编号") or "")
        lock = self._lock_for(credential_code)
        with lock:
            credential = self._credential(credential_code)
            if credential is None:
                return None, f"会话关联的登录凭证 {credential_code} 不存在", False
            epoch = self._epoch_key(credential)
            reused = self._find_success_log(credential_code, epoch)
            if reused is not None:
                # 本刷新周期已有一次有效结果，重复刷新直接复用，不再产生新记录
                return session, f"本周期已有验证成功结果（{reused.get('追踪编号')}），复用不重复验证", True

            trace = self._next_trace()
            now_text = _now_str()
            reasons = self._check_credential(credential)
            if reasons:
                detail = "、".join(reasons)
                credential["status"] = "已过期"
                credential["pending"] = False
                credential["abnormal"] = True
                credential["最近验证时间"] = now_text
                session["status"] = "已过期"
                session["pending"] = True
                session["abnormal"] = True
                session["最近验证时间"] = now_text
                session["追踪编号"] = trace
                self._append_alert(
                    trace=trace, metric="凭证验证失败", level="严重",
                    credential_code=credential_code, site=str(session.get("目标站点") or ""),
                    detail=f"{trigger}未通过：{detail}，凭证已标记过期",
                )
                self._append_log(
                    trace=trace, session=session, credential=credential, trigger=trigger,
                    result="失败", detail=detail, epoch=epoch,
                )
                return session, f"验证失败：{detail}；凭证已标记过期，告警 {trace} 已入采集告警队列", False

            strategy = self._enabled_strategy(str(session.get("目标站点") or ""))
            snapshot = self._snapshot(strategy)
            credential["status"] = "有效"
            credential["pending"] = False
            credential["abnormal"] = False
            credential["最近验证时间"] = now_text
            session["status"] = "有效"
            session["pending"] = False
            session["abnormal"] = False
            session["绑定代理"] = str(credential.get("绑定代理") or "")
            session["生效UA"] = snapshot["ua"]
            session["生效Referer"] = snapshot["referer"]
            session["生效延迟"] = snapshot["delay"]
            session["最近验证时间"] = now_text
            session["追踪编号"] = trace
            self._append_log(
                trace=trace, session=session, credential=credential, trigger=trigger,
                result="成功", detail="登录态与反爬配置校验通过", epoch=epoch, snapshot=snapshot,
            )
            return session, f"验证成功，会话已按目标站点反爬策略生效（{trace}）", True

    def sweep(self) -> list[dict[str, Any]]:
        """到期前扫描：按各凭证刷新策略找出进入验证窗口的会话并发起验证。"""
        now = _now()
        triggered: list[dict[str, Any]] = []
        for session in list(store.rows(MODULE)):
            credential = self._credential(str(session.get("凭证编号") or ""))
            if credential is None:
                continue
            expiry = _parse_time(credential.get("有效期至"))
            lead = self._refresh_lead(str(credential.get("刷新策略") or ""))
            if expiry is not None and now < expiry - lead:
                continue  # 还没进入刷新策略要求的到期前窗口
            epoch = self._epoch_key(credential)
            if self._find_any_log(str(credential.get("凭证编号") or ""), epoch) is not None:
                continue  # 本周期已验证过（无论成败），不重复刷新、不重复告警
            entry, message, ok = self.validate_session(int(session.get("id", 0)), trigger="到期前验证")
            triggered.append({
                "会话编号": session.get("会话编号"),
                "凭证编号": credential.get("凭证编号"),
                "验证通过": ok,
                "消息": message,
            })
        return triggered

    # ---------- 联动：代理失效 ----------

    def proxy_down(self, proxy_code: str) -> tuple[list[dict[str, Any]] | None, str]:
        """绑定代理失效：阻断使用该代理的全部旧会话，凭证回验证中并生成可追踪告警。"""
        proxy = self._proxy(proxy_code)
        if proxy is None:
            return None, f"代理节点 {proxy_code} 不存在或已归档"
        proxy["status"] = "已下线"
        proxy["健康状态"] = "失效"
        proxy["pending"] = False
        proxy["abnormal"] = True
        affected: list[dict[str, Any]] = []
        for session in store.rows(MODULE):
            if str(session.get("绑定代理") or "") != proxy_code or session.get("status") == "已阻断":
                continue
            credential = self._credential(str(session.get("凭证编号") or ""))
            trace = self._next_trace()
            session["status"] = "已阻断"
            session["pending"] = True
            session["abnormal"] = True
            session["追踪编号"] = trace
            if credential is not None:
                credential["status"] = "验证中"
                credential["pending"] = True
            detail = f"绑定代理 {proxy_code} 已失效，旧会话被阻断，请重新验证"
            self._append_alert(
                trace=trace, metric="绑定代理失效", level="严重",
                credential_code=str(session.get("凭证编号") or ""),
                site=str(session.get("目标站点") or ""), detail=detail,
            )
            self._append_log(
                trace=trace, session=session, credential=credential or {"凭证编号": session.get("凭证编号")},
                trigger="代理失效", result="阻断", detail=detail,
            )
            affected.append(session)
        return affected, f"代理 {proxy_code} 已标记失效，阻断 {len(affected)} 条会话并提示重新验证"

    # ---------- 联动：冲突判定与任务侧可用性 ----------

    def effective_config(self, site: str) -> dict[str, Any] | None:
        """凭证与反爬配置冲突时，按最近一次验证成功的快照和当前绑定代理判定。"""
        session = self._session_by_site(site)
        if session is None:
            return None
        credential = self._credential(str(session.get("凭证编号") or ""))
        strategy = self._enabled_strategy(site)
        current_proxy = str((credential or {}).get("绑定代理") or session.get("绑定代理") or "")
        conflicts: list[str] = []
        if strategy is None:
            conflicts.append("目标域名没有已启用的反爬策略")
        elif not str(strategy.get("UA模拟列表") or "").strip():
            conflicts.append("反爬策略未配置UA模拟列表")
        if credential is not None and str(credential.get("绑定代理") or "") != str(session.get("绑定代理") or ""):
            conflicts.append("凭证绑定代理与会话绑定代理不一致")

        last_ok = self._last_success_log(str(session.get("会话编号") or ""))
        if last_ok is not None and str(last_ok.get("绑定代理") or "") == current_proxy:
            source = "最近验证成功快照"
            ua = str(last_ok.get("生效UA") or "-")
            referer = str(last_ok.get("生效Referer") or "-")
            delay = str(last_ok.get("生效延迟") or "-")
        else:
            source = "按当前策略与绑定代理重算"
            snapshot = self._snapshot(strategy)
            ua, referer, delay = snapshot["ua"], snapshot["referer"], snapshot["delay"]
        return {
            "目标站点": site,
            "会话编号": session.get("会话编号"),
            "冲突": conflicts,
            "判定来源": source,
            "绑定代理": current_proxy or "-",
            "生效UA": ua,
            "生效Referer": referer,
            "生效延迟": delay,
        }

    def usable(self, site: str) -> dict[str, Any]:
        """任务侧放行检查：只有验证成功且代理可用的会话才允许继续采集。"""
        session = self._session_by_site(site)
        if session is None:
            return {"目标站点": site, "usable": False, "原因": "未建立采集会话"}
        if session.get("status") != "有效":
            return {"目标站点": site, "usable": False, "原因": f"会话状态为{session.get('status')}，需重新验证"}
        credential = self._credential(str(session.get("凭证编号") or ""))
        if credential is None or credential.get("status") != "有效":
            return {"目标站点": site, "usable": False, "原因": "凭证未通过验证或已过期"}
        proxy = self._proxy(str(credential.get("绑定代理") or ""))
        if proxy is None or proxy.get("status") != "可用":
            return {"目标站点": site, "usable": False, "原因": "绑定代理不可用，需重新验证"}
        return {"目标站点": site, "usable": True, "原因": "会话有效", "追踪编号": session.get("追踪编号")}

    # ---------- 内部工具 ----------

    def _lock_for(self, credential_code: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(credential_code, threading.Lock())

    def _next_trace(self) -> str:
        return f"TRACE-{_now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"

    def _credential(self, code: str) -> dict[str, Any] | None:
        for row in store.rows("lmcfg"):
            if str(row.get("凭证编号") or "") == code:
                return row
        return None

    def _proxy(self, code: str) -> dict[str, Any] | None:
        for row in store.rows("proxyconfig"):
            if str(row.get("节点编号") or "") == code:
                return row
        return None

    def _session_by_site(self, site: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("目标站点") or "") == site:
                return row
        return None

    def _enabled_strategy(self, site: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows("blacklist")
            if str(row.get("目标域名") or "") == site and row.get("status") == "已启用"
        ]
        return candidates[-1] if candidates else None

    def _snapshot(self, strategy: dict[str, Any] | None) -> dict[str, str]:
        """从已启用反爬策略取生效配置：UA 轮换取列表首项，Referer 与随机延迟整配生效。"""
        if strategy is None:
            return {"ua": "-", "referer": "-", "delay": "-"}
        ua_pool = [item.strip() for item in re.split(r"[/,，]", str(strategy.get("UA模拟列表") or "")) if item.strip()]
        return {
            "ua": ua_pool[0] if ua_pool else "-",
            "referer": str(strategy.get("Referer配置") or "-"),
            "delay": str(strategy.get("随机延迟范围") or "-"),
        }

    def _refresh_lead(self, policy: str) -> timedelta:
        """刷新策略解析：到期前N小时/分钟验证、每日验证，默认到期前1小时。"""
        match = re.search(r"到期前(\d+)小时", policy)
        if match:
            return timedelta(hours=int(match.group(1)))
        match = re.search(r"到期前(\d+)分钟", policy)
        if match:
            return timedelta(minutes=int(match.group(1)))
        if "每日" in policy:
            return timedelta(hours=24)
        return timedelta(hours=1)

    def _epoch_key(self, credential: dict[str, Any]) -> str:
        """刷新周期幂等键：凭证内容未变时只认一次有效结果；每日类策略按天进入新周期。"""
        parts = [
            str(credential.get("凭证编号") or ""),
            str(credential.get("有效期至") or ""),
            str(credential.get("刷新策略") or ""),
        ]
        if "每日" in parts[2]:
            parts.append(_now().strftime("%Y-%m-%d"))
        return "|".join(parts)

    def _find_success_log(self, credential_code: str, epoch: str) -> dict[str, Any] | None:
        for row in store.rows(LOG_MODULE):
            if (
                str(row.get("凭证编号") or "") == credential_code
                and str(row.get("幂等键") or "") == epoch
                and row.get("验证结果") == "成功"
            ):
                return row
        return None

    def _find_any_log(self, credential_code: str, epoch: str) -> dict[str, Any] | None:
        for row in store.rows(LOG_MODULE):
            if (
                str(row.get("凭证编号") or "") == credential_code
                and str(row.get("幂等键") or "") == epoch
            ):
                return row
        return None

    def _last_success_log(self, session_code: str) -> dict[str, Any] | None:
        found = None
        for row in store.rows(LOG_MODULE):
            if str(row.get("会话编号") or "") == session_code and row.get("验证结果") == "成功":
                found = row  # 流水按时间追加，遍历到最后一条即最近一次
        return found

    def _check_credential(self, credential: dict[str, Any]) -> list[str]:
        """登录态 + 出口代理 + 反爬配置三项校验，全部通过才算验证成功。"""
        reasons: list[str] = []
        if not str(credential.get("Cookie内容") or "").strip():
            reasons.append("Cookie内容为空")
        expiry = _parse_time(credential.get("有效期至"))
        if expiry is None:
            reasons.append("有效期至缺失或无法解析")
        elif _now() > expiry:
            reasons.append("凭证已过有效期")
        proxy = self._proxy(str(credential.get("绑定代理") or ""))
        if proxy is None:
            reasons.append("绑定代理未配置")
        elif proxy.get("status") != "可用":
            reasons.append(f"绑定代理{proxy.get('节点编号')}不可用")
        site = str(credential.get("目标站点") or "")
        strategy = self._enabled_strategy(site)
        if strategy is None:
            reasons.append("目标域名没有已启用的反爬策略")
        elif not str(strategy.get("UA模拟列表") or "").strip():
            reasons.append("反爬策略未配置UA模拟列表")
        return reasons

    def _append_alert(self, *, trace: str, metric: str, level: str, credential_code: str, site: str, detail: str) -> None:
        """向采集告警队列追加一次可追踪异常；只新增，不改历史告警。"""
        rows = store.rows("alertrule")
        seq = max(
            (int(str(row.get("告警编号", "")).split("-")[-1]) for row in rows
             if str(row.get("告警编号", "")).split("-")[-1].isdigit()),
            default=0,
        ) + 1
        rows.append({
            "id": _next_id(rows),
            "status": "已生效",
            "pending": True,
            "abnormal": True,
            "告警编号": f"ALER-{seq:04d}",
            "监控指标": metric,
            "比较运算符": "-",
            "阈值数值": "-",
            "持续周期": "-",
            "告警级别": level,
            "通知渠道": "采集告警队列",
            "静默时段": "-",
            "追踪编号": trace,
            "关联凭证": credential_code,
            "目标站点": site,
            "异常详情": detail,
        })

    def _append_log(
        self,
        *,
        trace: str,
        session: dict[str, Any],
        credential: dict[str, Any],
        trigger: str,
        result: str,
        detail: str,
        epoch: str = "",
        snapshot: dict[str, str] | None = None,
    ) -> None:
        """验证流水只追加：新的失败或恢复结果生成新记录，不覆盖过去。"""
        snapshot = snapshot or {"ua": "-", "referer": "-", "delay": "-"}
        rows = store.rows(LOG_MODULE)
        rows.append({
            "id": _next_id(rows),
            "status": "已记录",
            "pending": False,
            "abnormal": result != "成功",
            "追踪编号": trace,
            "会话编号": session.get("会话编号"),
            "凭证编号": credential.get("凭证编号"),
            "目标站点": session.get("目标站点"),
            "触发方式": trigger,
            "验证结果": result,
            "绑定代理": credential.get("绑定代理") or session.get("绑定代理") or "-",
            "生效UA": snapshot["ua"],
            "生效Referer": snapshot["referer"],
            "生效延迟": snapshot["delay"],
            "幂等键": epoch,
            "详情": detail,
            "记录时间": _now_str(),
        })
