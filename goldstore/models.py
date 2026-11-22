"""资料模型与载入。原始文字按收到的形式保留。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Role(str, Enum):
    MANAGER = "经理授权"   # 有权更正
    PUBLISH = "发布"       # 可发布
    FORWARD = "转发"       # 新增不了授权


class Pricing(str, Enum):
    GRAM = "gram"      # 按克：金料价＋工费
    FIXED = "fixed"    # 定价款：固定含工费总价


@dataclass(frozen=True)
class PolicyMessage:
    id: str
    store: str
    at: str
    role: Role
    text: str


@dataclass
class Item:
    id: str
    store: str
    name: str
    series: str
    pricing: Pricing
    weight: float | None = None
    gram_price: int | None = None
    labor: int | None = None
    fixed_price: int | None = None
    holdable: bool | None = None   # True 可留 / False 目录禁止 / None 等待门店确认
    hold_note: str = ""

    @property
    def gold(self) -> int:
        """金料价。按克件＝克重×克价；定价件不按克计金料（不计入换旧券门槛）。"""
        if self.pricing is Pricing.GRAM:
            return round((self.weight or 0) * (self.gram_price or 0))
        return 0

    @property
    def list_price(self) -> int:
        if self.pricing is Pricing.GRAM:
            return self.gold + (self.labor or 0)
        return int(self.fixed_price or 0)


@dataclass(frozen=True)
class CustomerMessage:
    id: str
    at: str
    customer: str
    store: str
    text: str
    request: str | None = None
    kind: str | None = None


def _rows(path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_policy(path) -> list[PolicyMessage]:
    return [
        PolicyMessage(id=r["id"], store=r["store"], at=r["at"],
                      role=Role(r["role"]), text=r["text"])
        for r in _rows(path)
    ]


def load_inventory(path) -> dict[str, Item]:
    items = {}
    for r in _rows(path):
        items[r["id"]] = Item(
            id=r["id"], store=r["store"], name=r["name"], series=r["series"],
            pricing=Pricing(r["pricing"]), weight=r.get("weight"),
            gram_price=r.get("gram_price"), labor=r.get("labor"),
            fixed_price=r.get("fixed_price"), holdable=r.get("holdable"),
        )
    return items


def load_customers(path) -> list[CustomerMessage]:
    return [
        CustomerMessage(id=r["id"], at=r["at"], customer=r["customer"],
                        store=r["store"], text=r["text"],
                        request=r.get("request"), kind=r.get("kind"))
        for r in _rows(path)
    ]
