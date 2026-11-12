"""Offline accounting and document-edit mechanics; no provider access."""

import json
import html
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from packages.travel_agent.single_session_document import DocumentStore, render_snapshot


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = DocumentStore(self.directory)
        self.source = self.store.register_source(
            "source-1", "https://example.test/admission",
            "Admission: THB 120.50 per adult. Open 09:00–17:00.",
            "2026-11-04T10:00:00+00:00",
        )

    def price(self, **overrides):
        args = dict(source_id="source-1", quote="THB 120.50 per adult",
                    claim="成人门票", kind="price", unit_amount="120.50",
                    currency="THB", quantity=3)
        args.update(overrides)
        return self.store.record_evidence(**args)

    def receipt(self):
        return dict(changes=["移除第二天下午一站"], judgment="步行安排更轻松",
                    alternatives=["可原地休息"], next_action="确认是否保留晚餐")

    def test_source_and_evidence_survive_restart_and_are_immutable(self):
        evidence = self.price()
        restarted = DocumentStore(self.directory)
        self.assertEqual(restarted.sources["source-1"], self.source)
        self.assertEqual(restarted.evidence["E1"], evidence)
        with self.assertRaises(ValueError):
            restarted.register_source("source-1", self.source["url"], "changed",
                                      self.source["retrieved_at"])

    def test_quote_requires_literal_match_and_known_source(self):
        for overrides in ({"quote": "THB 120.5 per adult"},
                          {"source_id": "unknown"}, {"quote": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.price(**overrides)
        self.assertEqual(self.store.evidence, {})

    def test_price_requires_amount_in_quote_and_positive_quantity(self):
        for overrides in ({"unit_amount": "20.50"}, {"unit_amount": "NaN"},
                          {"quantity": 0}, {"quantity": -1},
                          {"quantity": None}, {"currency": None},
                          {"unit_amount": None}, {"quantity": True}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.price(**overrides)

    def test_price_is_rendered_from_decimal_ledger_with_time_and_status(self):
        evidence = self.price()
        saved = self.store.save("# 三人行程\n门票：" + evidence["marker"])
        self.assertIn("120.5 泰铢/人 × 3人 = 361.5 泰铢", saved["rendered_markdown"])
        self.assertIn("只有一个来源", saved["rendered_markdown"])
        self.assertIn(evidence["verified_at"][:10], saved["rendered_markdown"])
        self.assertEqual(saved["price_checks"][0]["total_amount"], "361.5")
        self.assertEqual(saved["price_check_status"], "PASS")
        self.assertTrue(Path(saved["html_path"]).exists())
        self.assertEqual(DocumentStore(self.directory).current(), saved)

    def test_pure_render_uses_evidence_total_without_mutating_ledger(self):
        ev = self.price(quantity=1)
        ledger = self.store._read_json("ledger.json")
        before = deepcopy(ledger)
        snapshot = dict(markdown="# 行程\n票价 " + ev["marker"], receipt=self.receipt())
        output = render_snapshot(snapshot, ledger, {"travelers": 4})
        self.assertIn("120.5 泰铢/人 × 1人 = 120.5 泰铢", output["body_markdown"])
        self.assertEqual(ledger, before)
        self.assertEqual(output["price_checks"][0]["quantity"], "1")
        self.assertEqual(output["display_price_checks"][0]["quantity"], "1")
        self.assertIn('<sup><a href="#rec1"', output["html"])
        self.assertNotIn("〔", output["body_markdown"])
        self.assertNotIn("[证据", output["body_markdown"])

    def test_revision_headcount_and_car_basis(self):
        ev = self.price(quantity=1, claim="接机120.5泰铢/车")
        snapshot = dict(markdown="# 行程\n- **出行人**：4 人\n" + ev["marker"],
                        receipt=self.receipt())
        output = render_snapshot(snapshot, self.store._read_json("ledger.json"), {"travelers": 2})
        self.assertEqual(output["travelers"], "4")
        self.assertIn("120.5 泰铢/车 × 1车 = 120.5 泰铢", output["body_markdown"])
        snapshot["markdown"] = "# 行程（3人）\n" + ev["marker"]
        self.assertEqual(render_snapshot(snapshot, self.store._read_json("ledger.json"))["travelers"], "3")

    def test_all_quotes_collapsed_and_no_inferred_agreement(self):
        self.price()
        self.price()
        saved = self.store.save("未引用的证据也保留")
        page = Path(saved["html_path"]).read_text()
        self.assertEqual(page.count('class="rec"'), 2)
        self.assertEqual(page.count('class="quote"'), 2)
        self.assertIn('<details class="records"><summary>核查记录</summary>', page)
        self.assertNotIn(" open", page)
        self.assertEqual(page.count("只有一个来源"), 2)
        self.assertNotIn("多源一致", page)
        self.assertEqual(page.count(html.escape("THB 120.50 per adult")), 2)
        self.assertIn("example.test", page)

    def test_unreferenced_evidence_integrity_and_multiline_quote(self):
        quote = '第一行 <原文>\n第二行 & 保留'
        self.store.register_source("multiline", "https://example.test/multi", quote, "2026-11-05")
        self.store.record_evidence("multiline", quote, "说明", "fact")
        ledger = self.store._read_json("ledger.json")
        snapshot = dict(markdown="行程", receipt=self.receipt())
        rendered = render_snapshot(snapshot, ledger)
        self.assertIn(html.escape(quote), rendered["html"])
        ledger["evidence"]["E1"]["quote"] = "伪造原文"
        with self.assertRaisesRegex(ValueError, "integrity"):
            render_snapshot(snapshot, ledger)

    def test_explicit_conflict_preserves_only_its_cited_resolution(self):
        self.store.record_evidence("source-1", "Open 09:00–17:00", "开放时间与另一来源不一致", "hours")
        ledger = self.store._read_json("ledger.json")
        snapshot = dict(markdown="时段不一致，以景区当日公告为准 [证据 E1]", receipt=self.receipt())
        output = render_snapshot(snapshot, ledger)
        self.assertIn("两源不一致；以景区当日公告为准", output["html"])
        self.assertEqual(ledger["evidence"]["E1"]["verification_status"], "单源未交叉确认")
        snapshot["markdown"] = "以无关公告为准\n时段不一致 [证据 E1]"
        self.assertIn("两源不一致；采用依据待确认", render_snapshot(snapshot, ledger)["html"])
        self.assertNotIn("两源不一致；以无关公告为准", render_snapshot(snapshot, ledger)["html"])

    def test_save_loads_intake_read_only(self):
        # Put a document store under the same parent layout as a real session.
        store = DocumentStore(self.directory / "documents")
        (self.directory / "conversation.json").write_text('{"intake":{"travelers":4}}')
        store.register_source("s", "https://example.test", "CNY 60", "2026-11-05")
        ev = store.record_evidence("s", "CNY 60", "门票", "price", 60, "CNY", 1)
        saved = store.save(ev["marker"])
        self.assertIn("60 元/人 × 1人 = 60 元", saved["rendered_markdown"])
        self.assertEqual(store.evidence["E1"]["quantity"], "1")

    def test_decimal_arithmetic_does_not_use_float(self):
        self.store.register_source("small", "https://example.test/small",
                                   "USD 0.10 per person", "2026-11-04")
        ev = self.price(source_id="small", quote="USD 0.10 per person",
                        unit_amount="0.10", currency="USD")
        saved = self.store.save(ev["marker"])
        self.assertEqual(saved["price_checks"][0]["total_amount"], "0.3")

    def test_common_naked_prices_are_rejected_unknown_text_is_allowed(self):
        for price in ("THB 300", "300泰铢", "฿300", "$12.50", "USD15",
                      "120 CNY", "50元", "人民币50", "€90", "20 baht"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.store.save("门票：" + price)
        saved = self.store.save("# 行程\n价格未知，待核验；三人，2027-03-19 出发。")
        self.assertEqual(saved["price_checks"], [])

    def test_unknown_malformed_or_wrong_kind_marker_fails(self):
        fact = self.store.record_evidence("source-1", "Open 09:00–17:00",
                                          "开放时间", "opening_hours")
        for text in ("{{price:E99}}", "{{price:E1}}", "{{price:E1}",
                     "{{madeup:E1}}", "{{evidence:missing}}"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.store.save(text)
        saved = self.store.save("开放时间 " + fact["marker"])
        self.assertIn("Open 09:00–17:00", saved["rendered_markdown"])

    def test_currency_table_column_requires_a_bound_price_marker(self):
        table = "| 项目 | 价格（THB） | 人数 |\n| --- | --- | --- |\n| 门票 | VALUE | 3 |"
        with self.assertRaises(ValueError):
            self.store.save(table.replace("VALUE", "300"))
        evidence = self.price()
        saved = self.store.save(table.replace("VALUE", evidence["marker"]))
        self.assertEqual(saved["price_checks"][0]["evidence_id"], evidence["evidence_id"])
        self.assertIn("120.5 泰铢/人 × 3人 = 361.5 泰铢", saved["rendered_markdown"])

    def test_live_handbook_legacy_e3_reference_expands_registered_quote(self):
        # Recorded counterexample: live/document-0002.source.md cited E3, but
        # its rendered appendix only contained the four price marker records.
        self.price()
        self.price()
        quote = "Taking an electric tram, which is available from 6.00 am-6.00 pm."
        self.store.register_source(
            "live-tram", "https://www.tourismthailand.org/Attraction/wat-phra-that-doi-suthep",
            quote, "2026-11-04T08:52:26.299517+00:00")
        record = self.store.record_evidence("live-tram", quote, "电动缆车运行时间", "hours")
        self.assertEqual(record["evidence_id"], "E3")
        saved = self.store.save("运行 06:00–18:00 [证据 E3]")
        self.assertIn('id="rec3"', saved["rendered_markdown"])
        self.assertIn(quote, saved["rendered_markdown"])
        self.assertIn(quote, Path(saved["html_path"]).read_text(encoding="utf-8"))

    def test_legacy_reference_variants_deduplicate_and_mark_missing_ledger(self):
        record = self.store.record_evidence("source-1", "Open 09:00–17:00", "开放时间", "hours")
        saved = self.store.save("待确认 【证据 E1、E99】 [E1] " + record["marker"])
        self.assertEqual(saved["rendered_markdown"].count('id="rec1"'), 1)
        self.assertIn('id="rec99"', saved["rendered_markdown"])
        self.assertIn("来源记录缺失", saved["rendered_markdown"])
        self.assertNotIn("reference_checks", saved)

    def test_legacy_reference_does_not_bypass_existing_price_accounting(self):
        record = self.price()
        with self.assertRaises(ValueError):
            self.store.save("门票 THB 120.50 [证据 E1]")
        saved = self.store.save("门票 " + record["marker"] + " [证据 E1]")
        self.assertEqual(len(saved["price_checks"]), 1)
        self.assertEqual(saved["rendered_markdown"].count('id="rec1"'), 1)

    def test_edit_requires_base_hash_exact_unique_match_and_complete_receipt(self):
        first = self.store.save("# 行程\n上午散步\n下午爬山")
        for base, edits, receipt in (
            ("stale", [{"old": "下午爬山", "new": "下午休息"}], self.receipt()),
            (first["hash"], [{"old": "不存在", "new": "休息"}], self.receipt()),
            (first["hash"], [{"old": "", "new": "休息"}], self.receipt()),
            (first["hash"], [{"old": "下午爬山", "new": "下午休息"}], {}),
        ):
            with self.subTest(base=base, edits=edits), self.assertRaises(ValueError):
                self.store.edit(base, edits, receipt)
        self.assertEqual(self.store.current()["version"], 1)
        second = self.store.edit(first["hash"],
                                 [{"old": "下午爬山", "new": "下午休息"}],
                                 self.receipt())
        self.assertIn("-下午爬山", second["diff"])
        self.assertIn("+下午休息", second["diff"])
        self.assertIn("-下午爬山\n\\ No newline at end of file\n", second["diff"])
        self.assertEqual(second["receipt"]["judgment"], "步行安排更轻松")
        self.assertNotIn("## 修订回执", second["rendered_markdown"])
        self.assertTrue(second["receipt"])

    def test_duplicate_edit_target_fails_without_mutation(self):
        first = self.store.save("休息\n休息")
        with self.assertRaises(ValueError):
            self.store.edit(first["hash"], [{"old": "休息", "new": "散步"}],
                            self.receipt())
        self.assertEqual(self.store.current()["hash"], first["hash"])

    def test_edit_cannot_introduce_naked_prices_in_body_or_receipt(self):
        first = self.store.save("上午散步")
        with self.assertRaises(ValueError):
            self.store.edit(first["hash"], [{"old": "散步", "new": "门票 THB 300"}],
                            self.receipt())
        receipt = dict(self.receipt(), judgment="总价 THB 300")
        with self.assertRaises(ValueError):
            self.store.edit(first["hash"], [{"old": "散步", "new": "休息"}], receipt)
        self.assertEqual(self.store.current()["version"], 1)

    def test_undo_creates_new_version_and_preserves_audit_history(self):
        first = self.store.save("上午散步")
        second = self.store.edit(first["hash"], [{"old": "散步", "new": "休息"}],
                                 self.receipt())
        undone = DocumentStore(self.directory).undo(second["hash"])
        self.assertEqual(undone["version"], 3)
        self.assertEqual(undone["markdown"], first["markdown"])
        self.assertEqual(undone["hash"], first["hash"])
        self.assertIn("+上午散步", undone["diff"])
        self.assertTrue(Path(second["markdown_path"]).exists())
        with self.assertRaises(ValueError):
            self.store.undo(undone["hash"])

    def test_current_detects_document_or_snapshot_corruption(self):
        first = self.store.save("原文")
        path = Path(first["html_path"])
        path.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            DocumentStore(self.directory).current()

    def test_snapshot_metadata_hash_is_checked(self):
        self.store.save("原文")
        path = self.directory / "version-0001.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["receipt"]["judgment"] = "changed"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.current()

    def test_raw_html_and_unsafe_links_are_not_executable(self):
        saved = self.store.save(
            '# 手册\n<script>alert(1)</script>\n'
            '[危险](javascript:alert)\n[原文](https://example.test/info)\n'
            '| 时段 | 活动 |\n| --- | --- |\n| 上午 | 散步 |'
        )
        page = Path(saved["html_path"]).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn('href="javascript:', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn('<a href="https://example.test/info"', page)
        self.assertIn("<table>", page)
        self.assertIn('name="viewport"', page)


if __name__ == "__main__":
    unittest.main()
