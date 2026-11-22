"""把一天的消息按时刻接进来：活动群、客服来信、到点时钟。

处理顺序即门店当地时序；报价只能看到已到达的活动群规则。
"""
from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field

from .cnnum import parse_duration_minutes, parse_time_phrase, to_hhmm, to_minutes
from .holds import HoldError, HoldRegistry, HoldStatus, Waiting
from .models import CustomerMessage, Item, PolicyMessage, Role
from .policy import PolicyBook, describe_rule
from .pricing import Quote, QuoteEngine

END_OF_DAY = "23:59"   # 「今日」→ 今日打烊


@dataclass
class Intent:
    kind: str = "review"
    items: list[str] = field(default_factory=list)
    notify: list[str] = field(default_factory=list)
    until: str | None = None
    duration_min: int | None = None
    member: bool = False
    trade_in: bool = False
    question: str | None = None
    all_or_nothing: bool = False
    refuse: list[str] = field(default_factory=list)
    confirm_keepable: bool = False
    ask_before_new: bool = False
    wants_total: bool = False


def interpret(msg: CustomerMessage, items: dict[str, Item],
              known: dict[str, Intent]) -> Intent:
    """把一条客服来信读成结构化意图；读不懂的交给人工（review）。"""
    text = msg.text
    intent = Intent()
    ids = list(dict.fromkeys(re.findall(r"K\d+", text)))
    for it in items.values():   # 名称 → 编号（同店货品）
        if it.store == msg.store and it.name in text and it.id not in ids:
            ids.append(it.id)
    intent.items = ids
    intent.member = "会员" in text
    intent.trade_in = "换旧" in text or "旧货换" in text
    intent.wants_total = "总价" in text
    if "不接受单独一件" in text or "两件都留" in text:
        intent.all_or_nothing = True
    m = re.search(r"(?:不接受改为|别替我换成)\s*(K\d+)", text)
    if m:
        intent.refuse.append(m.group(1))
        ids = [i for i in ids if i not in intent.refuse]   # 拒绝件不进组合
        intent.items = ids
    until = parse_time_phrase(text)
    duration = parse_duration_minutes(text)

    if msg.kind == "柜台回条":
        intent.kind = "counter_commitment"
        ref = known.get(msg.request) if msg.request else None
        if not intent.items and ref:
            intent.items = list(ref.items)
        intent.until = until or (ref.until if ref else None)
        intent.member = intent.member or (ref.member if ref else False)
        intent.trade_in = intent.trade_in or (ref.trade_in if ref else False)
        return intent
    if "先确认可留下的" in text:
        intent.kind = "instruction"
        intent.confirm_keepable = True
        intent.ask_before_new = "再问我" in text
        return intent
    if "提醒我" in text or "空出来" in text:
        intent.kind = "tryon_notify"
        m2 = re.search(r"(K\d+)\s*空出来", text)
        intent.notify = [m2.group(1)] if m2 else []
        intent.items = [i for i in ids if i not in intent.notify]
        return intent
    if "重新问" in text and "留" in text:
        intent.kind = "hold_request"
        intent.until = until
        intent.duration_min = duration
        return intent
    if "若有" in text and ("都留" in text or "两件" in text):
        intent.kind = "standing_hold"
        intent.until = END_OF_DAY if "今日" in text else until
        return intent
    if "若有" in text and "想留" in text:
        intent.kind = "conditional_hold"
        intent.until = until
        intent.duration_min = duration
        if "每克减" in text and "定价" in text:
            intent.question = "fixed_gram_discount"
        return intent
    if "想留" in text or "看看总价" in text or "留" in text:
        intent.kind = "hold_request"
        intent.until = until
        intent.duration_min = duration
        return intent
    return intent


