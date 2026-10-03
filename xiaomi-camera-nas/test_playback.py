import json
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import playback as p
import server


class QuietTests(unittest.TestCase):
    def setUp(self):
        self.q = p.QuietWindow(300)
        self.sig = [1024, 0, 0, 123, 1, p.RECIPE]

    def test_default_waits_five_minutes(self):
        q = p.QuietWindow()
        self.assertEqual(q.seconds, 300)
        self.assertFalse(q.ready('a', self.sig, now=0, wall=10000))
        self.assertFalse(q.ready('a', self.sig, now=299, wall=10299))
        self.assertTrue(q.ready('a', self.sig, now=300, wall=10300))

    def test_requires_full_observed_window_even_for_old_files(self):
        self.assertFalse(self.q.ready('a', self.sig, now=0, wall=1000))
        self.assertFalse(self.q.ready('a', self.sig, now=299, wall=1299))
        self.assertTrue(self.q.ready('a', self.sig, now=300, wall=1300))
        self.assertFalse(p.QuietWindow(300).ready('a', self.sig, now=301, wall=1301))

    def test_resets_for_growth_in_place_writes_and_replacement(self):
        for field in range(5):
            with self.subTest(field=field):
                q = p.QuietWindow(300)
                q.ready('a', self.sig, now=0, wall=1000)
                changed = self.sig.copy()
                changed[field] += 1
                self.assertFalse(q.ready('a', changed, now=299, wall=1299))
                self.assertFalse(q.ready('a', changed, now=598, wall=1598))
                self.assertTrue(q.ready('a', changed, now=599, wall=1599))

    def test_recent_writes_and_empty_files_are_not_ready(self):
        recent = self.sig.copy()
        recent[1] = 1200 * 10**9
        self.q.ready('a', recent, now=0, wall=1000)
        self.assertFalse(self.q.ready('a', recent, now=300, wall=1300))
        empty = self.sig.copy()
        empty[0] = 0
        self.q.ready('b', empty, now=0, wall=1000)
        self.assertFalse(self.q.ready('b', empty, now=1000, wall=2000))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cache = self.root / '.cache'
        self.cache.mkdir()
        self.root_patch = patch.object(p, 'ROOT', self.root)
        self.cache_patch = patch.object(p, 'CACHE', self.cache)
        self.root_patch.start()
        self.cache_patch.start()
        self.source = self.root / 'recording.mp4'
        self.source.write_bytes(b'a' * 4096)
        self.before = p.signature(self.source)
        p.REJECTED.clear()
        p.STATES.clear()
        p.drain_invalidations(p.QuietWindow())

    def tearDown(self):
        self.root_patch.stop()
        self.cache_patch.stop()
        self.tmp.cleanup()

    def fake_encode(self, command, *args, **kwargs):
        if command[-1] != '-':
            snapshot = Path(command[command.index('-i') + 1])
            self.assertNotEqual(snapshot, self.source)
            self.assertEqual(snapshot.read_bytes(), b'a' * 4096)
            Path(command[-1]).write_bytes(b'compressed' * 8)

    def publish(self):
        with patch.object(p, 'duration', return_value=10), patch.object(p, 'run_checked', side_effect=self.fake_encode):
            p.prepare(self.source, p.signature(self.source))
        return p.prepared(self.source)

    def request(self, path, headers=None, method='GET', fail_body=False):
        class Socket:
            def __init__(self, payload):
                self.input = io.BytesIO(payload)
                self.output = io.BytesIO()
            def makefile(self, *args, **kwargs):
                return self.input
            def sendall(self, data):
                if fail_body and self.output.tell():
                    raise BrokenPipeError('simulated disconnect')
                self.output.write(data)
        lines = ['{} {} HTTP/1.0'.format(method, path), 'Host: localhost']
        lines += ['{}: {}'.format(k, v) for k, v in (headers or {}).items()]
        sock = Socket(('\r\n'.join(lines) + '\r\n\r\n').encode())
        with patch.object(server, 'ROOT', self.root), patch.object(server.Handler, 'log_message'):
            server.Handler(sock, ('127.0.0.1', 0), SimpleNamespace())
        header, body = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = header.decode().split('\r\n')
        return int(lines[0].split()[1]), dict(line.split(': ', 1) for line in lines[1:]), body

    def test_publish_smaller_copy_and_keep_original(self):
        old, manifest = p.cache_paths(self.source)
        old.write_bytes(b'legacy copy')
        manifest.write_text(json.dumps([4096, self.before[1], 1]))
        self.assertIsNone(p.prepared(self.source))
        with patch.object(p, 'duration', return_value=10), patch.object(p, 'run_checked', side_effect=self.fake_encode):
            p.prepare(self.source, self.before)
        output = p.prepared(self.source)
        self.assertIsNotNone(output)
        self.assertLess(output.stat().st_size, self.source.stat().st_size)
        self.assertEqual(self.source.read_bytes(), b'a' * 4096)
        self.assertFalse(old.exists())
        self.assertFalse(list(self.cache.glob('*.part.*')))
        self.assertFalse(list(self.cache.glob('*.snapshot.mp4')))
        record = json.loads(manifest.read_text())
        self.assertEqual(record['source_md5'], hashlib.md5(self.source.read_bytes()).hexdigest())
        # Same length, preserved mtime: ctime still invalidates the result.
        self.source.write_bytes(b'b' * 4096)
        os.utime(self.source, ns=(self.before[1], self.before[1]))
        self.assertIsNone(p.prepared(self.source))

    def test_copy_with_unobserved_content_change_is_rejected(self):
        real_digest = p.source_md5
        def modify_after_copy(source, before, destination=None):
            value = real_digest(source, before, destination)
            if destination is not None:
                source.write_bytes(b'b' * 4096)
            return value
        with patch.object(p, 'stat_signature', return_value=self.before), patch.object(p, 'source_md5', side_effect=modify_after_copy):
            with self.assertRaises(p.SourceChanged):
                p.prepare(self.source, self.before)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_content_change_after_encoding_is_rejected_by_md5(self):
        def modify_after_encode(command, *args, **kwargs):
            self.fake_encode(command)
            if command[-1] != '-':
                self.source.write_bytes(b'b' * 4096)
        with patch.object(p, 'stat_signature', return_value=self.before), patch.object(p, 'duration', return_value=10), patch.object(p, 'run_checked', side_effect=modify_after_encode):
            with self.assertRaises(p.SourceChanged):
                p.prepare(self.source, self.before)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_read_detects_write_during_copy(self):
        class Destination:
            def write(inner, block):
                self.source.write_bytes(b'b' * 4096)
        with self.assertRaises(p.SourceChanged):
            p.source_md5(self.source, self.before, Destination())

    def test_md5_mismatch_restarts_full_quiet_window_once(self):
        self.publish()
        q = p.QuietWindow()
        q.ready(self.source, self.before, now=0, wall=1e12)
        self.source.write_bytes(b'b' * 4096)
        with patch.object(p, 'stat_signature', return_value=self.before):
            self.assertIsNone(p.prepared(self.source, verify_content=True))
            p.drain_invalidations(q)
            self.assertFalse(q.ready(self.source, self.before, now=600, wall=1e12))
            self.assertIsNone(p.prepared(self.source, verify_content=True))
            p.drain_invalidations(q)
            self.assertFalse(q.ready(self.source, self.before, now=899, wall=1e12))
            self.assertTrue(q.ready(self.source, self.before, now=900, wall=1e12))

    def test_download_checks_md5_and_sends_complete_attachment(self):
        target = self.publish()
        status, headers, body = self.request('/recording.mp4', {'Range': 'bytes=3-12'})
        self.assertEqual(status, 200)
        self.assertEqual(body, target.read_bytes())
        self.assertEqual(headers['Accept-Ranges'], 'none')
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertIn('recording.720p.mp4', headers['Content-Disposition'])
        self.source.write_bytes(b'b' * 4096)
        with patch.object(p, 'stat_signature', return_value=self.before):
            status, headers, _ = self.request('/recording.mp4')
        self.assertEqual(status, 503)
        self.assertNotIn('Content-Disposition', headers)
        status, _, body = self.request('/recording.mp4?original=1', {'Range': 'bytes=0-2'})
        self.assertEqual((status, body), (200, b'b' * 4096))

    def test_in_progress_download_keeps_its_open_file(self):
        old = self.publish()
        expected = old.read_bytes()
        original_end_headers = server.Handler.end_headers
        def replace_after_open(handler):
            original_end_headers(handler)
            def different_encode(command, *args, **kwargs):
                if command[-1] != '-':
                    Path(command[-1]).write_bytes(b'new compressed content')
            with patch.object(p, 'duration', return_value=10), patch.object(p, 'run_checked', side_effect=different_encode):
                p.prepare(self.source, p.signature(self.source))
        with patch.object(server.Handler, 'end_headers', replace_after_open):
            status, _, body = self.request('/recording.mp4')
        self.assertEqual((status, body), (200, expected))
        self.assertFalse(old.exists())
        self.assertNotEqual(p.prepared(self.source).read_bytes(), expected)

    def test_download_diagnostics_count_bytes_and_verification(self):
        target = self.publish()
        with patch.object(server, 'diagnostic') as log:
            status, headers, body = self.request('/recording.mp4')
        events = log.call_args_list
        self.assertEqual(events[0].args[0], 'download_start')
        self.assertEqual(events[-1].args[0], 'request_end')
        record = events[-1].kwargs
        self.assertEqual(record['outcome'], 'sent_to_proxy')
        self.assertEqual(record['file_bytes_sent'], len(body))
        self.assertEqual(record['file_bytes_expected'], target.stat().st_size)
        self.assertEqual(record['request_id'], headers['X-Request-ID'])
        self.assertGreaterEqual(record['verify_seconds'], 0)

    def test_disconnected_download_is_not_logged_as_completed(self):
        self.publish()
        with patch.object(server, 'diagnostic') as log:
            self.request('/recording.mp4', fail_body=True)
        record = log.call_args.kwargs
        self.assertEqual(record['outcome'], 'disconnected')
        self.assertEqual(record['file_bytes_sent'], 0)
        self.assertGreater(record['file_bytes_expected'], 0)

    def test_head_and_not_ready_are_not_logged_as_downloads(self):
        with patch.object(server, 'diagnostic') as log:
            self.assertEqual(self.request('/recording.mp4')[0], 503)
        self.assertEqual(log.call_args.kwargs['outcome'], 'not_ready')
        self.publish()
        with patch.object(server, 'diagnostic') as log:
            status, _, body = self.request('/recording.mp4', method='HEAD')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(log.call_args.kwargs['outcome'], 'head_only')
        self.assertEqual(log.call_args.kwargs['file_bytes_sent'], 0)

    def test_writes_during_processing_never_publish(self):
        def changed(command, *args, **kwargs):
            self.fake_encode(command)
            self.source.write_bytes(b'new data')
        with patch.object(p, 'duration', return_value=10), patch.object(p, 'run_checked', side_effect=changed):
            with self.assertRaises(p.SourceChanged):
                p.prepare(self.source, self.before)
        self.assertIsNone(p.prepared(self.source))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_failed_or_truncated_outputs_never_publish(self):
        for durations in ([10, 5], [10, 0]):
            with patch.object(p, 'duration', side_effect=durations), patch.object(p, 'run_checked', side_effect=self.fake_encode):
                with self.assertRaises(ValueError):
                    p.prepare(self.source, self.before)
            self.assertEqual(list(self.cache.iterdir()), [])
        with patch.object(p, 'duration', return_value=10), patch.object(p, 'run_checked', side_effect=RuntimeError('encode failed')):
            with self.assertRaises(RuntimeError):
                p.prepare(self.source, self.before)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_live_process_is_cancelled_when_writes_resume(self):
        processes = []
        real_popen = subprocess.Popen
        def capture(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            processes.append(proc)
            return proc
        timer = threading.Timer(.2, lambda: self.source.write_bytes(b'new data'))
        timer.start()
        try:
            with patch.object(p.subprocess, 'Popen', side_effect=capture):
                with self.assertRaises(p.SourceChanged):
                    p.run_checked([sys.executable, '-c', 'import time; time.sleep(30)'], self.source, self.before)
        finally:
            timer.join()
        self.assertIsNotNone(processes[0].poll())


if __name__ == '__main__':
    unittest.main()
