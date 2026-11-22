"""活动群原文 → 结构化店规。

经理授权＝有权更正；发布＝可进报价；转发＝只登记为未授权说法，不进报价。
每条规则都带来源句，报价可引用完整证据；同一句话的适用范围
（某一店 / 按克件 / 定价款 / 单件 / 整套）都落在结构化字段上。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cnnum import chinese_number, parse_time_phrase, to_minutes
from .models import PolicyMessage, Role

# 店规要点（如 P7）→ 内部键
_META_PHRASES = [
    ("按克优惠只从金料价扣", "gram_basis_gold"),
    ("先扣按克优惠，再核换旧券门槛", "threshold_after_gram_discount"),
    ("定价件不计金料门槛", "fixed_excluded_from_threshold"),
    ("展示品能试戴但不能锁货", "display_no_lock"),
    ("套装券只取一张", "set_voucher_single"),
    ("未明说可同用就不叠", "no_implicit_stacking"),
]


@dataclass
class Rule:
    rid: str
    source: str       # 活动群消息编号
    store: str | None
    kind: str
    authorized: bool  # 转发=False
    at: str
    evidence: str     # 来源句原文
    data: dict = field(default_factory=dict)

    def cite(self) -> str:
        return f"{self.source}（{self.at}）：{self.evidence}"


def describe_rule(rule: Rule) -> str:
    d = rule.data
    if rule.kind == "gram_rate":
        return f"按克优惠每克少{d['rate']}（{d['effective']} 起，限{rule.store}按克足金）"
    if rule.kind == "labor_rule":
        return "工费不打折"
    if rule.kind == "set_gram_rule":
        return "二件套按克优惠只减符合件中最重的一件"
    if rule.kind == "voucher":
        bits = [f"{d['name']}：面额{d.get('face')}"]
        if d.get("threshold"):
            bits.append(f"实付金料满{d['threshold']}可用")
        if d.get("once_per_order"):
            bits.append("一单一次")
        if d.get("stack_condition"):
            bits.append(f"与{d.get('stack_with')}同用仅限{d['stack_condition']}")
        if d.get("deadline"):
            bits.append(f"{d['deadline']} 前确认")
        return "，".join(bits)
    if rule.kind == "fixed_discount":
        extra = f"，不跟{'、'.join(d['no_stack'])}叠" if d.get("no_stack") else ""
        return f"定价款按件减{d['amount']}（限{rule.store}{extra}）"
    if rule.kind == "meta":
        return d["phrase"]
    if rule.kind == "grandfather":
        return "已确认且准许留货的继续用旧价"
    if rule.kind == "correction":
        return f"更正 {d['corrects']}"
    if rule.kind == "display_item":
        return f"{d['item']} 是不准留货的展示件"
    if rule.kind == "claim":
        return f"未授权说法（{d.get('topic')}）"
    return rule.kind


def extract_rules(msg: PolicyMessage) -> list[Rule]:
    text = msg.text
    authorized = msg.role is not Role.FORWARD
    rules: list[Rule] = []

    def add(kind, **data):
        rules.append(Rule(rid=f"{msg.id}.{len(rules) + 1}", source=msg.id,
                          store=msg.store, kind=kind, authorized=authorized,
                          at=msg.at, evidence=text, data=data))

    m = re.search(r"更正\s*(P\d+)", text)
    if m:
        add("correction", corrects=m.group(1))

    m = re.search(r"每克少([一二两三四五六七八九十]+)", text)
    if m:
        add("gram_rate", rate=chinese_number(m.group(1)),
            effective=parse_time_phrase(text) or msg.at)

    if "工费不打折" in text:
        add("labor_rule", discounted=False)

    if "二件套" in text and "最重" in text:
        add("set_gram_rule", mode="heaviest_qualifying")

    if "换旧返券" in text and "一单一次" in text:
        m_th = re.search(r"满([一二两三四五六七八九十百千]+)", text)
        m_face = re.search(r"券面([一二两三四五六七八九十百千]+)", text)
        add("voucher", name="换旧返券",
            threshold=chinese_number(m_th.group(1)) if m_th else None,
            face=chinese_number(m_face.group(1)) if m_face else None,
            once_per_order=True, deadline=parse_time_phrase(text))

    if "套装券" in text and "券面" in text:
        # 只有给出券面的句子才是券定义；「套装券只取一张」这类是使用规则（meta）
        m_face = re.search(r"券面([一二两三四五六七八九十百千]+)", text)
        add("voucher", name="套装券",
            face=chinese_number(m_face.group(1)) if m_face else None,
            stack_with="换旧返券" if "换旧返券" in text else None,
            stack_condition="会员" if "会员" in text else None,
            deadline=parse_time_phrase(text))

    m = re.search(r"按件一件减([一二两三四五六七八九十百千]+)", text)
    if m:
        add("fixed_discount", amount=chinese_number(m.group(1)),
            no_stack=["换旧返券"] if "不跟换旧返券叠" in text else [])

    if "继续用旧价" in text:
        add("grandfather", condition="已确认且准许留货")

    m = re.search(r"编号\s*(K\d+)\s*是不准留货的展示件", text)
    if m:
        add("display_item", item=m.group(1), contact_customer="联系顾客" in text)

    for phrase, key in _META_PHRASES:
        if phrase in text:
            add("meta", key=key, phrase=phrase)

    if not authorized:
        topic = "other"
        if "定价" in text and "减" in text:
            topic = "fixed_pricing_discount"
        elif "工费" in text and "免" in text:
            topic = "labor_waiver"
        add("claim", topic=topic)

    return rules


class PolicyBook:
    """随时间接收活动群消息；报价只能看到已到达的规则。"""

    def __init__(self):
        self.messages: dict[str, PolicyMessage] = {}
        self.rules: list[Rule] = []

    def receive(self, msg: PolicyMessage) -> list[Rule]:
        self.messages[msg.id] = msg
        rules = extract_rules(msg)
        self.rules.extend(rules)
        return rules

    def _find(self, kind, store=None, authorized_only=True) -> list[Rule]:
        return [r for r in self.rules if r.kind == kind
                and (store is None or r.store == store)
                and (not authorized_only or r.authorized)]

    def gram_rate_rule(self, store, at) -> Rule | None:
        """at 时刻生效的按克优惠；调价只影响其生效点之后的确认。"""
        best = None
        for r in self._find("gram_rate", store):
            if to_minutes(r.data["effective"]) <= to_minutes(at):
                if best is None or to_minutes(r.data["effective"]) > to_minutes(best.data["effective"]):
                    best = r
        return best

    def gram_rate(self, store, at) -> int | None:
        r = self.gram_rate_rule(store, at)
        return r.data["rate"] if r else None

    def voucher(self, store, name) -> Rule | None:
        return next((r for r in self._find("voucher", store)
                     if r.data.get("name") == name), None)

    def fixed_discount(self, store) -> Rule | None:
        return next(iter(self._find("fixed_discount", store)), None)

    def set_gram_rule(self, store) -> Rule | None:
        return next(iter(self._find("set_gram_rule", store)), None)

    def labor_rule(self, store) -> Rule | None:
        return next(iter(self._find("labor_rule", store)), None)

    def grandfather(self, store) -> Rule | None:
        return next(iter(self._find("grandfather", store)), None)

    def meta_rule(self, key) -> Rule | None:
        return next((r for r in self._find("meta") if r.data.get("key") == key), None)

    def display_items(self) -> list[Rule]:
        return self._find("display_item")

    def rumors(self, topic=None) -> list[Rule]:
        return [r for r in self.rules if r.kind == "claim" and not r.authorized
                and (topic is None or r.data.get("topic") == topic)]

    def confirm_deadline(self, store) -> str | None:
        for r in self._find("voucher", store):
            if r.data.get("deadline"):
                return r.data["deadline"]
        return None
