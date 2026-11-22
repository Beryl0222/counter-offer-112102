"""留货登记：同一库存单件同一时刻只能留住给一个顾客。

接待员只有在顾客接受完整组合和有效期后才留货；暂停兑现的单
在等待答复期间仍然占住可合法留的件，已不允许留货的件必须释放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .cnnum import to_minutes
from .models import Item


class HoldStatus(str, Enum):
    ACTIVE = "生效"
    PAUSED = "暂停兑现"
    EXPIRED = "到期未取"
    RELEASED = "已释放"
    FULFILLED = "已成交"


class HoldError(Exception):
    pass


@dataclass
class Hold:
    hid: str
    customer: str
    store: str
    item_ids: list[str]
    created_at: str
    expires_at: str
    source: str
    quote_id: str | None = None
    status: HoldStatus = HoldStatus.ACTIVE
    context: dict = field(default_factory=dict)   # member / trade_in 等报价上下文
    notes: list[str] = field(default_factory=list)
    history: list[tuple[str, HoldStatus]] = field(default_factory=list)  # (时刻, 状态)
    original_items: list[str] = field(default_factory=list)              # 初始组合

    def status_at(self, at: str) -> HoldStatus:
        s = self.status if not self.history else self.history[0][1]
        for t, st in self.history:
            if to_minutes(t) <= to_minutes(at):
                s = st
            else:
                break
        return s


@dataclass
class Waiting:
    wid: str
    customer: str
    store: str
    created_at: str
    kind: str                 # hold=等候留货 / notify=空出提醒
    item_ids: list[str]
    source: str
    until: str | None = None
    duration_min: int | None = None
    all_or_nothing: bool = False
    refuse: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 例如缺有效期，需先联系补全
    status: str = "pending"   # pending / done / cancelled
    note: str = ""


class HoldRegistry:
    def __init__(self, items: dict[str, Item]):
        self.items = items
        self.holds: list[Hold] = []
        self._seq = 0

    def _occupies(self, hold: Hold, now: str) -> bool:
        return hold.status_at(now) in (HoldStatus.ACTIVE, HoldStatus.PAUSED) \
            and to_minutes(hold.created_at) <= to_minutes(now) \
            and to_minutes(now) < to_minutes(hold.expires_at)

    def holder_of(self, item_id: str, now: str) -> Hold | None:
        for h in self.holds:
            if item_id in h.item_ids and self._occupies(h, now):
                return h
        return None

    def conflicts(self, item_ids, now) -> dict[str, Hold]:
        out = {}
        for iid in item_ids:
            h = self.holder_of(iid, now)
            if h:
                out[iid] = h
        return out

    def confirm(self, *, customer, store, item_ids, until, now, source,
                quote_id=None, context=None, allow_unconfirmed=False, notes=()) -> Hold:
        for iid in item_ids:
            it = self.items.get(iid)
            if it is None:
                raise HoldError(f"{iid} 不在库存")
            if it.store != store:
                raise HoldError(f"{iid} 属{it.store}，不能在{store}留")
            if it.holdable is False:
                raise HoldError(f"{iid}（{it.name}）不允许留货：{it.hold_note or '目录禁止'}")
            if it.holdable is None and not allow_unconfirmed:
                raise HoldError(f"{iid}（{it.name}）留货资格待门店确认")
        for iid, h in self.conflicts(item_ids, now).items():
            raise HoldError(f"{iid} 已被 {h.customer} 留住至 {h.expires_at}（{h.hid}）")
        self._seq += 1
        hold = Hold(hid=f"H{self._seq}", customer=customer, store=store,
                    item_ids=list(item_ids), created_at=now, expires_at=until,
                    source=source, quote_id=quote_id, context=dict(context or {}),
                    notes=list(notes), original_items=list(item_ids))
        hold.history.append((now, HoldStatus.ACTIVE))
        self.holds.append(hold)
        return hold

    def release_items(self, hold: Hold, item_ids, now, reason):
        for iid in item_ids:
            if iid in hold.item_ids:
                hold.item_ids.remove(iid)
                hold.notes.append(f"{now} 释放 {iid}：{reason}")
        if not hold.item_ids and hold.status not in (HoldStatus.EXPIRED, HoldStatus.FULFILLED):
            self.set_status(hold, HoldStatus.RELEASED, at=now)

    def set_status(self, hold: Hold, status: HoldStatus, note="", at=None):
        hold.status = status
        if at is not None:
            hold.history.append((at, status))
        if note:
            hold.notes.append(note)

    def expire_due(self, now) -> list[tuple[Hold, list[str]]]:
        """到点未取的预留释放，返回 (留货单, 释放件) 列表。"""
        freed = []
        for h in self.holds:
            if h.status in (HoldStatus.ACTIVE, HoldStatus.PAUSED) \
                    and to_minutes(h.expires_at) <= to_minutes(now):
                self.set_status(h, HoldStatus.EXPIRED,
                                f"{now} 到期未取，释放 {', '.join(h.item_ids)}", at=now)
                freed.append((h, list(h.item_ids)))
        return freed

    def active_holds(self, now) -> list[Hold]:
        return [h for h in self.holds if self._occupies(h, now)]
