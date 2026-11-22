"""店规解析、报价、留货与一日流程的单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldstore.cnnum import (chinese_number, parse_duration_minutes,  # noqa: E402
                             parse_time_phrase)
from goldstore.holds import HoldError, HoldRegistry, HoldStatus  # noqa: E402
from goldstore.models import load_customers, load_inventory, load_policy  # noqa: E402
from goldstore.pipeline import Simulation  # noqa: E402
from goldstore.policy import PolicyBook  # noqa: E402
from goldstore.pricing import QuoteEngine  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"


def load_all():
    policy = load_policy(DATA / "policy.json")
    items = load_inventory(DATA / "inventory.json")
    customers = load_customers(DATA / "customers.json")
    return policy, items, customers


def full_book():
    policy, _, _ = load_all()
    book = PolicyBook()
    for m in policy:
        book.receive(m)
    return book


class CnNumTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(chinese_number("四十"), 40)
        self.assertEqual(chinese_number("二十"), 20)
        self.assertEqual(chinese_number("一百"), 100)
        self.assertEqual(chinese_number("两百"), 200)
        self.assertEqual(chinese_number("四千"), 4000)

    def test_time_phrases(self):
        self.assertEqual(parse_time_phrase("从十点半起"), "10:30")
        self.assertEqual(parse_time_phrase("中午十二点前确认"), "12:00")
        self.assertEqual(parse_time_phrase("留到十一点半"), "11:30")
        self.assertIsNone(parse_time_phrase("今日若有"))

    def test_duration(self):
        self.assertEqual(parse_duration_minutes("留一刻钟"), 15)
        self.assertIsNone(parse_duration_minutes("留到十一点半"))


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.book = full_book()

    def test_gram_rate_windows(self):
        self.assertEqual(self.book.gram_rate("东桥", "10:15"), 40)
        self.assertEqual(self.book.gram_rate("东桥", "10:29"), 40)
        self.assertEqual(self.book.gram_rate("东桥", "10:30"), 20)
        self.assertEqual(self.book.gram_rate("东桥", "11:31"), 20)

    def test_store_scope(self):
        # 字面活动同名不代表规则相同：西渡没有东桥的按克优惠与券
        self.assertIsNone(self.book.gram_rate("西渡", "10:15"))
        self.assertIsNone(self.book.voucher("西渡", "换旧返券"))
        fd = self.book.fixed_discount("西渡")
        self.assertEqual(fd.data["amount"], 100)
        self.assertEqual(fd.data["no_stack"], ["换旧返券"])
        self.assertIsNone(self.book.fixed_discount("东桥"))

    def test_voucher_rules(self):
        ti = self.book.voucher("东桥", "换旧返券")
        self.assertEqual(ti.data["threshold"], 4000)
        self.assertEqual(ti.data["face"], 200)
        self.assertEqual(ti.data["deadline"], "12:00")
        st = self.book.voucher("东桥", "套装券")
        self.assertEqual(st.data["face"], 200)
        self.assertEqual(st.data["stack_condition"], "会员")

    def test_rumors_not_authorized(self):
        rumors = self.book.rumors()
        self.assertEqual({r.source for r in rumors}, {"P4", "P6"})
        self.assertTrue(all(not r.authorized for r in rumors))
        self.assertEqual(self.book.rumors("fixed_pricing_discount")[0].source, "P4")
        self.assertEqual(self.book.rumors("labor_waiver")[0].source, "P6")

    def test_display_and_meta(self):
        self.assertEqual([r.data["item"] for r in self.book.display_items()], ["K2"])
        for key in ("gram_basis_gold", "threshold_after_gram_discount",
                    "fixed_excluded_from_threshold", "display_no_lock",
                    "set_voucher_single", "no_implicit_stacking"):
            self.assertIsNotNone(self.book.meta_rule(key), key)


class PricingTest(unittest.TestCase):
    def setUp(self):
        _, self.items, _ = load_all()
        self.book = full_book()
        self.engine = QuoteEngine(self.book, self.items)

    def test_member_stacking_combo(self):
        q = self.engine.quote("Q", "顾客甲", "东桥", ["K1", "K2"], "10:06",
                              member=True, trade_in=True)
        # 二件套：按克优惠只落在最重的符合件 K2
        self.assertEqual(q.lines[0].gram_discount, 0)
        self.assertEqual(q.lines[1].gram_discount, 328)
        self.assertEqual(q.gold_material, 10034)
        self.assertTrue(q.threshold_ok)
        self.assertEqual({v.name: v.amount for v in q.vouchers},
                         {"换旧返券": 200, "套装券": 200})
        self.assertEqual(q.total, 10034)
        self.assertEqual(q.deadline, "12:00")
        self.assertTrue(any("待门店确认" in n for n in q.notes))  # K2 holdable=null

    def test_non_member_cannot_stack(self):
        q = self.engine.quote("Q", "某客", "东桥", ["K1", "K2"], "10:06",
                              member=False, trade_in=True)
        self.assertEqual([v.name for v in q.vouchers], ["换旧返券"])
        self.assertEqual(q.total, 10234)
        self.assertTrue(any("仅限会员" in d for d in q.declined))

    def test_single_piece_keeps_voucher(self):
        q = self.engine.quote("Q", "顾客甲", "东桥", ["K1"], "10:28",
                              member=True, trade_in=True,
                              price_basis={"K1": "10:10"})
        self.assertEqual(q.lines[0].gram_discount, 300)   # 旧价每克少 40
        self.assertTrue(any("旧价保留" in n for n in q.lines[0].notes))
        self.assertEqual(q.total, 4630)
        self.assertTrue(any("单件不成套装" in n for n in q.notes))

    def test_grandfather_only_for_confirmed_holdable(self):
        q = self.engine.quote("Q", "某客", "东桥", ["K1"], "10:35",
                              price_basis={"K1": "10:10"})
        self.assertEqual(q.lines[0].gram_discount, 300)   # 10:30 后仍按旧价
        q2 = self.engine.quote("Q", "某客", "东桥", ["K1"], "10:35")
        self.assertEqual(q2.lines[0].gram_discount, 150)  # 新确认按每克少 20

    def test_threshold_counts_gold_after_discount_only(self):
        # K4 单件：金料 2970−90=2880 未满 4000，换旧券不可用
        q = self.engine.quote("Q", "某客", "东桥", ["K4"], "10:35", trade_in=True)
        self.assertEqual(q.gold_material, 2880)
        self.assertFalse(q.threshold_ok)
        self.assertEqual(q.vouchers, [])
        self.assertEqual(q.total, 3000)
        # 定价件不计金料门槛：K3 单件金料计 0
        q = self.engine.quote("Q", "某客", "东桥", ["K3"], "10:35", trade_in=True)
        self.assertEqual(q.gold_material, 0)
        self.assertFalse(q.threshold_ok)

    def test_two_piece_discount_on_heaviest(self):
        q = self.engine.quote("Q", "顾客丁", "东桥", ["K1", "K4"], "11:30")
        self.assertEqual(q.lines[0].gram_discount, 150)   # K1 最重，落在 K1
        self.assertEqual(q.lines[1].gram_discount, 0)
        # 两件套享套装券 200（未提换旧，无换旧券）
        self.assertEqual([v.name for v in q.vouchers], ["套装券"])
        self.assertEqual(q.total, 7870)

    def test_same_name_activity_other_store(self):
        # 西渡「白鹭日」：定价款按件减 100，不用东桥规则
        q = self.engine.quote("Q", "某客", "西渡", ["K5"], "10:15", trade_in=True)
        self.assertEqual(q.lines[0].fixed_discount, 100)
        self.assertEqual(q.total, 4180)
        self.assertTrue(any("不是本店" in d for d in q.declined))
        self.assertTrue(any(src == "P3" for src, _ in q.applied))
        self.assertFalse(any(src in ("P1", "P2") for src, _ in q.applied))

    def test_unauthorized_claims_declined(self):
        q = self.engine.quote("Q", "顾客乙", "东桥", ["K1"], "10:16",
                              asked=["fixed_gram_discount"])
        self.assertTrue(any("转发" in d and "不能承诺" in d for d in q.declined))
        self.assertFalse(any(src in ("P4", "P6") for src, _ in q.applied))


class HoldTest(unittest.TestCase):
    def setUp(self):
        _, self.items, _ = load_all()
        self.reg = HoldRegistry(self.items)

    def test_no_double_hold(self):
        self.reg.confirm(customer="甲", store="东桥", item_ids=["K1"],
                         until="11:30", now="10:10", source="T")
        with self.assertRaises(HoldError):
            self.reg.confirm(customer="乙", store="东桥", item_ids=["K1"],
                             until="12:00", now="10:20", source="T")

    def test_unconfirmed_holdable_needs_manual_flag(self):
        with self.assertRaises(HoldError):
            self.reg.confirm(customer="甲", store="东桥", item_ids=["K2"],
                             until="11:30", now="10:10", source="T")
        h = self.reg.confirm(customer="甲", store="东桥", item_ids=["K2"],
                             until="11:30", now="10:10", source="T",
                             allow_unconfirmed=True)
        self.assertEqual(h.item_ids, ["K2"])

    def test_partial_release_and_expiry(self):
        h = self.reg.confirm(customer="甲", store="东桥", item_ids=["K1", "K3"],
                             until="11:30", now="10:10", source="T")
        self.reg.release_items(h, ["K3"], "10:28", "测试")
        self.assertEqual(h.item_ids, ["K1"])
        self.assertIsNotNone(self.reg.holder_of("K1", "10:35"))
        freed = self.reg.expire_due("11:30")
        self.assertEqual(freed[0][1], ["K1"])
        self.assertIsNone(self.reg.holder_of("K1", "11:31"))
        self.assertEqual(h.status, HoldStatus.EXPIRED)


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        policy, items, customers = load_all()
        cls.sim = Simulation(policy, items, customers).run()

    def quotes(self, customer):
        return [q for q in self.sim.quotes if q.customer == customer]

    def test_k2_display_override_releases_item_keeps_k1(self):
        self.assertFalse(self.sim.items["K2"].holdable)
        h1 = self.sim.holds.holds[0]
        self.assertEqual(h1.customer, "顾客甲")
        self.assertEqual(h1.item_ids, ["K1"])          # K2 必须释放，K1 继续保留
        self.assertEqual(h1.status, HoldStatus.EXPIRED)  # 11:30 到期未取
        self.assertTrue(any("释放 K2" in n for n in h1.notes))

    def test_confirmed_order_honored_at_old_price(self):
        requote = next(q for q in self.quotes("顾客甲")
                       if q.item_ids == ["K1"] and "守住" in q.purpose)
        self.assertEqual(requote.total, 4630)            # 旧价 40/克＋换旧券
        self.assertEqual(requote.lines[0].gram_discount, 300)

    def test_options_show_threshold_break(self):
        opts = {tuple(q.item_ids): q for q in self.sim.options["顾客甲"]}
        self.assertEqual(opts[("K1", "K3")].total, 8730)   # 会员可叠两券
        self.assertEqual(opts[("K1", "K4")].total, 7520)
        self.assertFalse(opts[("K4",)].threshold_ok)       # 换成 K4 单件破门槛
        self.assertFalse(opts[("K3",)].threshold_ok)       # 定价件不计金料门槛

    def test_waitlist_fills_after_expiry_no_double_hold(self):
        h2 = next(h for h in self.sim.holds.holds if h.customer == "顾客丁")
        self.assertEqual(h2.status, HoldStatus.ACTIVE)
        self.assertEqual(sorted(h2.item_ids), ["K1", "K4"])
        q = next(q for q in self.quotes("顾客丁") if q.qid == h2.quote_id)
        self.assertEqual(q.total, 7870)                  # 11:30 确认按新价 20/克＋套装券
        self.assertEqual(q.lines[0].gram_discount, 150)
        # 同一时刻 K1 只有一个持有人
        self.assertIs(self.sim.holds.holder_of("K1", "11:31"), h2)
        self.assertIs(self.sim.holds.holder_of("K1", "10:35"),
                      self.sim.holds.holds[0])

    def test_late_request_cannot_hold(self):
        q = next(q for q in self.quotes("顾客乙") if q.at == "11:31")
        self.assertEqual(q.total, 4980)                  # 现价每克少 20
        self.assertFalse(any(h.customer == "顾客乙" and h.status == HoldStatus.ACTIVE
                             for h in self.sim.holds.holds))
        w = next(w for w in self.sim.waiting
                 if w.customer == "顾客乙" and w.status == "pending")
        self.assertIn("K3", w.refuse)                    # 不接受改为 K3

    def test_rumors_never_applied(self):
        for q in self.sim.quotes:
            self.assertFalse(any(src in ("P4", "P6") for src, _ in q.applied), q.qid)

    def test_handover_pending_contacts(self):
        open_who = {c["customer"] for c in self.sim.contacts if c["status"] == "open"}
        self.assertIn("顾客甲", open_who)   # 到期未取，原组合不可恢复
        self.assertIn("顾客乙", open_who)   # K1 被留走，等候释放
        self.assertIn("顾客丁", open_who)   # 等候单补位成功待确认
        pending_notify = [w for w in self.sim.waiting
                          if w.kind == "notify" and w.status == "pending"]
        self.assertEqual([w.customer for w in pending_notify], ["顾客丙"])


if __name__ == "__main__":
    unittest.main()
