"""Compress quiet camera recordings and atomically publish playback copies."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import motion

ROOT = Path(os.environ.get('XIAOMI_CAMERA_ROOT', '/Users/Shared/XiaomiCamera')).resolve()
STATE = Path(os.environ.get('XIAOMI_CAMERA_STATE', str(ROOT / '.remote-access' / 'var'))).resolve()
CACHE = STATE / 'playback'
FFMPEG = shutil.which('ffmpeg') or '/Applications/Elmedia Player.app/Contents/Resources/ffmpeg'
SETTLE_SECONDS = 5 * 60
POLL_SECONDS = 15
POST_COMPRESSION_GRACE_SECONDS = 60 * 60
RECIPE = 'hevc-720p-15fps-400k-aac32-snapshot-md5-v4'
STATES = {}
STALE_HINTS = set()
FORCE_RECOMPRESS = {}
INVALIDATIONS = queue.SimpleQueue()
REJECTED = set()
STATE_LOCK = threading.Lock()
QUIET_LABEL = '连续 {} 分钟无变化'.format(SETTLE_SECONDS // 60)


class SourceChanged(Exception):
    pass


def signature(source):
    return stat_signature(source.stat())


def stat_signature(stat):
    # Detect preallocated writes, preserved mtimes and file replacement.
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev, RECIPE]


def invalidate(source):
    """Reset the quiet timer once, even if a client keeps retrying old ranges."""
    with STATE_LOCK:
        if source not in REJECTED:
            REJECTED.add(source)
            INVALIDATIONS.put(source)
    STATES[source] = '源录像已变化，重新等待文件稳定'


def drain_invalidations(quiet):
    while True:
        try:
            quiet.observed.pop(INVALIDATIONS.get_nowait(), None)
        except queue.Empty:
            return


def source_md5(source, before, destination=None):
    """Bounded read with both descriptor and pathname checks before/after.

    MD5 identifies accidental content changes, not hostile tampering. The NAS
    writer does not share our lock; this is optimistic validation, not an atomic
    filesystem snapshot.
    """
    assert_unchanged(source, before)
    digest = hashlib.md5()
    with source.open('rb') as stream:
        if stat_signature(os.fstat(stream.fileno())) != before:
            raise SourceChanged(source.name)
        remaining = before[0]
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise SourceChanged(source.name)
            if destination is not None:
                destination.write(block)
            digest.update(block)
            remaining -= len(block)
            assert_unchanged(source, before)
        if stream.read(1) or stat_signature(os.fstat(stream.fileno())) != before:
            raise SourceChanged(source.name)
    assert_unchanged(source, before)
    return digest.hexdigest()


def make_snapshot(source, before, snapshot):
    with snapshot.open('xb') as output:
        copied_md5 = source_md5(source, before, destination=output)
    # A second independent read catches a torn copy even when metadata appears
    # unchanged. Encoding will only ever read the private copy.
    if source_md5(source, before) != copied_md5:
        raise SourceChanged(source.name)
    snapshot.chmod(0o400)
    return copied_md5


class QuietWindow:
    """Require a full observed quiet interval, including after a restart."""
    def __init__(self, seconds=SETTLE_SECONDS):
        self.seconds = seconds
        self.observed = {}

    def ready(self, source, fingerprint, now=None, wall=None):
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        previous = self.observed.get(source)
        if previous is None or previous[0] != fingerprint:
            self.observed[source] = (fingerprint, now)
            return False
        last_write = max(fingerprint[1], fingerprint[2]) / 1e9
        return (now - previous[1] >= self.seconds and
                wall - last_write >= self.seconds and fingerprint[0] > 0)


def cache_paths(source):
    key = hashlib.sha256(str(source.relative_to(ROOT)).encode()).hexdigest()
    return CACHE / (key + '.mp4'), CACHE / (key + '.json')


def completed_at(record, manifest):
    """Old manifests use their publication mtime as the completion timestamp."""
    value = record.get('completed_at')
    return value if isinstance(value, (int, float)) else manifest.stat().st_mtime


def changed_after_grace(record, manifest, current):
    return max(current[1], current[2]) / 1e9 > completed_at(record, manifest) + POST_COMPRESSION_GRACE_SECONDS


def keep_older_copy(source):
    with STATE_LOCK:
        STALE_HINTS.add(source)


def cached_record(source):
    _, manifest = cache_paths(source)
    record = json.loads(manifest.read_text())
    if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{32}', record.get('source_md5', '')):
        return None
    name = record.get('file', '')
    if not name or Path(name).name != name:
        return None
    target = CACHE / name
    if not target.is_file() or target.stat().st_size != record.get('output_bytes'):
        return None
    return record, manifest, target


def deleted_recordings():
    """Find published copies whose indexed source has since disappeared."""
    for manifest in CACHE.glob('*.json'):
        try:
            record = json.loads(manifest.read_text())
            relative = record.get('source_relative')
            if not isinstance(relative, str):
                continue
            source = ROOT / relative
            if (source.suffix.lower() != '.mp4' or
                    source.exists() or source.is_symlink() or
                    cache_paths(source)[1] != manifest):
                continue
            if cached_record(source):
                yield source
        except (OSError, ValueError, TypeError):
            continue


def index_existing_manifests():
    """Migrate old manifests while their source filenames are still known."""
    for source in recordings():
        _, manifest = cache_paths(source)
        try:
            record = json.loads(manifest.read_text())
            if not isinstance(record, dict) or record.get('source_relative'):
                continue
            record['completed_at'] = completed_at(record, manifest)
            record['source_relative'] = str(source.relative_to(ROOT))
            temporary = manifest.with_suffix('.index.part.json')
            temporary.write_text(json.dumps(record))
            os.replace(temporary, manifest)
        except (OSError, ValueError, TypeError):
            continue


def request_recompress(source):
    if not source.is_file() or not prepared(source, verify_content=True):
        return False
    with STATE_LOCK:
        if source not in STALE_HINTS:
            return False
        FORCE_RECOMPRESS[source] = time.monotonic()
        INVALIDATIONS.put(source)
    STATES[source] = '等待文件稳定后重新压缩（' + QUIET_LABEL + '）；旧压缩版仍可下载'
    return True


def forget(source):
    with STATE_LOCK:
        REJECTED.discard(source)
        STALE_HINTS.discard(source)
        FORCE_RECOMPRESS.pop(source, None)
    STATES.pop(source, None)


def purge_cache(source):
    """Remove every published copy, manifest, motion index and thumbnail for a source."""
    old_target, manifest = cache_paths(source)
    removed = False
    try:
        manifest.unlink()
        removed = True
    except FileNotFoundError:
        pass
    for path in set(CACHE.glob(old_target.stem + '.*.mp4')) | {old_target}:
        if path.name.endswith(('.part.mp4', '.snapshot.mp4')):
            continue
        for generated in [path, motion.index_path(path)] + list(CACHE.glob(path.stem + '.event-*.png')):
            try:
                generated.unlink()
                removed = True
            except FileNotFoundError:
                pass
    forget(source)
    return removed


def delete_compressed(source):
    """Drop the playback copy; a surviving source is compressed and indexed again."""
    if not prepared(source):
        return False
    purge_cache(source)
    logging.info('Deleted compressed copy of %s', source.name)
    return True


def delete_original(source):
    """Delete the NAS original; with no compressed copy left, forget the recording."""
    if not source.is_file() or source.is_symlink():
        return False
    source.unlink()
    if not prepared(source):
        purge_cache(source)
    forget(source)
    logging.info('Deleted original %s', source.name)
    return True


def recompress_requested(source):
    with STATE_LOCK:
        return source in FORCE_RECOMPRESS


def changed(source):
    prepared(source)
    with STATE_LOCK:
        return source in STALE_HINTS


def prepared(source, verify_content=False):
    if source.suffix.lower() != '.mp4':
        return None
    try:
        cached = cached_record(source)
        if cached is None:
            return None
        record, manifest, target = cached
        if not source.exists():
            return target if record.get('source_relative') == str(source.relative_to(ROOT)) else None
        with STATE_LOCK:
            if source in REJECTED:
                return None
        before = signature(source)
        if record.get('signature') != before:
            if changed_after_grace(record, manifest, before):
                keep_older_copy(source)
                return target
            invalidate(source)
            return None
        if verify_content:
            try:
                matches = source_md5(source, before) == record['source_md5']
            except SourceChanged:
                current = signature(source)
                if changed_after_grace(record, manifest, current):
                    keep_older_copy(source)
                    return target
                invalidate(source)
                return None
            if not matches:
                if time.time() > completed_at(record, manifest) + POST_COMPRESSION_GRACE_SECONDS:
                    keep_older_copy(source)
                    return target
                invalidate(source)
                return None
        if verify_content:
            with STATE_LOCK:
                STALE_HINTS.discard(source)
        return target
    except (OSError, ValueError, TypeError, SourceChanged):
        if verify_content:
            invalidate(source)
    return None


def status(source):
    target = prepared(source)
    if target:
        if not source.exists():
            return '已压缩 · {:.1f} MiB · 源录像已删除，仅保留压缩版'.format(target.stat().st_size / 1024**2)
        with STATE_LOCK:
            changed = source in STALE_HINTS
            requested = source in FORCE_RECOMPRESS
        hint = (' · 提示：源录像发生变化，旧压缩版仍可下载' +
                ('；' + STATES.get(source, '等待重新压缩') if requested else '')) if changed else ''
        return '已压缩 · {:.1f} MiB{}'.format(target.stat().st_size / 1024**2, hint)
    return STATES.get(source, '等待文件稳定（' + QUIET_LABEL + '）')


def assert_unchanged(source, before):
    try:
        if signature(source) == before:
            return
    except OSError:
        pass
    raise SourceChanged(source.name)


def run_checked(command, source, before, timeout=3600):
    """Cancel promptly if NAS writes resume; avoid filling a stderr pipe."""
    with tempfile.TemporaryFile(dir=str(CACHE)) as log:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
        deadline = time.monotonic() + timeout
        try:
            while True:
                assert_unchanged(source, before)
                if time.monotonic() >= deadline:
                    raise TimeoutError('Compression timed out')
                try:
                    code = process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    pass
            assert_unchanged(source, before)
            if code:
                log.seek(max(0, log.tell() - 3000))
                raise RuntimeError(log.read().decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def duration(path):
    result = subprocess.run([FFMPEG, '-hide_banner', '-nostdin', '-i', str(path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    match = re.search(rb'Duration: (\d+):(\d+):([\d.]+)', result.stderr)
    if not match:
        raise ValueError('Cannot read media duration: ' + path.name)
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def prepare(source, before):
    """Called only after the scheduler observes a complete quiet window."""
    assert_unchanged(source, before)
    CACHE.mkdir(parents=True, exist_ok=True)
    old_target, manifest = cache_paths(source)
    token = uuid.uuid4().hex
    temporary = old_target.with_suffix('.' + token + '.part.mp4')
    snapshot = old_target.with_suffix('.' + token + '.snapshot.mp4')
    target = old_target.with_suffix('.' + token + '.mp4')
    meta_tmp = manifest.with_suffix('.' + token + '.part.json')
    published = False
    try:
        snapshot_md5 = make_snapshot(source, before, snapshot)
        source_duration = duration(snapshot)
        command = [
            '/usr/bin/nice', '-n', '10', FFMPEG, '-hide_banner', '-nostdin',
            '-v', 'error', '-xerror', '-y', '-threads', '2', '-i', str(snapshot),
            '-map', '0:v:0', '-map', '0:a:0?', '-filter_threads', '2',
            '-vf', "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=15",
            # 400k is a ceiling: VideoToolbox spends far less on static scenes.
            # The Intel HEVC encoder only opens with nv12; hvc1 lets iOS play it.
            '-c:v', 'hevc_videotoolbox', '-allow_sw', '1', '-b:v', '400k',
            '-maxrate', '600k', '-bufsize', '1200k', '-g', '45', '-pix_fmt', 'nv12', '-tag:v', 'hvc1',
            '-c:a', 'aac', '-b:a', '32k', '-af', 'aresample=async=1:first_pts=0',
            '-movflags', '+faststart', str(temporary),
        ]
        run_checked(command, source, before)
        output_duration = duration(temporary)
        if source_duration <= 0 or abs(output_duration - source_duration) > max(1, source_duration * .002):
            raise ValueError('Compressed duration does not match source')
        run_checked([FFMPEG, '-hide_banner', '-nostdin', '-v', 'error', '-xerror',
                     '-ss', str(max(0, output_duration - 1)), '-i', str(temporary),
                     '-frames:v', '1', '-an', '-f', 'null', '-'], source, before, timeout=60)
        if temporary.stat().st_size >= before[0]:
            raise ValueError('Compression did not reduce file size; original retained')
        if source_md5(source, before) != snapshot_md5:
            raise SourceChanged(source.name)
        assert_unchanged(source, before)
        # Immutable video + atomic manifest: clients never see partial output.
        os.replace(str(temporary), str(target))
        record = {'signature': before, 'source_md5': snapshot_md5, 'file': target.name,
                  'source_bytes': before[0], 'output_bytes': target.stat().st_size,
                  'duration_seconds': output_duration, 'completed_at': time.time(),
                  'source_relative': str(source.relative_to(ROOT))}
        meta_tmp.write_text(json.dumps(record))
        assert_unchanged(source, before)
        os.replace(str(meta_tmp), str(manifest))
        published = True
        with STATE_LOCK:
            REJECTED.discard(source)
            STALE_HINTS.discard(source)
            FORCE_RECOMPRESS.pop(source, None)
        logging.info('Compressed %s: %.1f -> %.1f MiB (%.0f%% smaller), %.2fs',
                     source.name, before[0] / 1024**2, record['output_bytes'] / 1024**2,
                     (1 - record['output_bytes'] / before[0]) * 100, output_duration)
        # Remove superseded generated copies only; originals are never modified.
        obsolete = set(CACHE.glob(old_target.stem + '.*.mp4')) | {old_target}
        for path in obsolete:
            if path != target and not path.name.endswith(('.part.mp4', '.snapshot.mp4')):
                try:
                    path.unlink()
                    motion.index_path(path).unlink(missing_ok=True)
                    for thumb in CACHE.glob(path.stem + '.event-*.png'):
                        thumb.unlink()
                except FileNotFoundError:
                    pass
    finally:
        for path in [temporary, snapshot, meta_tmp] + ([] if published else [target]):
            if path.exists():
                path.unlink()


def recordings():
    for directory, dirs, files in os.walk(str(ROOT), followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.') and not Path(directory, d).is_symlink()]
        for name in sorted(files):
            path = Path(directory, name)
            if not name.startswith('.') and path.suffix.lower() == '.mp4' and not path.is_symlink():
                yield path


def worker():
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / '.worker.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.error('Another compression worker is already running')
            return
        _watch()


def _watch():
    quiet = QuietWindow()
    retry_after = {}
    future = active_source = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        while True:
            drain_invalidations(quiet)
            if future is not None and future.done():
                try:
                    future.result()
                    STATES.pop(active_source, None)
                    retry_after.pop(active_source, None)
                except SourceChanged:
                    quiet.observed.pop(active_source, None)
                    STATES[active_source] = '文件又有写入，重新等待 {} 分钟'.format(SETTLE_SECONDS // 60)
                    logging.info('Writes resumed; cancelled %s', active_source.name)
                except Exception:
                    logging.exception('Cannot compress %s', active_source.name)
                    retry_after[active_source] = time.monotonic() + 300
                    STATES[active_source] = '压缩失败，稍后重试；可打开原文件'
                future = active_source = None
            seen = set()
            for source in recordings():
                seen.add(source)
                try:
                    fingerprint = signature(source)
                    ready = quiet.ready(source, fingerprint)
                    with STATE_LOCK:
                        requested_at = FORCE_RECOMPRESS.get(source)
                    if requested_at is not None and time.monotonic() - requested_at < SETTLE_SECONDS:
                        ready = False
                    if (prepared(source) and not recompress_requested(source)) or source == active_source:
                        continue
                    if not ready:
                        STATES[source] = '等待文件稳定（' + QUIET_LABEL + '）'
                    elif time.monotonic() < retry_after.get(source, 0):
                        continue
                    elif future is None:
                        STATES[source] = '正在压缩（720p）'
                        logging.info('Quiet for %ss; compressing %s', SETTLE_SECONDS, source.name)
                        active_source = source
                        future = executor.submit(prepare, source, fingerprint)
                    else:
                        STATES[source] = '等待压缩'
                except OSError:
                    quiet.observed.pop(source, None)
            for source in set(quiet.observed) - seen:
                quiet.observed.pop(source, None)
                STATES.pop(source, None)
                retry_after.pop(source, None)
                with STATE_LOCK:
                    REJECTED.discard(source)
                    STALE_HINTS.discard(source)
                    FORCE_RECOMPRESS.pop(source, None)
            time.sleep(POLL_SECONDS)


def start():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    index_existing_manifests()
    if not Path(FFMPEG).is_file():
        logging.error('FFmpeg not found: %s', FFMPEG)
        return
    threading.Thread(target=worker, daemon=True, name='compression-watcher').start()
    threading.Thread(target=motion_worker, daemon=True, name='motion-indexer').start()


def motion_worker():
    """Index current published copies, including copies made before this feature."""
    while True:
        try:
            for manifest in CACHE.glob('*.json'):
                try:
                    record = json.loads(manifest.read_text())
                    relative = record.get('source_relative')
                    if not isinstance(relative, str):
                        continue
                    source = ROOT / relative
                    if cache_paths(source)[1] != manifest:
                        continue
                    video = prepared(source)
                    if video and not motion.read_index(video):
                        motion.build(video, FFMPEG)
                except (OSError, ValueError, TypeError):
                    logging.exception('Cannot inspect motion index for %s', manifest.name)
        except OSError:
            logging.exception('Cannot scan playback cache for motion indexes')
        time.sleep(POLL_SECONDS)
