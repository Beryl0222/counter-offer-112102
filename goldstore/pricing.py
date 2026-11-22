"""报价引擎：把店规作用到一组货品，给出可兑现总价、优惠落点与证据。

- 价签类别：按克（金料价＋工费）/ 定价款（固定含工费总价）。
- 计量口径：按克优惠只从金料价扣；工费不打折。
- 负项与例外：定价件不计金料门槛；展示品能试戴不能锁货；
  转发话术一律不进报价。
- 券口径：换旧返券在顾客提出换旧时按门槛核算；套装券是套装
  （两件及以上）订单的活动权益，按店规计入可兑现总价——会员可与
  换旧返券同用，非会员未明说可同用就不叠（两券择一）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Item, Pricing
from .policy import PolicyBook


@dataclass
class QuoteLine:
    item_id: str
    name: str
    pricing: str
    gold: int
    labor: int
    list_price: int
    gram_discount: int = 0
    fixed_discount: int = 0
    rate_per_gram: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> int:
        return self.list_price - self.gram_discount - self.fixed_discount


@dataclass
class VoucherUse:
    name: str
    amount: int
    note: str
    source: str


@dataclass
class Quote:
    qid: str
    customer: str
    store: str
    at: str
    purpose: str
    lines: list[QuoteLine]
    vouchers: list[VoucherUse]
    gold_material: int            # 计门槛的实付金料（先扣按克优惠；定价件不计）
    threshold: int | None
    threshold_ok: bool | None
    total: int
    deadline: str | None
    applied: list[tuple[str, str]]   # (活动编号, 怎样作用到本报价)
    declined: list[str]              # 未授权/不可叠加而不承诺的说法
    notes: list[str]

    @property
    def item_ids(self) -> list[str]:
        return [l.item_id for l in self.lines]


class QuoteEngine:
    def __init__(self, policy: PolicyBook, items: dict[str, Item]):
        self.policy = policy
        self.items = items

    def quote(self, qid, customer, store, item_ids, at, *, member=False,
              trade_in=False, price_basis=None, purpose="预留报价", asked=()) -> Quote:
        """price_basis: {件号: 确认时刻}，已确认且准许留货的件按确认时价兑现。"""
        policy = self.policy
        basis = dict(price_basis or {})
        items = [self.items[i] for i in item_ids]
        applied: list[tuple[str, str]] = []
        declined: list[str] = []
        notes: list[str] = []

        # ---- 按克优惠落点：二件套只减符合件中最重的一件 ----
        gram_items = [it for it in items if it.pricing is Pricing.GRAM]
        set_rule = policy.set_gram_rule(store)
        discounted_id = None
        if gram_items:
            if len(items) >= 2 and set_rule:
                discounted_id = max(gram_items, key=lambda it: it.weight or 0).id
            else:
                discounted_id = gram_items[0].id

        lines: list[QuoteLine] = []
        for it in items:
            line = QuoteLine(item_id=it.id, name=it.name, pricing=it.pricing.value,
                             gold=it.gold, labor=it.labor or 0, list_price=it.list_price)
            if it.pricing is Pricing.GRAM:
                basis_at = basis.get(it.id, at)
                rate_rule = policy.gram_rate_rule(store, basis_at)
                if basis_at != at and rate_rule is not None:
                    gf = policy.grandfather(store)
                    if gf and it.holdable is True:
                        line.notes.append(
                            f"旧价保留：{basis_at} 已确认且准许留货，按确认时价（{gf.source}）")
                        applied.append((gf.source,
                                        f"{it.id} 按 {basis_at} 确认价兑现，正常调价不影响已确认单"))
                    else:
                        rate_rule = policy.gram_rate_rule(store, at)
                rate = rate_rule.data["rate"] if rate_rule else None
                if rate and it.id == discounted_id:
                    line.rate_per_gram = rate
                    line.gram_discount = round((it.weight or 0) * rate)
                    if len(items) >= 2 and set_rule:
                        line.notes.append(
                            f"每克少{rate}（{rate_rule.source}）：二件套只减符合件中最重的一件，优惠落在本件")
                        applied.append((set_rule.source,
                                        f"二件套按克优惠只落在最重的符合件 {it.id}"))
                    else:
                        line.notes.append(f"每克少{rate}（{rate_rule.source}）：按克优惠落在本件")
                    applied.append((rate_rule.source,
                                    f"{it.id} 按克减 {line.gram_discount}（每克少{rate}）"))
                elif rate:
                    src = set_rule.source if set_rule else rate_rule.source
                    line.notes.append(f"按克优惠不落在本件（二件套只减最重的符合件，{src}）")
            else:
                fd = policy.fixed_discount(store)
                if fd:
                    line.fixed_discount = fd.data["amount"]
                    line.notes.append(f"定价款按件减{fd.data['amount']}（{fd.source}）")
                    applied.append((fd.source, f"{it.id} 定价减 {fd.data['amount']}"))
                    for ns in fd.data.get("no_stack", []):
                        notes.append(f"定价减{fd.data['amount']}不跟{ns}叠（{fd.source}）")
                else:
                    line.notes.append("定价款：本店无已授权的定价优惠")
            if it.holdable is False:
                line.notes.append(f"不允许留货（{it.hold_note or '目录禁止'}），可试戴")
                notes.append(f"{it.id} 不允许留货，不能锁货")
            elif it.holdable is None:
                line.notes.append("留货资格待门店确认")
                notes.append(f"{it.id} 留货资格待门店确认，总价以所有件均可留为前提")
            lines.append(line)

        labor_rule = policy.labor_rule(store)
        if labor_rule and any(l.labor for l in lines):
            applied.append((labor_rule.source, "工费不打折，全额计入"))

        # ---- 金料门槛：先扣按克优惠再核；定价件不计 ----
        gold_material = (
            sum(l.gold for l in lines if l.pricing == Pricing.GRAM.value)
            - sum(l.gram_discount for l in lines)
        )

        vouchers: list[VoucherUse] = []
        threshold = None
        threshold_ok = None
        ti = policy.voucher(store, "换旧返券")
        if trade_in:
            if ti is None:
                declined.append(f"换旧返券不是本店（{store}）活动，不适用")
            else:
                threshold = ti.data["threshold"]
                threshold_ok = threshold is None or gold_material >= threshold
                for key in ("threshold_after_gram_discount", "fixed_excluded_from_threshold"):
                    mr = policy.meta_rule(key)
                    if mr:
                        applied.append((mr.source, mr.data["phrase"]))
                if threshold_ok:
                    vouchers.append(VoucherUse(
                        "换旧返券", ti.data["face"] or 0,
                        f"一单一次；实付金料{gold_material}满{threshold}", ti.source))
                    applied.append((ti.source,
                                    f"换旧返券 −{ti.data['face']}（实付金料{gold_material}≥{threshold}）"))
                else:
                    notes.append(
                        f"实付金料{gold_material}未满{threshold}（差{threshold - gold_material}），"
                        "换旧返券不可用")
                    applied.append((ti.source,
                                    f"实付金料{gold_material}未满{threshold}，换旧返券不可用"))
        st = policy.voucher(store, "套装券")
        if st:
            if len(items) >= 2:
                used_ti = any(v.name == "换旧返券" for v in vouchers)
                if used_ti:
                    if member and st.data.get("stack_condition") == "会员":
                        vouchers.append(VoucherUse(
                            "套装券", st.data["face"] or 0,
                            "会员可与换旧返券同用；两个套装券只取一张", st.source))
                        applied.append((st.source, f"套装券 −{st.data['face']}（会员与换旧返券同用）"))
                    else:
                        declined.append(
                            f"套装券与换旧返券同用仅限会员（{st.source}）；"
                            "未明说可同用就不叠，非会员两券只能择一（面额相同，按一张计）")
                else:
                    vouchers.append(VoucherUse(
                        "套装券", st.data["face"] or 0,
                        "套装可用；两个套装券只取一张", st.source))
                    applied.append((st.source, f"套装券 −{st.data['face']}"))
            else:
                notes.append(f"单件不成套装，套装券不适用（{st.source}）")

        # ---- 顾客问到的说法：只认授权依据 ----
        for topic in asked or ():
            if topic == "fixed_gram_discount":
                rumors = policy.rumors("fixed_pricing_discount")
                src = "、".join(r.source for r in rumors) if rumors else "无出处"
                basis_txt = []
                meta = policy.meta_rule("gram_basis_gold")
                rate = policy.gram_rate_rule(store, at)
                if meta:
                    basis_txt.append(f"{meta.source} 按克优惠只从金料价扣、定价件不按克计")
                if rate:
                    basis_txt.append(f"{rate.source} 活动仅限按克的足金")
                declined.append(
                    f"“定价款每克减”只有转发说法（{src}），无授权人澄清，不能承诺；"
                    f"{'；'.join(basis_txt)}。可按原价试戴，或用已确定的按克优惠。")
            elif topic == "labor_waiver":
                rumors = policy.rumors("labor_waiver")
                src = "、".join(r.source for r in rumors) if rumors else "无出处"
                declined.append(
                    f"“第二件工费免”只有转发说法（{src}），与“工费不打折”冲突，"
                    "无授权人澄清前不承诺。")

        deadline = policy.confirm_deadline(store) if (vouchers or trade_in) else None
        total = sum(l.subtotal for l in lines) - sum(v.amount for v in vouchers)
        return Quote(qid=qid, customer=customer, store=store, at=at, purpose=purpose,
                     lines=lines, vouchers=vouchers, gold_material=gold_material,
                     threshold=threshold, threshold_ok=threshold_ok, total=total,
                     deadline=deadline, applied=applied, declined=declined, notes=notes)
