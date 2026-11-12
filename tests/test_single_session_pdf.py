"""Offline PDF contracts. Mock printing is not real-browser acceptance."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from travel_agent.single_session_pdf import PDFError, digest, pdf_receipt, render_pdf, verify_pdf
from travel_agent.single_session_api import APIError, SessionService
from test_single_session_api import OfflineAgent, fake_pdf




class PDFContracts(unittest.TestCase):
    def test_poppler_counts_images_not_masks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a.pdf'
            path.write_bytes(b'%PDF-test')
            with patch('travel_agent.single_session_pdf._poppler', side_effect=[
                    'page num type\n1 0 image\n1 1 smask\n2 2 image\n', 'Pages: 2\n']):
                self.assertEqual(verify_pdf(path, 2)['embedded_images'], 2)
            with patch('travel_agent.single_session_pdf._poppler', return_value='1 0 image\n'):
                with self.assertRaisesRegex(PDFError, 'mismatch'):
                    verify_pdf(path, 2)

    def test_failed_print_preserves_prior_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'a.html', Path(folder) / 'a.pdf'
            source.write_text('<img loading="lazy" src="data:image/png;base64,AA">')
            target.write_bytes(b'previous')
            with patch('travel_agent.single_session_pdf._print', side_effect=RuntimeError('decode failed')):
                with self.assertRaises(PDFError):
                    render_pdf(source, target)
            self.assertEqual(target.read_bytes(), b'previous')
            self.assertFalse(target.with_suffix('.pdf.json').exists())

    def test_print_expands_check_records(self):
        # A closed <details> stays closed in Chromium print even with author
        # @media print CSS forcing display:block (verified 2026-11-12); the
        # renderer must open the records itself before printing. Needs the
        # real browser: run under the python that has playwright installed.
        import shutil
        import subprocess
        try:
            import playwright  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('playwright is required for a real-browser print')
        if shutil.which('pdftotext') is None:
            self.skipTest('pdftotext is required to read back the printed PDF')
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'a.html', Path(folder) / 'a.pdf'
            source.write_text('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
                              '<p>Before.</p><details class="records"><summary>核查记录</summary>'
                              '<div class="rec">Record one must print.</div></details>'
                              '<p>After.</p></body></html>', encoding='utf-8')
            render_pdf(source, target)
            text = subprocess.run(['pdftotext', '-layout', str(target), '-'],
                                  check=True, capture_output=True, text=True, timeout=180).stdout
            self.assertIn('Record one must print.', text)

    def test_receipt_binds_both_files(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'a.html', Path(folder) / 'a.pdf'
            source.write_text('original')
            fake_pdf(source, target)
            self.assertEqual(pdf_receipt(source, target)['status'], 'ready')
            source.write_text('changed')
            with self.assertRaises(PDFError):
                pdf_receipt(source, target)

    def test_public_pdf_state_rechecks_files_and_restore_requires_verified_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            service = SessionService(folder, OfflineAgent, pdf_renderer=fake_pdf)
            try:
                trip_id = 'a' * 32
                directory = Path(folder) / trip_id
                directory.mkdir()
                agent = OfflineAgent(directory / 'session')
                current = agent.documents.save('# source')
                record = {'id': trip_id, 'read_capability': 'read', 'intake': {},
                          'status': 'running', 'created_at': 'now', 'updated_at': 'now',
                          'jobs': [{}], 'document': None}
                service._capture_existing_document(record)
                self.assertIsNone(record['document'])
                html = Path(current['html_path'])
                fake_pdf(html, html.with_suffix('.pdf'))
                service._capture_existing_document(record)
                self.assertEqual(record['document']['pdf_status'], 'ready')
                html.with_suffix('.pdf').write_bytes(b'tampered')
                public = service._public(record)
                self.assertEqual(public['document']['pdf_status'], 'unavailable')
                self.assertIsNone(public['pdf_url'])
            finally:
                service.close()

    def test_service_pdf_auth_version_and_revision_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            OfflineAgent.entered = OfflineAgent.release = None
            OfflineAgent.failure = False
            service = SessionService(folder, OfflineAgent, pdf_renderer=fake_pdf)
            try:
                result = service.create({'brief': 'PDF offline test'})
                trip_id, token = result['trip']['id'], result['token']
                def finished():
                    for _ in range(500):
                        trip = service.get(trip_id, token)['trip']
                        if trip['status'] != 'running':
                            return trip
                        time.sleep(.01)
                    self.fail('worker timeout')
                trip = finished()
                self.assertEqual(trip['status'], 'completed')
                self.assertIn('version=1', trip['pdf_url'])
                with self.assertRaises(APIError):
                    service.pdf(trip_id)
                read = service.records[trip_id]['read_capability']
                self.assertTrue(service.pdf(trip_id, read_capability=read).startswith(b'%PDF'))
                self.assertTrue(service.pdf(trip_id, token).startswith(b'%PDF'))
                with self.assertRaises(APIError):
                    service.pdf(trip_id, token, version=2)
                def fail(*args):
                    raise PDFError('image mismatch')
                service.pdf_renderer = fail
                service.revise(trip_id, token, {'feedback': 'change', 'expected_revision': 1})
                failed = finished()
                self.assertEqual(failed['status'], 'completed')
                self.assertIsNone(failed['error'])
                self.assertEqual(failed['document']['pdf_status'], 'unavailable')
                self.assertEqual(failed['revision'], 2)
                self.assertIn('修改后的行程'.encode(), service.handbook(trip_id, token))
                with self.assertRaises(APIError):
                    service.pdf(trip_id, token)
                self.assertEqual(service.pdf(trip_id, token, version=1), b'%PDF-unit-stub')
                error = json.loads((Path(folder) / trip_id / 'pdf-error.json').read_text())
                self.assertEqual(error['version'], 2)
                self.assertEqual(error['detail'], 'image mismatch')
            finally:
                service.close()
