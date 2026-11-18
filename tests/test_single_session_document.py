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

    def test_noop_save_edit_and_cancelling_edits_preserve_all_artifacts(self):
        saved = self.store.save('# Original plan')
        before = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        attempts = (
            lambda: self.store.save(saved['markdown']),
            lambda: self.store.edit(1, [{'old': 'Original', 'new': 'Original'}], self.receipt()),
            lambda: self.store.edit(1, [{'old': 'Original', 'new': 'Temporary'},
                                      {'old': 'Temporary', 'new': 'Original'}], self.receipt()),
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, 'document_unchanged'):
                attempt()
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()})
            self.assertEqual(saved['hash'], self.store.current()['hash'])

    def test_noop_member_does_not_block_a_batch_with_a_real_edit(self):
        self.store.save('# Original plan')
        result = self.store.edit(1, [{'old': 'plan', 'new': 'plan'},
                                    {'old': 'Original', 'new': 'Updated'}], self.receipt())
        self.assertEqual(2, result['version'])
        self.assertIn('Updated', result['markdown'])

    def test_source_and_evidence_survive_restart_and_are_immutable(self):
        evidence = self.price()
        restarted = DocumentStore(self.directory)
        self.assertEqual(restarted.sources["source-1"], self.source)
        self.assertEqual(restarted.evidence["E1"], evidence)
        with self.assertRaises(ValueError):
            restarted.register_source("source-1", self.source["url"], "changed",
                                      self.source["retrieved_at"])

    def test_marker_type_errors_identify_exact_evidence_and_safe_marker(self):
        price = self.price()
        fact = self.store.record_evidence(source_id='source-1', quote='Open 09:00–17:00.',
                                          claim='Opening hours', kind='fact')
        before = (self.directory / 'ledger.json').read_bytes()
        cases = ((f"{{{{price:{fact['evidence_id']}}}}}", 'price marker requires price evidence',
                  fact['evidence_id'], f"{{{{evidence:{fact['evidence_id']}}}}}"),
                 (f"{{{{evidence:{price['evidence_id']}}}}}", 'price evidence requires a price marker',
                  price['evidence_id'], f"{{{{price:{price['evidence_id']}}}}}"))
        for text, reason, eid, replacement in cases:
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                self.store.save('# Plan\n' + text)
            self.assertIn(reason, str(caught.exception))
            self.assertIn(eid, str(caught.exception))
            self.assertIn(replacement, str(caught.exception))
            self.assertIsNone(self.store.current())
            self.assertEqual(before, (self.directory / 'ledger.json').read_bytes())

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

    def test_multilingual_explicit_free_prices_keep_verbatim_scope(self):
        quotes = ('山塘街的門票價格\n免費入場', '公园门票免费', 'Museum: free admission',
                  'Gallery admission is free', 'Musée : entrée gratuite', 'Museo: entrada gratuita',
                  'Museum: Eintritt frei', '美術館：入場無料', '박물관 무료 입장', 'พิพิธภัณฑ์เข้าชมฟรี',
                  'Museum: free admission; booking is not required')
        for index, quote in enumerate(quotes):
            source = f'free-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-16')
            with self.subTest(quote=quote):
                record = self.price(source_id=source, quote=quote, claim=quote,
                                    unit_amount=0, quantity=2, currency='CNY')
                self.assertEqual(record['unit_amount'], '0')
                self.assertEqual(record['total_amount'], '0')
                self.assertIsNone(record['matched_quote_number'])
                self.assertEqual(record['free_price_scope'], quote)
                self.assertEqual(record['price_basis'], 'explicit_free_quote')

    def test_free_children_or_date_condition_cannot_be_rewritten_unrestricted(self):
        for index, quote in enumerate(('Museum: children under six enter free',
                                       'Museum: free admission on Mondays for members only',
                                       '博物館：週三兒童免費入場')):
            source = f'conditional-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-16')
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, 'complete quote'):
                self.price(source_id=source, quote=quote, claim='All adults have free admission',
                           unit_amount=0, currency='CNY')
        self.assertEqual(self.store.evidence, {})

    def test_annual_and_line_wrapped_admission_preserve_source_scope(self):
        # First quote is the observed rejection, copied verbatim from source S3.
        quotes = ('西湖环湖景区：可步游白堤、苏堤、孤山等，全年免费。',
                  '公园全年免费。', '博物館全年免費。', '公园门票\n免费',
                  '公园免费\n入场', 'Museum: free\nadmission',
                  'Museum: admission\nis free', '美術館：入場\n無料',
                  '博物館：入館\n無料。会員のみ')
        for index, quote in enumerate(quotes):
            source = f'wrapped-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-17')
            with self.subTest(quote=quote):
                record = self.price(source_id=source, quote=quote, claim=quote,
                                    unit_amount='0', currency='CNY', quantity=3)
                self.assertEqual(record['free_price_scope'], quote)
                self.assertIn(record['matched_free_expression'], quote)
                self.assertEqual(record['total_amount'], '0')

    def test_free_services_gifts_and_wrapped_negations_are_not_zero_admission(self):
        quotes = ('中国丝绸博物馆、中国茶叶博物馆：免费了解传统工艺与茶文化。',
                  '公园免费导览', '公园全年免费导览', '导览服务全年免费。',
                  '博物馆：礼品全年免费。', '博物馆赠品免费', '博物馆免费赠送纪念品',
                  '公园不再免费\n入场', '公园不再\n免费入场', '公园免费\n入场\n已取消',
                  'Museum: no free\nadmission',
                  'Museum: admission\nis free is not available', '美術館：入場\n無料ではありません',
                  '美術館：入場\n無料\nではありません',
                  '公园全年免费已取消。')
        for index, quote in enumerate(quotes):
            source = f'non-admission-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-17')
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, 'zero price requires'):
                self.price(source_id=source, quote=quote, claim=quote, unit_amount='0')
        self.assertEqual(self.store.evidence, {})

    def test_scoped_free_condition_is_visible_next_to_price_in_body(self):
        quote = 'Museum: children under six enter free on Mondays'
        self.store.register_source('kids', 'https://example.test/kids', quote, '2026-11-16')
        record = self.price(source_id='kids', quote=quote, claim=quote, unit_amount=0, quantity=1)
        output = render_snapshot(dict(markdown='Museum ' + record['marker'], receipt=self.receipt()),
                                 self.store._read_json('ledger.json'))
        self.assertIn('0 泰铢/人 × 1人 = 0 泰铢', output['body_markdown'])
        self.assertIn('免费适用原文：' + quote, output['body_markdown'])
        self.assertEqual(output['price_checks'][0]['free_price_scope'], quote)

    def test_whole_quote_conflicts_cannot_be_zero_price_witnesses(self):
        # The first three are frozen independent-review counterexamples. Mixed
        # scopes are intentionally conservative; keep them as other evidence.
        quotes = (
            '公园全年免费，现已取消。',
            '博物馆全年免费。\n但该政策已取消，恢复收费。',
            '博物馆全年免费。仅指讲解服务，参观门票须另购。',
            '公园全年免费，\n入场政策已经取消。',
            '公园全年免费，但讲解收费。',
            '儿童免费入场，成人收费。',
            '此前收费，现免费入场。',
            '取消收费，改为免费入场。',
            '公园全年免费。仅限赠品。',
            'Museum: free admission.\nThis policy has been withdrawn.',
            'Museum: admission is free. This refers only to guided tours.',
            '美術館：入場無料。\nこの制度は廃止されました。',
            '美術館：入場無料。対象はガイドサービスのみ。',
            '博物馆门票0元。现已取消。',
            '博物馆门票CNY 0。仅指讲解服务。',
            '儿童门票0元，成人20元。',
            'Museum: free admission; adults pay USD 20.',
            'Museum admission USD 0. This policy has been withdrawn.',
            'Museum admission USD 0. Adults pay USD 20.',
            'Museum admission USD 0 is no longer valid.',
            'Museum admission is not USD 0.',
            '美術館：入場料金JPY 0。ガイドサービスのみ。',
        )
        for index, quote in enumerate(quotes):
            source = f'conflicting-zero-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-17')
            before = (self.directory / 'ledger.json').read_bytes()
            with self.subTest(quote=quote):
                with self.assertRaisesRegex(ValueError, 'zero price'):
                    self.price(source_id=source, quote=quote, claim=quote, unit_amount='0')
                self.assertEqual((self.directory / 'ledger.json').read_bytes(), before)
                record = self.store.record_evidence(source, quote, quote, 'other')
                self.assertEqual(record['quote'], quote)
                self.assertNotIn('unit_amount', record)
        saved = self.store.save('# Scope preserved\n' + record['marker'])
        self.assertIn(record['quote'], saved['rendered_markdown'])

    def test_plain_free_and_zero_prices_still_preserve_complete_conditions(self):
        quotes = ('公园全年免费。', '免费参观，包括免费讲解。',
                  'Museum: free admission; booking is not required',
                  'Museum admission USD 0 on Mondays only.',
                  '博物館：入場\n無料。会員のみ')
        for index, quote in enumerate(quotes):
            source = f'plain-zero-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-17')
            with self.subTest(quote=quote):
                record = self.price(source_id=source, quote=quote, claim=quote, unit_amount='0')
                self.assertEqual(record['free_price_scope'], quote)
                self.assertEqual(record['claim'], quote)

    def test_free_binding_requires_source_quote_and_preserves_decimal_rules(self):
        quote = 'Museum: free entry'
        self.store.register_source('free', 'https://example.test/free', quote, '2026-11-16')
        for overrides in (dict(quote='Museum free entry'), dict(unit_amount=5), dict(quantity=0)):
            args = dict(source_id='free', quote=quote, claim=quote, unit_amount=0)
            args.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.price(**args)
        self.assertEqual(self.store.evidence, {})

    def test_unknown_or_negated_free_does_not_infer_zero(self):
        for index, quote in enumerate(('Museum admission price unknown', 'Museum: not free admission',
                                       'Museum: no free admission', 'Museum: free cancellation',
                                       'Museum: free admission unavailable', '公园不再免费入场')):
            source = f'not-free-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-16')
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.price(source_id=source, quote=quote, claim=quote, unit_amount=0)
        self.assertEqual(self.store.evidence, {})

    def test_free_scope_tampering_is_rejected_after_reload(self):
        quote = 'Museum: free admission on Mondays'
        self.store.register_source('free', 'https://example.test/free', quote, '2026-11-16')
        record = self.price(source_id='free', quote=quote, claim=quote, unit_amount=0)
        ledger = self.store._read_json('ledger.json')
        ledger['evidence'][record['evidence_id']]['free_price_scope'] = 'All days free'
        with self.assertRaisesRegex(ValueError, 'accounting mismatch'):
            render_snapshot(dict(markdown=record['marker'], receipt=self.receipt()), ledger)

    def test_zero_age_and_time_numerals_do_not_bypass_free_scope(self):
        quote = 'Museum: children aged 0–6 only have free admission, 09:00–11:00 on Mondays.'
        self.store.register_source('kids', 'https://example.test/kids', quote, '2026-11-16')
        record = self.price(source_id='kids', quote=quote, claim=quote, unit_amount=0, quantity=1)
        self.assertEqual(record['free_price_scope'], quote)
        self.assertEqual(record['price_basis'], 'explicit_free_quote')
        self.assertIsNone(record['matched_quote_number'])
        output = render_snapshot(dict(markdown='Museum ' + record['marker'], receipt=self.receipt()),
                                 self.store._read_json('ledger.json'))
        self.assertIn(quote, output['body_markdown'])

    def test_zero_age_and_time_numerals_do_not_authorize_unrestricted_claim(self):
        quote = 'Museum: children aged 0–6 only have free admission, 09:00–11:00 on Mondays.'
        self.store.register_source('kids', 'https://example.test/kids', quote, '2026-11-16')
        with self.assertRaisesRegex(ValueError, 'complete quote'):
            self.price(source_id='kids', quote=quote, claim='All adults admitted free every day',
                       unit_amount=0, quantity=2)
        self.assertEqual(self.store.evidence, {})

    def test_zero_requires_free_expression_or_actual_zero_monetary_quote(self):
        for index, quote in enumerate(('Museum: children aged 0–6; price unknown.',
                                       'Museum hours 09:00–11:00 on Mondays.', 'Museum: CNY 0.50.')):
            source = f'unknown-zero-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-16')
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, 'zero monetary'):
                self.price(source_id=source, quote=quote, claim=quote, unit_amount=0)
        self.assertEqual(self.store.evidence, {})

    def test_explicit_numeric_zero_keeps_its_conditions(self):
        quote = 'Museum: children aged 0–6 pay CNY 0.00 on Mondays.'
        self.store.register_source('zero', 'https://example.test/zero', quote, '2026-11-16')
        record = self.price(source_id='zero', quote=quote, claim=quote, unit_amount=0)
        self.assertEqual(record['matched_quote_number'], '0.00')
        self.assertEqual(record['price_basis'], 'explicit_zero_price_quote')
        self.assertEqual(record['free_price_scope'], quote)
        with self.assertRaisesRegex(ValueError, 'complete quote'):
            self.price(source_id='zero', quote=quote, claim='All adults admitted free every day', unit_amount=0)

    def test_sentence_period_does_not_change_whole_decimal_matching(self):
        for index, (quote, allowed) in enumerate((('Adults CNY 70.', True),
                                                ('Adults CNY 70.00.', True),
                                                ('Adults CNY 700.', False),
                                                ('Adults CNY 70.50.', False))):
            source = f'sentence-price-{index}'
            self.store.register_source(source, f'https://example.test/{source}', quote, '2026-11-16')
            with self.subTest(quote=quote):
                if allowed:
                    record = self.price(source_id=source, quote=quote, claim='Adult admission', unit_amount=70)
                    self.assertEqual(record['unit_amount'], '70')
                else:
                    with self.assertRaisesRegex(ValueError, 'whole decimal'):
                        self.price(source_id=source, quote=quote, claim='Adult admission', unit_amount=70)

    def test_legacy_numeric_zero_replays_without_ledger_mutation(self):
        quote = 'Admission CNY 0 per person on Mondays only'
        source = self.store.register_source('S1', 'https://example.test/free', quote, '2026-11-01')
        legacy = dict(evidence_id='E1', source_id='S1', quote=quote, claim='Monday admission',
                      kind='price', url=source['url'], retrieved_at=source['retrieved_at'],
                      verified_at='2026-11-16T03:09:38.495992+00:00',
                      verification_status='单源未交叉确认', source_content_hash=source['content_hash'],
                      unit_amount='0', quantity='1', currency='CNY', total_amount='0',
                      matched_quote_number='0', marker='{{price:E1}}')
        ledger = self.store._read_json('ledger.json')
        ledger['evidence']['E1'] = legacy
        self.store._write_json('ledger.json', ledger)
        original = (self.directory / 'ledger.json').read_bytes()
        saved = self.store.save('# Monday trip\nAdmission {{price:E1}}')
        reopened = DocumentStore(self.directory)
        rendered = render_snapshot(reopened.current(), reopened._read_json('ledger.json'))
        self.assertIn('0 元/人 × 1人 = 0 元', rendered['body_markdown'])
        self.assertIn('免费适用原文：' + quote, rendered['body_markdown'])
        self.assertEqual(saved['price_checks'][0]['matched_quote_number'], '0')
        self.assertEqual(reopened.evidence['E1'], legacy)
        self.assertEqual((self.directory / 'ledger.json').read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'complete quote'):
            reopened.record_evidence('S1', quote, 'Monday admission', 'price', 0, 'CNY', 1)
        self.assertEqual((self.directory / 'ledger.json').read_bytes(), original)

    def test_legacy_numeric_zero_cannot_skip_source_or_number_validation(self):
        quote = 'Admission CNY 0 per person on Mondays only'
        self.store.register_source('free', 'https://example.test/free', quote, '2026-11-01')
        record = self.price(source_id='free', quote=quote, claim=quote, unit_amount=0)
        ledger = self.store._read_json('ledger.json')
        legacy = ledger['evidence'][record['evidence_id']]
        for key in ('price_basis', 'free_price_scope', 'matched_zero_price_expression'):
            legacy.pop(key, None)
        legacy['claim'] = 'Monday admission'
        legacy['matched_quote_number'] = '9'
        with self.assertRaisesRegex(ValueError, 'accounting mismatch'):
            render_snapshot(dict(markdown=record['marker'], receipt=self.receipt()), ledger)
        legacy['matched_quote_number'] = '0'
        legacy['quote'] = 'Admission CNY 0 every day'
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            render_snapshot(dict(markdown=record['marker'], receipt=self.receipt()), ledger)

    def _historical_scope_ledger(self):
        # Actual records produced offline by the pre-scope-policy implementation;
        # quotes, witness fields, status and timestamps are frozen, not re-admitted.
        rows = (
            ('historical-free', '公园免费入场，但讲解收费。',
             '2026-11-17T05:28:38.062525+00:00', None, 'explicit_free_quote',
             'matched_free_expression', '免费入场'),
            ('historical-numeric', '儿童门票CNY 0，成人门票CNY 20。',
             '2026-11-17T05:28:38.063640+00:00', '0', 'explicit_zero_price_quote',
             'matched_zero_price_expression', 'CNY 0'),
        )
        records = {}
        for index, (sid, quote, verified, number, basis, field, expression) in enumerate(rows, 1):
            source = self.store.register_source(sid, f'https://example.test/{sid}', quote, '2026-11-16')
            eid = f'E{index}'
            records[eid] = dict(evidence_id=eid, source_id=sid, quote=quote, claim=quote,
                kind='price', url=source['url'], retrieved_at=source['retrieved_at'],
                verified_at=verified, verification_status='单源未交叉确认',
                source_content_hash=source['content_hash'], unit_amount='0', quantity='2',
                currency='CNY', total_amount='0', matched_quote_number=number,
                price_basis=basis, free_price_scope=quote, marker='{{price:' + eid + '}}',
                **{field: expression})
        ledger = self.store._read_json('ledger.json')
        ledger['evidence'] = records
        ledger['next_evidence_number'] = 3
        self.store._write_json('ledger.json', ledger)
        return ledger

    def test_historical_zero_scope_replays_without_readmission_or_mutation(self):
        ledger = self._historical_scope_ledger()
        original = deepcopy(ledger)
        disk = (self.directory / 'ledger.json').read_bytes()
        reopened = DocumentStore(self.directory)
        for markdown in ('# Unrelated plan', '# Plan\n{{price:E1}}\n{{price:E2}}'):
            with self.subTest(markdown=markdown):
                output = render_snapshot(dict(markdown=markdown, receipt=self.receipt()), ledger)
                self.assertEqual(ledger, original)
                if '{{price:' in markdown:
                    self.assertEqual(len(output['price_checks']), 2)
                    for record in ledger['evidence'].values():
                        self.assertIn(record['free_price_scope'], output['body_markdown'])
                    self.assertIn('只有一个来源', output['rendered_markdown'])
                else:
                    self.assertEqual(output['price_checks'], [])
        for record in ledger['evidence'].values():
            with self.assertRaisesRegex(ValueError, 'zero price scope unclear'):
                reopened.record_evidence(record['source_id'], record['quote'], record['claim'],
                                         'price', '0', 'CNY', 2)
        self.assertEqual(reopened.evidence, original['evidence'])
        self.assertEqual((self.directory / 'ledger.json').read_bytes(), disk)

    def test_historical_zero_scope_replay_keeps_integrity_and_witness_checks(self):
        ledger = self._historical_scope_ledger()
        changes = (
            ('sources', 'historical-free', 'content', 'changed source'),
            ('evidence', 'E1', 'source_content_hash', 'wrong hash'),
            ('evidence', 'E1', 'quote', '公园无条件免费。'),
            ('evidence', 'E1', 'claim', '公园无条件免费。'),
            ('evidence', 'E1', 'free_price_scope', '公园无条件免费。'),
            ('evidence', 'E1', 'matched_free_expression', 'free admission'),
            ('evidence', 'E1', 'price_basis', 'explicit_zero_price_quote'),
            ('evidence', 'E1', 'matched_quote_number', '0'),
            ('evidence', 'E1', 'total_amount', '2'),
            ('evidence', 'E1', 'quantity', '0'),
            ('evidence', 'E2', 'matched_zero_price_expression', 'CNY 20'),
            ('evidence', 'E2', 'matched_quote_number', '20'),
            ('evidence', 'E2', 'unit_amount', '20'),
        )
        for section, key, field, value in changes:
            for markdown in ('# Unrelated plan', '# Plan\n{{price:E1}}\n{{price:E2}}'):
                tampered = deepcopy(ledger)
                tampered[section][key][field] = value
                with self.subTest(field=field, markdown=markdown), self.assertRaises(ValueError):
                    render_snapshot(dict(markdown=markdown, receipt=self.receipt()), tampered)

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

    def test_edit_requires_integer_version_and_exact_unique_match(self):
        first = self.store.save("# 行程\n上午散步\n下午爬山")
        for base, edits, receipt in (
            ("stale", [{"old": "下午爬山", "new": "下午休息"}], self.receipt()),
            (first["version"], [{"old": "不存在", "new": "休息"}], self.receipt()),
            (first["version"], [{"old": "", "new": "休息"}], self.receipt()),
        ):
            with self.subTest(base=base, edits=edits), self.assertRaises(ValueError):
                self.store.edit(base, edits, receipt)
        self.assertEqual(self.store.current()["version"], 1)
        second = self.store.edit(first["version"],
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
            self.store.edit(first["version"], [{"old": "休息", "new": "散步"}],
                            self.receipt())
        self.assertEqual(self.store.current()["hash"], first["hash"])

    def test_edit_cannot_introduce_naked_prices_in_body_or_receipt(self):
        first = self.store.save("上午散步")
        with self.assertRaises(ValueError):
            self.store.edit(first["version"], [{"old": "散步", "new": "门票 THB 300"}],
                            self.receipt())
        receipt = dict(self.receipt(), judgment="总价 THB 300")
        with self.assertRaises(ValueError):
            self.store.edit(first["version"], [{"old": "散步", "new": "休息"}], receipt)
        self.assertEqual(self.store.current()["version"], 1)

    def test_undo_creates_new_version_and_preserves_audit_history(self):
        first = self.store.save("上午散步")
        second = self.store.edit(first["version"], [{"old": "散步", "new": "休息"}],
                                 self.receipt())
        undone = DocumentStore(self.directory).undo(second["version"])
        self.assertEqual(undone["version"], 3)
        self.assertEqual(undone["markdown"], first["markdown"])
        self.assertEqual(undone["hash"], first["hash"])
        self.assertIn("+上午散步", undone["diff"])
        self.assertTrue(Path(second["markdown_path"]).exists())
        with self.assertRaises(ValueError):
            self.store.undo(undone["version"])

    def test_current_detects_document_or_snapshot_corruption(self):
        first = self.store.save("原文")
        path = Path(first["html_path"])
        path.write_text("tampered", encoding="utf-8")
        recovered = DocumentStore(self.directory).current()
        self.assertEqual(recovered['version'], 1)
        self.assertEqual(recovered['hash_observations'][0]['field'], 'html_path')
        self.assertEqual(self.store.edit(1, [dict(old='原文', new='改文')], {})['version'], 2)

    def test_snapshot_metadata_hash_is_checked(self):
        self.store.save("原文")
        path = self.directory / "version-0001.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["receipt"]["judgment"] = "changed"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        recovered = self.store.current()
        self.assertEqual(recovered['receipt']['judgment'], 'changed')
        self.assertEqual(recovered['hash_observations'][0]['field'], 'snapshot_hash')

    def test_markdown_hash_difference_does_not_block_read_or_edit(self):
        self.store.save('原文')
        path = self.directory / 'version-0001.json'
        snapshot = json.loads(path.read_text())
        snapshot['markdown'] = '改过的原文'
        path.write_text(json.dumps(snapshot))
        recovered = self.store.current()
        self.assertIn('hash', [row['field'] for row in recovered['hash_observations']])
        self.assertEqual(self.store.edit(1, [dict(old='改过的原文', new='新文')], {})['version'], 2)

    def test_same_content_new_revision_rejects_old_integer_version(self):
        first = self.store.save('原文')
        self.store.edit(1, [dict(old='原文', new='临时改文')], {})
        second = self.store.undo(2)
        self.assertEqual(first['hash'], second['hash'])
        for stale in (1, 2, True, '3', first['hash']):
            with self.subTest(stale=stale), self.assertRaisesRegex(ValueError, 'base_version'):
                self.store.edit(stale, [dict(old='原文', new='改文')], {})
        self.assertEqual(self.store.edit(3, [dict(old='原文', new='改文')], {})['version'], 4)

    def test_source_date_is_recorded_without_format_gate(self):
        source = self.store.register_source('source', 'https://example.test', '原文', '今天上午')
        self.assertEqual(source['retrieved_at'], '今天上午')

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
