"""Read-only recording downloads, bound to loopback for Tailscale Serve."""
import html
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import playback
import motion

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(__file__).resolve().parent / 'var' / 'log'
VIDEO = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.ts'}
DOWNLOAD_LOG = logging.getLogger('xiaomicamera.downloads')
DOWNLOAD_LOG.addHandler(logging.NullHandler())
DOWNLOAD_LOG.propagate = False
ACTION_TOKEN = secrets.token_urlsafe(32)
RECORDING_TIMES = re.compile(r'^(?:.*_)?(\d{14})_(\d{14})\.[^.]+$')


def recording_label(name):
    match = RECORDING_TIMES.fullmatch(name)
    if not match:
        return name
    try:
        start, end = (datetime.strptime(value, '%Y%m%d%H%M%S') for value in match.groups())
    except ValueError:
        return name
    if end < start:
        return name
    start_text = start.strftime('%Y-%m-%d %H:%M:%S')
    end_text = end.strftime('%H:%M:%S' if start.date() == end.date() else '%Y-%m-%d %H:%M:%S')
    return '{} ～ {}'.format(start_text, end_text)


def event_label(offset):
    return '录像内 ' + str(timedelta(seconds=int(offset)))


def configure_download_log():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / 'downloads.jsonl',
                                  maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    DOWNLOAD_LOG.addHandler(handler)
    DOWNLOAD_LOG.setLevel(logging.INFO)


