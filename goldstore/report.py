"""交班报告与报价单渲染。"""
from __future__ import annotations

from .policy import describe_rule


def render_quote(q) -> str:
    lines = [f"[{q.qid}] {q.customer}｜{q.store}｜{q.at}｜{q.purpose}"]
    for l in q.lines:
        if l.pricing == "gram":
            head = f"  {l.item_id} {l.name}：按克 金料{l.gold}＋工费{l.labor}＝{l.list_price}"
        else:
            head = f"  {l.item_id} {l.name}：定价 {l.list_price}（含工费）"
        if l.gram_discount:
            head += f"｜按克优惠 −{l.gram_discount}"
        if l.fixed_discount:
            head += f"｜定价优惠 −{l.fixed_discount}"
        lines.append(head)
        for n in l.notes:
            lines.append(f"      · {n}")
    if q.threshold is not None:
        ok = "达标" if q.threshold_ok else "未达标"
        lines.append(f"  实付金料（先扣按克优惠；定价件不计）：{q.gold_material}"
                     f"｜换旧券门槛 {q.threshold} {ok}")
    for v in q.vouchers:
        lines.append(f"  券：{v.name} −{v.amount}（{v.note}，{v.source}）")
    lines.append(f"  合计：{q.total}" + (f"｜确认截止 {q.deadline}" if q.deadline else ""))
    for n in q.notes:
        lines.append(f"  注意：{n}")
    for d in q.declined:
        lines.append(f"  不承诺：{d}")
    if q.applied:
        cited = "、".join(dict.fromkeys(src for src, _ in q.applied))
        lines.append(f"  证据：{cited}（原文见交班报告第一节）")
    return "\n".join(lines)


def render_handover(sim) -> str:
    out = ["=" * 64, "交班报告", "=" * 64, ""]

    out.append("一、每句活动怎样作用到报价")
    for pid, msg in sim.policy.messages.items():
        out.append(f"  {pid}（{msg.role.value}｜{msg.store}｜{msg.at}）：{msg.text}")
        for r in sim.policy.rules:
            if r.source == pid:
                tag = "授权" if r.authorized else "未授权"
                out.append(f"      规则[{tag}] {describe_rule(r)}")
        apps = []
        for q in sim.quotes:
            for src, how in q.applied:
                if src == pid:
                    apps.append(f"{q.qid}（{q.customer}）：{how}")
        apps.extend(sim.policy_notes.get(pid, []))
        if apps:
            for a in dict.fromkeys(apps):
                out.append(f"      → {a}")
        else:
            out.append("      → 本日未作用到任何报价")
        out.append("")

    out.append(f"二、实际占用的件（截至 {sim.now}）")
    active = sim.holds.active_holds(sim.now)
    if not active:
        out.append("  （当前无占用）")
    for h in active:
        out.append(f"  {h.hid} {h.customer}：{'、'.join(h.item_ids)} 留至 {h.expires_at}"
                   f"（{h.status.value}，报价 {h.quote_id}）")
        for n in h.notes:
            out.append(f"      · {n}")
    closed = [h for h in sim.holds.holds if all(h is not a for a in active)]
    if closed:
        out.append("  已结束：")
        for h in closed:
            out.append(f"  {h.hid} {h.customer}：{h.status.value}"
                       f"（原组合 {'、'.join(h.original_items)}）")
            for n in h.notes:
                out.append(f"      · {n}")
    out.append("")

    out.append("三、仍需联系谁确认")
    open_contacts = [c for c in sim.contacts if c["status"] == "open"]
    if not open_contacts:
        out.append("  （无）")
    for c in open_contacts:
        out.append(f"  联系 {c['customer']}（{c['at']} 起）：{c['reason']}")
    resolved = [c for c in sim.contacts if c["status"] != "open"]
    if resolved:
        out.append("  已解决：")
        for c in resolved:
            out.append(f"  {c['customer']}：{c['reason']} ⇒ {c['closed']}")
    pending_w = [w for w in sim.waiting if w.status == "pending"]
    if pending_w:
        out.append("  等候中的预留：")
        for w in pending_w:
            bits = f"  {w.wid} {w.customer}：{'、'.join(w.item_ids)}"
            holders = [f"{iid} 现被 {h.customer} 留至 {h.expires_at}"
                       for iid in w.item_ids
                       if (h := sim.holds.holder_of(iid, sim.now)) is not None]
            if holders:
                bits += f"（{'；'.join(holders)}）"
            if w.until:
                bits += f"（空出后留至 {w.until}）"
            if w.missing:
                bits += f"（缺 {'、'.join(w.missing)}）"
            out.append(bits)
    out.append("")

    out.append("四、本日全部报价")
    for q in sim.quotes:
        out.append(render_quote(q))
        out.append("")
    return "\n".join(out)