class Simulation:
    def __init__(self, policy_msgs, items, customer_msgs):
        self.items = items
        self.policy_msgs = policy_msgs
        self.customer_msgs = customer_msgs
        self.policy = PolicyBook()
        self.engine = QuoteEngine(self.policy, self.items)
        self.holds = HoldRegistry(self.items)
        self.waiting: list[Waiting] = []
        self.quotes: list[Quote] = []
        self.log: list[str] = []
        self.contacts: list[dict] = []
        self.policy_notes: dict[str, list[str]] = {}
        self.options: dict[str, list[Quote]] = {}
        self.intents: dict[str, Intent] = {}
        self.last_quote: dict[str, Quote] = {}
        self.now = "00:00"
        self._qseq = 0
        self._wseq = 0
        self._events: list[tuple] = []
        self._eseq = 0

    # ---------- 工具 ----------
    def _log(self, at, text):
        self.log.append(f"[{at}] {text}")

    def _note_policy(self, pid, text):
        self.policy_notes.setdefault(pid, []).append(text)

    def _add_contact(self, customer, at, reason, ref=""):
        self.contacts.append({"customer": customer, "at": at, "reason": reason,
                              "ref": ref, "status": "open", "closed": ""})

    def _close_contacts(self, customer, ref, at, note):
        for c in self.contacts:
            if c["customer"] == customer and c["status"] == "open" \
                    and (not ref or c["ref"] == ref):
                c["status"] = "resolved"
                c["closed"] = f"{at} {note}"

    def _quote(self, customer, store, item_ids, at, **kw) -> Quote:
        self._qseq += 1
        q = self.engine.quote(f"Q{self._qseq}", customer, store, item_ids, at, **kw)
        self.quotes.append(q)
        self.last_quote[customer] = q
        return q

    def _log_quote(self, q: Quote):
        self._log(q.at, f"  ↳ 报价 {q.qid}（{q.purpose}）：{'、'.join(q.item_ids)} "
                        f"合计 {q.total}" + (f"；确认截止 {q.deadline}" if q.deadline else ""))
        for n in q.notes:
            self._log(q.at, f"      注意：{n}")
        for d in q.declined:
            self._log(q.at, f"      不承诺：{d}")

    def _push(self, at, kind, payload):
        self._eseq += 1
        heapq.heappush(self._events, (to_minutes(at), self._eseq, kind, payload))

    def _wait(self, msg, intent, *, kind, until=None, missing=(), note="") -> Waiting:
        self._wseq += 1
        w = Waiting(wid=f"W{self._wseq}", customer=msg.customer, store=msg.store,
                    created_at=msg.at, kind=kind, item_ids=list(intent.items),
                    source=msg.id, until=until or intent.until,
                    duration_min=intent.duration_min,
                    all_or_nothing=intent.all_or_nothing, refuse=list(intent.refuse),
                    missing=list(missing), note=note)
        self.waiting.append(w)
        if missing:
            self._add_contact(msg.customer, msg.at,
                              f"等候 {'、'.join(w.item_ids)}；缺{'、'.join(missing)}，需联系确认",
                              ref=w.wid)
        return w

    # ---------- 主循环 ----------
    def run(self):
        for m in self.policy_msgs:
            self._push(m.at, "policy", m)
        for m in self.customer_msgs:
            self._push(m.at, "customer", m)
        while self._events:
            _, _, kind, payload = heapq.heappop(self._events)
            if kind == "clock":
                self.now = payload
                self._on_clock(payload)
            else:
                self.now = payload.at
                if kind == "policy":
                    self._on_policy(payload)
                else:
                    self._on_customer(payload)
        return self

    # ---------- 活动群 ----------
    def _on_policy(self, msg: PolicyMessage):
        rules = self.policy.receive(msg)
        self._log(msg.at, f"活动群 {msg.id}（{msg.role.value}｜{msg.store}）：{msg.text}")
        for r in rules:
            self._log(msg.at, f"  ↳ 规则[{'授权' if r.authorized else '未授权'}] {describe_rule(r)}")
        if msg.role is Role.FORWARD:
            self._log(msg.at, f"  ↳ {msg.id} 是转发，新增不了授权：只登记，不进报价。")
            self._note_policy(msg.id, "转发说法，无授权；未作用于任何报价，顾客问起按不能承诺答复")
            for r in rules:
                if r.kind == "claim" and r.data.get("topic") == "labor_waiver":
                    labor = self.policy.labor_rule(msg.store)
                    if labor:
                        self._log(msg.at, f"  ↳ 与 {labor.source}「工费不打折」冲突，"
                                          "无授权人澄清：相关优惠先不承诺。")
                        self._note_policy(msg.id, f"与 {labor.source}「工费不打折」冲突；无授权澄清，未承诺")
        for r in rules:
            if r.kind == "gram_rate" and any(x.kind == "correction" for x in rules):
                self._log(msg.at, f"  ↳ 调价：{r.data['effective']} 起每克少{r.data['rate']}；"
                                  "已确认且准许留货的继续用旧价，后续确认按新价。")
                self._note_policy(msg.id, f"按克优惠自 {r.data['effective']} 起每克少{r.data['rate']}；"
                                          "只影响后续确认，已确认单按承诺兑现到期限")
            if r.kind == "display_item":
                self._apply_display_item(r, msg.at)

    def _apply_display_item(self, rule, now):
        iid = rule.data["item"]
        it = self.items[iid]
        it.holdable = False
        it.hold_note = f"{rule.source} 明确为不准留货的展示件"
        self._log(now, f"  ↳ {iid}（{it.name}）确认为展示件：能试戴，不能锁货。")
        self._note_policy(rule.source, f"{iid} 确认为不准留货的展示件")
        for hold in list(self.holds.active_holds(now)):
            if iid not in hold.item_ids:
                continue
            self.holds.set_status(hold, HoldStatus.PAUSED,
                                  f"{now} {rule.source} 揭示 {iid} 不允许留货，暂停兑现",
                                  at=now)
            self.holds.release_items(hold, [iid], now,
                                     f"{rule.source}：展示件不允许留货，必须释放")
            kept = "、".join(hold.item_ids)
            self._log(now, f"  ↳ 暂停 {hold.hid}（{hold.customer}）：{iid} 释放；"
                           f"可合法留的 {kept} 继续为其保留至 {hold.expires_at}。")
            self._note_policy(rule.source,
                              f"{hold.hid}（{hold.customer}）暂停兑现：{iid} 释放，{kept} 保留")
            self._add_contact(hold.customer, now,
                              f"{iid} 误入已签字预留（{rule.source} 明确为展示件）；"
                              "请按原组合确认可守住的部分与替代选择", ref=hold.hid)
            ctx = hold.context
            basis = {i: hold.created_at for i in hold.item_ids}
            q = self._quote(hold.customer, hold.store, hold.item_ids, now,
                            member=ctx.get("member", False),
                            trade_in=ctx.get("trade_in", False),
                            price_basis=basis,
                            purpose="暂停单重报：原组合可守住的部分（旧价保留）")
            hold.quote_id = q.qid
            self._log_quote(q)
            self._build_options(hold, now)

    def _build_options(self, hold, now):
        """给暂停单的顾客：可守住部分之外的替代选择，以及换一件为何破门槛的对照。"""
        ctx = hold.context
        kept = list(hold.item_ids)
        basis = {i: hold.created_at for i in kept}
        opts = []
        for it in self.items.values():
            if it.store != hold.store or it.id in kept or it.holdable is not True:
                continue
            q = self._quote(hold.customer, hold.store, kept + [it.id], now,
                            member=ctx.get("member", False),
                            trade_in=ctx.get("trade_in", False),
                            price_basis=basis,
                            purpose=f"替代选择：{'、'.join(kept)} 加 {it.id}（需顾客确认）")
            opts.append(q)
        for it in self.items.values():
            if it.store != hold.store or it.id in kept or it.holdable is not True:
                continue
            q = self._quote(hold.customer, hold.store, [it.id], now,
                            member=ctx.get("member", False),
                            trade_in=ctx.get("trade_in", False),
                            purpose=f"门槛对照：若把 {'、'.join(kept)} 换成 {it.id} 单件")
            opts.append(q)
        self.options[hold.customer] = opts
        for q in opts:
            self._log_quote(q)

    # ---------- 客服来信 ----------
    def _on_customer(self, msg: CustomerMessage):
        intent = interpret(msg, self.items, self.intents)
        self.intents[msg.id] = intent
        self._log(msg.at, f"客服 {msg.id}（{msg.customer}｜{msg.store}）：{msg.text}")
        handler = {
            "hold_request": self._do_hold_request,
            "counter_commitment": self._do_counter_commitment,
            "conditional_hold": self._do_conditional_hold,
            "tryon_notify": self._do_tryon_notify,
            "instruction": self._do_instruction,
            "standing_hold": self._do_standing_hold,
        }.get(intent.kind, self._do_review)
        handler(msg, intent)

    def _supersede_waitings(self, msg, intent):
        for w in self.waiting:
            if (w.customer == msg.customer and w.status == "pending"
                    and w.kind == "hold" and set(w.item_ids) == set(intent.items)):
                w.status = "cancelled"
                w.note = (w.note + "；" if w.note else "") + f"被 {msg.id} 取代"
                self._close_contacts(msg.customer, w.wid, msg.at, f"被 {msg.id} 取代")

    def _do_hold_request(self, msg, intent):
        at = msg.at
        self._supersede_waitings(msg, intent)
        q = self._quote(msg.customer, msg.store, intent.items, at,
                        member=intent.member, trade_in=intent.trade_in,
                        purpose="预留报价",
                        asked=([intent.question] if intent.question else []))
        self._log_quote(q)
        until = intent.until
        if until is None and intent.duration_min:
            until = to_hhmm(to_minutes(at) + intent.duration_min)
        if until is None:
            self._log(at, "  ↳ 顾客未接受有效期，接待员不能留货；需联系确认留到几点。")
            self._wait(msg, intent, kind="hold", missing=["有效期"])
            return
        blocked = [i for i in intent.items if self.items[i].holdable is False]
        pending = [i for i in intent.items if self.items[i].holdable is None]
        if blocked:
            names = "、".join(f"{i}（{self.items[i].hold_note}）" for i in blocked)
            self._log(at, f"  ↳ 不能留货：{names} 不允许留货，只能试戴。")
            return
        if pending:
            self._log(at, f"  ↳ {'、'.join(pending)} 留货资格待门店确认，暂不能系统确认；"
                          "可试戴，或待门店确认／柜台手工登记。")
            return
        conflicts = self.holds.conflicts(intent.items, at)
        if conflicts:
            got = "；".join(f"{iid} 已被 {h.customer} 留住至 {h.expires_at}"
                            for iid, h in conflicts.items())
            self._log(at, f"  ↳ 不能留货：{got}。可按已确定部分或原价试戴。")
            if intent.refuse:
                self._log(at, f"  ↳ 顾客声明不接受改为 {'、'.join(intent.refuse)}，不替换。")
            w = self._wait(msg, intent, kind="hold", until=until)
            self._add_contact(msg.customer, at,
                              f"等候 {'、'.join(intent.items)} 释放后留至 {until}（{w.wid}）"
                              + (f"；不接受改为 {'、'.join(intent.refuse)}" if intent.refuse else ""),
                              ref=w.wid)
            return
        hold = self.holds.confirm(
            customer=msg.customer, store=msg.store, item_ids=intent.items,
            until=until, now=at, source=msg.id, quote_id=q.qid,
            context={"member": intent.member, "trade_in": intent.trade_in})
        self._log(at, f"  ↳ 留货 {hold.hid}：{'、'.join(hold.item_ids)} 至 {until}；"
                      f"总价 {q.total}" + (f"，确认截止 {q.deadline}" if q.deadline else ""))
        if until != END_OF_DAY:
            self._push(until, "clock", until)

    def _do_counter_commitment(self, msg, intent):
        at = msg.at
        q = self.last_quote.get(msg.customer)
        try:
            hold = self.holds.confirm(
                customer=msg.customer, store=msg.store, item_ids=intent.items,
                until=intent.until, now=at, source=msg.id,
                quote_id=q.qid if q else None,
                context={"member": intent.member, "trade_in": intent.trade_in},
                allow_unconfirmed=True,
                notes=["柜台手工签字单（旧操作），系统补登记"])
        except HoldError as e:
            self._log(at, f"  ↳ 柜台回条无法登记：{e}")
            return
        for iid in intent.items:
            if self.items[iid].holdable is None:
                self._log(at, f"  ↳ 注意：{iid} 留货资格仍待门店确认，此单为柜台手工承诺。")
                self._add_contact(msg.customer, at,
                                  f"{iid} 留货资格待门店确认（柜台已手工签字 {hold.hid}）",
                                  ref=hold.hid)
        self._log(at, f"  ↳ 登记柜台承诺 {hold.hid}：{'、'.join(hold.item_ids)} 至 {hold.expires_at}"
                      + (f"，引用报价 {q.qid} 总价 {q.total}" if q else ""))
        if hold.expires_at != END_OF_DAY:
            self._push(hold.expires_at, "clock", hold.expires_at)

    def _do_conditional_hold(self, msg, intent):
        at = msg.at
        if intent.question:
            self._answer_question(msg, intent)
        conflicts = self.holds.conflicts(intent.items, at)
        missing = []
        if intent.until is None and intent.duration_min is None:
            missing.append("有效期")
        note = ""
        if conflicts:
            note = "；".join(f"{iid} 已被 {h.customer} 留至 {h.expires_at}"
                             for iid, h in conflicts.items())
            self._log(at, f"  ↳ 暂不能留：{note}。登记等候。")
        w = self._wait(msg, intent, kind="hold", missing=missing, note=note)
        if missing:
            self._log(at, f"  ↳ 等候单 {w.wid} 缺有效期，需联系 {msg.customer} 确认留到几点；"
                          "未接受完整组合和有效期前不留货。")

    def _answer_question(self, msg, intent):
        at = msg.at
        if intent.question != "fixed_gram_discount":
            return
        rumors = self.policy.rumors("fixed_pricing_discount")
        src = "、".join(r.source for r in rumors) if rumors else "无出处"
        basis = []
        meta = self.policy.meta_rule("gram_basis_gold")
        rate = self.policy.gram_rate_rule(msg.store, at)
        if meta:
            basis.append(f"{meta.source} 按克优惠只从金料价扣、定价件不按克计")
        if rate:
            basis.append(f"{rate.source} 活动仅限按克的足金")
        self._log(at, f"  ↳ 答复 {msg.customer}：定价款“每克减”只有转发说法（{src}），"
                      f"无授权人澄清，不能承诺；{'；'.join(basis)}。"
                      "可按原价试戴，或用已确定的按克优惠。")
        for r in rumors:
            self._note_policy(r.source, f"{msg.customer} 问定价款每克减：转发无授权，未承诺")

    def _do_tryon_notify(self, msg, intent):
        at = msg.at
        mr = self.policy.meta_rule("display_no_lock")
        src = f"（{mr.source} 展示品能试戴但不能锁货）" if mr else ""
        if intent.items:
            self._log(at, f"  ↳ 试戴 {'、'.join(intent.items)}：试戴不锁货{src}。")
        for iid in intent.notify:
            holder = self.holds.holder_of(iid, at)
            note = f"{iid} 现被 {holder.customer} 留至 {holder.expires_at}" if holder else f"{iid} 当前可留"
            w = self._wait(msg, intent, kind="notify", note=note)
            w.item_ids = [iid]
            self._log(at, f"  ↳ 登记空出提醒 {w.wid}：{iid} 空出时提醒 {msg.customer}（{note}）。")

    def _do_instruction(self, msg, intent):
        at = msg.at
        paused = [h for h in self.holds.holds
                  if h.customer == msg.customer and h.status == HoldStatus.PAUSED]
        for h in paused:
            self.holds.set_status(h, HoldStatus.ACTIVE,
                                  f"{at} 顾客确认可留下的部分（{msg.id}）", at=at)
            self._log(at, f"  ↳ {msg.customer} 决定：确认可留下的 {'、'.join(h.item_ids)}"
                          f"（{h.hid}，按承诺兑现至 {h.expires_at}）。")
        if intent.refuse:
            self._log(at, f"  ↳ 不替换为 {'、'.join(intent.refuse)}。")
        self._close_contacts(msg.customer, None, at, f"顾客已确认可留下部分（{msg.id}）")
        if intent.ask_before_new:
            self._log(at, "  ↳ 改用新品组合需再问顾客；替代选择保持“需顾客确认”。")
            self._add_contact(msg.customer, at,
                              "替代组合（加 K3／加 K4）需顾客确认后方可兑现", ref="options")

    def _do_standing_hold(self, msg, intent):
        at = msg.at
        until = intent.until or END_OF_DAY
        conflicts = self.holds.conflicts(intent.items, at)
        if conflicts:
            got = "、".join(f"{iid} 被 {h.customer} 留至 {h.expires_at}"
                            for iid, h in conflicts.items())
            w = self._wait(msg, intent, kind="hold", until=until)
            self._log(at, f"  ↳ {got}；登记等候 {w.wid}：{'、'.join(intent.items)} 两件齐才留"
                          "（不接受单独一件），空出即按当前活动确认。")
            return
        q = self._quote(msg.customer, msg.store, intent.items, at, purpose="预留报价")
        self._log_quote(q)
        hold = self.holds.confirm(customer=msg.customer, store=msg.store,
                                  item_ids=intent.items, until=until, now=at,
                                  source=msg.id, quote_id=q.qid)
        self._log(at, f"  ↳ 留货 {hold.hid}：{'、'.join(hold.item_ids)} 至 {until}。")
        if until != END_OF_DAY:
            self._push(until, "clock", until)

    def _do_review(self, msg, intent):
        self._log(msg.at, "  ↳ 来信意图需人工处理。")

    # ---------- 到点 ----------
    def _on_clock(self, now):
        expired = self.holds.expire_due(now)
        if not expired:
            return
        for hold, freed in expired:
            self._log(now, f"到点：{hold.hid}（{hold.customer}）预留到期未取，"
                           f"释放 {'、'.join(freed)}。")
            self._add_contact(hold.customer, now,
                              f"预留 {'、'.join(freed)} 到期未取已释放；"
                              "部分件若被他人留走，原组合不视为仍可恢复，"
                              "如仍需购买按当前活动重新报价", ref=hold.hid)
        self._process_waiting(now)

    def _process_waiting(self, now):
        # 先满足信息齐全的留货等候（按登记先后），再发空出提醒
        for w in self.waiting:
            if w.kind != "hold" or w.status != "pending" or w.missing:
                continue
            if self.holds.conflicts(w.item_ids, now):
                continue
            try:
                q = self._quote(w.customer, w.store, w.item_ids, now,
                                purpose="等候补位确认（按确认时活动）")
                until = w.until or (to_hhmm(to_minutes(now) + w.duration_min)
                                    if w.duration_min else END_OF_DAY)
                hold = self.holds.confirm(customer=w.customer, store=w.store,
                                          item_ids=w.item_ids, until=until, now=now,
                                          source=w.source, quote_id=q.qid)
            except HoldError:
                continue
            w.status = "done"
            self._log_quote(q)
            self._log(now, f"  ↳ 等候单 {w.wid}（{w.customer}）补位成功："
                           f"{'、'.join(w.item_ids)} 留至 {until}，总价 {q.total}。")
            self._add_contact(w.customer, now,
                              f"等候的 {'、'.join(w.item_ids)} 已留住至 {until}（{hold.hid}），"
                              f"总价 {q.total}，请与顾客确认", ref=hold.hid)
            if until != END_OF_DAY:
                self._push(until, "clock", until)
        for w in self.waiting:
            if w.kind != "notify" or w.status != "pending":
                continue
            if all(self.holds.holder_of(iid, now) is None for iid in w.item_ids):
                w.status = "done"
                self._log(now, f"  ↳ 提醒 {w.customer}：{'、'.join(w.item_ids)} 已空出（{w.wid}）。")
                self._add_contact(w.customer, now,
                                  f"{'、'.join(w.item_ids)} 已空出，请提醒顾客（{w.wid}）",
                                  ref=w.wid)
