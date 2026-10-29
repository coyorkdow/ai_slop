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
from string import Template
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import playback
import motion

ROOT = playback.ROOT
WEB = Path(__file__).resolve().parent / 'web'
LOG_DIR = playback.STATE / 'log'
VIDEO = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.ts'}
DOWNLOAD_LOG = logging.getLogger('xiaomicamera.downloads')
DOWNLOAD_LOG.addHandler(logging.NullHandler())
DOWNLOAD_LOG.propagate = False
ACTION_TOKEN = secrets.token_urlsafe(32)
RECORDING_TIMES = re.compile(r'^(?:.*_)?(\d{14})_(\d{14})\.[^.]+$')
DATE_FOLDER = re.compile(r'\d{4}(?:-\d{2}(?:-\d{2})?)?')


def recording_times(name):
    match = RECORDING_TIMES.fullmatch(name)
    if not match:
        return None
    try:
        start, end = (datetime.strptime(value, '%Y%m%d%H%M%S') for value in match.groups())
    except ValueError:
        return None
    return (start, end) if end >= start else None


def recording_label(name):
    times = recording_times(name)
    if not times:
        return name
    start, end = times
    start_text = start.strftime('%Y-%m-%d %H:%M:%S')
    end_text = end.strftime('%H:%M:%S' if start.date() == end.date() else '%Y-%m-%d %H:%M:%S')
    return '{} ～ {}'.format(start_text, end_text)


def recording_date(name):
    """Virtual folder key (YYYY-MM-DD) from the recording's start time."""
    times = recording_times(name)
    return times[0].strftime('%Y-%m-%d') if times else None


def date_folder_label(key):
    if len(key) == 4:
        return key
    if len(key) == 7:
        return datetime.strptime(key, '%Y-%m').strftime('%B')
    day = datetime.strptime(key, '%Y-%m-%d')
    return day.strftime('%b {} (%a)').format(day.day)


def listing_href(directory, date=''):
    relative = directory.relative_to(ROOT).as_posix()
    href = '/' if relative == '.' else quote('/' + relative + '/')
    return href + ('?date=' + date if date else '')


def recording_listing(source):
    return listing_href(source.parent, recording_date(source.name) or '')


def file_size(path):
    try:
        size = path.stat().st_size
    except OSError:
        return '—'
    for unit in ('B', 'KiB', 'MiB'):
        if size < 1024:
            return '{:.0f} {}'.format(size, unit) if unit == 'B' else '{:.1f} {}'.format(size, unit)
        size /= 1024
    return '{:.1f} GiB'.format(size)


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

    def page(self, template, body, status=200, **values):
        page = Template((WEB / template).read_text(encoding='utf-8')).substitute(values).encode('utf-8')
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
        self.page('waiting.html', body, status=503, status_text=html.escape(playback.status(source)),
                  back=html.escape(recording_listing(source)))

    def serve(self, body):
        url = urlsplit(self.path)
        if url.path == '/assets/style.css':
            data = (WEB / 'style.css').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        parts = Path(unquote(url.path).lstrip('/')).parts
        if any(p.startswith('.') for p in parts):
            return self.send_error(404)
        path = ROOT.joinpath(*parts).resolve()
        if path != ROOT and ROOT not in path.parents:
            return self.send_error(404)
        if path.is_dir():
            return self.directory(path, parse_qs(url.query).get('date', [''])[0], body)
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

    def directory(self, path, date, body):
        """List a directory, grouping dated recordings into year/month/day folders."""
        if date and not DATE_FOLDER.fullmatch(date):
            return self.send_error(404)
        child = {0: 4, 4: 7, 7: 10}.get(len(date))
        dirs, folders, files = [], {}, []
        entries = set(path.iterdir()) | set(playback.deleted_recordings(path))
        for p in sorted(entries, key=lambda p: p.name):
            if p.name.startswith('.') or p.is_symlink():
                continue
            if p.is_dir():
                if not date:
                    dirs.append(p)
                continue
            if p.suffix.lower() not in VIDEO:
                continue
            key = recording_date(p.name)
            if key is None:
                if not date:
                    files.append(p)
            elif key.startswith(date):
                if child:
                    folders[key[:child]] = folders.get(key[:child], 0) + 1
                else:
                    files.append(p)
        rows = []
        for p in dirs:
            rows.append('<li class="folder"><span class="icon">📁</span><a class="name" href="{}">{}</a>'
                        '<span class="meta">文件夹</span></li>'.format(listing_href(p), html.escape(p.name)))
        for key, count in folders.items():
            rows.append('<li class="folder"><span class="icon">📁</span><a class="name" href="{}">{}</a>'
                        '<span class="meta">{} 个录像</span></li>'
                        .format(html.escape(listing_href(path, key)), date_folder_label(key), count))
        rows.extend(self.recording_row(p) for p in files)
        if not rows:
            rows.append('<li class="empty">此文件夹为空</li>')

        crumbs = [(listing_href(ROOT), '全部录像')]
        current = ROOT
        for part in path.relative_to(ROOT).parts:
            current = current / part
            crumbs.append((listing_href(current), part))
        for length in (4, 7, 10):
            if len(date) >= length:
                crumbs.append((listing_href(path, date[:length]), date_folder_label(date[:length])))
        trail = ' <span class="sep">›</span> '.join(
            '<a href="{}">{}</a>'.format(html.escape(href), html.escape(label)) for href, label in crumbs[:-1])
        trail += (' <span class="sep">›</span> ' if len(crumbs) > 1 else '') + \
            '<span class="current">{}</span>'.format(html.escape(crumbs[-1][1]))
        if len(crumbs) > 1:
            up = '<a class="up" href="{}" title="返回上级">↑ 上级</a>'.format(html.escape(crumbs[-2][0]))
        else:
            up = '<span class="up disabled">↑ 上级</span>'
        self.page('index.html', body, crumbs=trail, up=up, items=''.join(rows))

    def recording_row(self, p):
        href = quote('/' + str(p.relative_to(ROOT)))
        label = recording_label(p.name)
        name = html.escape(label)
        if label != p.name:
            name += '<br><small>{}</small>'.format(html.escape(p.name))
        state = playback.status(p) if p.suffix.lower() == '.mp4' else '原文件'
        extra = '<br><small>{}'.format(html.escape(state))
        if p.is_file():
            extra += ' · <a href="{}?original=1">下载原文件</a>'.format(href)
            if p.suffix.lower() == '.mp4' and playback.changed(p):
                extra += (' · <form method="post" action="{}?recompress=1&amp;token={}" '
                          'class="recompress"><button type="submit">重新压缩</button></form>'
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
        return ('<li class="file"><span class="icon">🎞️</span><div class="name"><a href="{}">{}</a>{}</div>'
                '<span class="meta">{}</span></li>').format(href, name, extra, file_size(p))

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
        self.send_header('Location', recording_listing(source))
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()


if __name__ == '__main__':
    configure_download_log()
    diagnostic('server_start', pid=os.getpid())
    playback.start()
    print('Recording downloads: http://127.0.0.1:8765', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