def diagnostic(event, **fields):
    DOWNLOAD_LOG.info(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(),
                                     event=event, **fields), ensure_ascii=False))


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.respond(False)

    def do_GET(self):
        self.respond(True)

    def do_POST(self):
        self.respond(True)

    def respond(self, body):
        self.request_id = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.response_status = None
        self.headers_finished = None
        self.transfer_started = None
        self.file_bytes_sent = 0
        self.file_bytes_expected = None
        self.verify_seconds = 0
        self.outcome = 'response'
        self.error_type = None
        try:
            if self.command == 'POST':
                self.serve_post()
            else:
                self.serve(body)
        except (BrokenPipeError, ConnectionResetError):
            self.outcome = 'disconnected'
        except (OSError, ValueError) as error:
            self.outcome = 'error'
            self.error_type = type(error).__name__
            if self.headers_finished is None:
                self.send_error(404)
            else:
                self.close_connection = True
        finally:
            self.report('request_end')

    def send_response(self, code, message=None):
        self.response_status = code
        super().send_response(code, message)
        self.send_header('X-Request-ID', self.request_id)

    def end_headers(self):
        super().end_headers()
        self.headers_finished = time.monotonic()

    def report(self, event):
        now = time.monotonic()
        elapsed = now - self.transfer_started if self.transfer_started is not None else 0
        diagnostic(event, request_id=self.request_id, method=self.command,
                   path=unquote(urlsplit(self.path).path),
                   original=parse_qs(urlsplit(self.path).query).get('original') == ['1'],
                   peer=self.client_address[0], status=self.response_status, outcome=self.outcome,
                   error_type=self.error_type, file_bytes_expected=self.file_bytes_expected,
                   file_bytes_sent=self.file_bytes_sent,
                   verify_seconds=round(self.verify_seconds, 4),
                   headers_seconds=round(self.headers_finished - self.started, 4) if self.headers_finished else None,
                   transfer_seconds=round(elapsed, 4), total_seconds=round(now - self.started, 4),
                   send_MiB_s=round(self.file_bytes_sent / elapsed / 1024**2, 4) if elapsed else None)

    def page(self, content, body, status=200):
        page = ('<!doctype html><html lang="zh"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>小米录像下载</title><style>body{font:18px system-ui;'
                'max-width:900px;margin:40px auto;padding:0 20px}li{margin:20px 0;'
                'overflow-wrap:anywhere}.events-panel{margin-top:8px}'
                '.events-panel summary{cursor:pointer;font-size:15px}'
                '.events{display:flex;flex-wrap:wrap;gap:12px;'
                'padding:8px 0}.event{font-size:14px;width:160px}.event img{width:160px;'
                'height:90px;object-fit:cover;display:block}</style>' + content + '</html>').encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page)))
        self.send_header('Cache-Control', 'no-store')
        if status == 503:
            self.send_header('Retry-After', '30')
        self.end_headers()
        if body:
            self.wfile.write(page)

    def waiting(self, source, body):
        self.page('<h1>' + html.escape(playback.status(source)) + '</h1>'
                  '<p>压缩完成后再下载，请稍后刷新目录。</p>'
                  '<p><a href="./">返回目录</a> · '
                  '<a href="?original=1">下载原文件</a></p>', body, status=503)

    def serve(self, body):
        url = urlsplit(self.path)
        parts = Path(unquote(url.path).lstrip('/')).parts
        if any(p.startswith('.') for p in parts):
            return self.send_error(404)
        path = ROOT.joinpath(*parts).resolve()
        if path != ROOT and ROOT not in path.parents:
            return self.send_error(404)
        if path.is_dir():
            items = []
            entries = set(path.iterdir()) | set(playback.deleted_recordings(path))
            for p in sorted(entries, key=lambda p: (not p.is_dir(), p.name)):
                if p.name.startswith('.') or p.is_symlink():
                    continue
                if not p.is_dir() and p.suffix.lower() not in VIDEO:
                    continue
                href = quote('/' + str(p.relative_to(ROOT)) + ('/' if p.is_dir() else ''))
                label = recording_label(p.name) if not p.is_dir() else p.name + '/'
                name = html.escape(label)
                if label != p.name and not p.is_dir():
                    name += '<br><small>{}</small>'.format(html.escape(p.name))
                extra = ''
                if p.is_file() or not p.exists():
                    state = playback.status(p) if p.suffix.lower() == '.mp4' else '原文件'
                    extra = '<br><small>{}'.format(html.escape(state))
                    if p.is_file():
                        extra += ' · <a href="{}?original=1">下载原文件</a>'.format(href)
                        if p.suffix.lower() == '.mp4' and playback.changed(p):
                            extra += (' · <form method="post" action="{}?recompress=1&amp;token={}" '
                                      'style="display:inline"><button type="submit">重新压缩</button></form>'
                                      .format(href, ACTION_TOKEN))
                    extra += '</small>'
                    if p.suffix.lower() == '.mp4':
                        video = playback.prepared(p)
                        if video:
                            index = motion.read_index(video)
                            if index is None:
                                extra += '<br><small>画面变化分析中</small>'
                            elif index['state'] == 'failed':
                                extra += '<br><small>画面变化分析失败；录像仍可下载</small>'
                            elif not index['events']:
                                extra += '<br><small>未检测到明显活动</small>'
                            else:
                                cards = []
                                for number, event in enumerate(index['events']):
                                    label = html.escape(event_label(event['offset_seconds']))
                                    cards.append('<span class="event"><img loading="lazy" src="{}?event_thumb={}" alt="">{}</span>'
                                                 .format(href, number, label))
                                extra += ('<details class="events-panel"><summary>检测到 {} 处画面变化 · 展开关键帧</summary>'
                                          '<div class="events">{}</div></details>').format(len(cards), ''.join(cards))
                items.append('<li><a href="{}">{}</a>{}</li>'.format(href, name, extra))
            self.page('<h1>小米录像下载</h1><p>点击录像下载压缩版，再用本地播放器打开。</p>'
                      '<a href="../">返回上级</a><ul>' + ''.join(items) + '</ul>', body)
            return
        if path.suffix.lower() not in VIDEO:
            return self.send_error(404)
        source = path
        params = parse_qs(url.query)
        original = params.get('original') == ['1']
        if 'event_thumb' in params:
            if original or source.suffix.lower() != '.mp4':
                return self.send_error(404)
            video = playback.prepared(source)
            index = motion.read_index(video) if video else None
            try:
                number = int(params['event_thumb'][0])
                event = index['events'][number] if number >= 0 and index and index['state'] == 'ready' else None
            except (ValueError, IndexError, TypeError, KeyError):
                event = None
            if event is None:
                return self.send_error(404)
            thumb = motion.thumbnail_path(video, number)
            if event.get('thumbnail') != thumb.name:
                return self.send_error(404)
            try:
                data = thumb.read_bytes()
            except OSError:
                return self.send_error(404)
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        if not path.is_file() and (original or path.suffix.lower() != '.mp4' or not playback.prepared(source)):
            return self.send_error(404)
        name = source.name
        if source.suffix.lower() == '.mp4' and not original:
            # Check MD5 once per download, then pin the opened immutable file.
            verify_start = time.monotonic()
            try:
                ready = playback.prepared(source, verify_content=True)
            finally:
                self.verify_seconds = time.monotonic() - verify_start
            if ready is None:
                self.outcome = 'not_ready'
                return self.waiting(source, body)
            path = ready
            name = source.stem + '.720p.mp4'
        try:
            stream = path.open('rb')
        except FileNotFoundError:
            if path != source:
                return self.waiting(source, body)
            raise
        with stream as f:
            size = os.fstat(f.fileno()).st_size
            self.file_bytes_expected = size
            # Always send a complete download. Ignore Range/If-Range rather than
            # allowing a downloader to resume bytes from a different generation.
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name))
            self.send_header('Accept-Ranges', 'none')
            self.send_header('Content-Length', str(size))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if body:
                self.transfer_started = time.monotonic()
                self.outcome = 'sending'
                self.report('download_start')
                last_report = self.transfer_started
                remaining = size
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        self.outcome = 'source_truncated'
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                    self.file_bytes_sent += len(chunk)
                    if time.monotonic() - last_report >= 5:
                        self.report('download_progress')
                        last_report = time.monotonic()
                if remaining == 0:
                    self.outcome = 'sent_to_proxy'
            else:
                self.outcome = 'head_only'

    def serve_post(self):
        url = urlsplit(self.path)
        parts = Path(unquote(url.path).lstrip('/')).parts
        if any(p.startswith('.') for p in parts):
            return self.send_error(404)
        source = ROOT.joinpath(*parts).resolve()
        if source == ROOT or ROOT not in source.parents or source.suffix.lower() != '.mp4':
            return self.send_error(404)
        params = parse_qs(url.query)
        if params.get('recompress') != ['1'] or not hmac.compare_digest(params.get('token', [''])[0], ACTION_TOKEN):
            return self.send_error(403)
        if not playback.request_recompress(source):
            return self.send_error(409, 'Source is not eligible for recompression')
        self.send_response(303)
        self.send_header('Location', quote('/' + str(source.parent.relative_to(ROOT)) + '/'))
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()


if __name__ == '__main__':
    configure_download_log()
    diagnostic('server_start', pid=os.getpid())
    playback.start()
    print('Recording downloads: http://127.0.0.1:8765', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
