"""Index local motion in immutable compressed camera recordings."""
import json
import logging
import os
import subprocess
import time
import uuid
from collections import deque
import cv2
import numpy as np

WIDTH, HEIGHT, FPS = 320, 180, 2
FRAME_BYTES = WIDTH * HEIGHT
COMPARE_FRAMES = 4  # Two seconds catches slow motion in night footage.
RECIPE = 'opencv-local-motion-320x180-2fps-2s-v2'


def index_path(video):
    return video.with_suffix('.events.json')


def thumbnail_path(video, number):
    return video.with_suffix('.event-{}.png'.format(number))


def read_index(video):
    try:
        record = json.loads(index_path(video).read_text())
        if record.get('recipe') == RECIPE and record.get('video') == video.name and record.get('state') in ('ready', 'failed'):
            return record
    except (OSError, ValueError, AttributeError):
        pass
    return None


def activity(previous, current):
    """Score local contours after compensating for a uniform exposure change."""
    before = cv2.GaussianBlur(previous, (5, 5), 0)
    after = cv2.GaussianBlur(current, (5, 5), 0)
    signed = after.astype(np.int16) - before.astype(np.int16)
    signed -= int(np.median(signed))
    mask = (np.abs(signed) >= 15).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    changed = int(cv2.countNonZero(mask))
    if changed < 100 or changed > FRAME_BYTES * .55:
        return 0
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    return max((int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, count)), default=0)


def detect(frames):
    """Yield (seconds, representative gray frame) for sustained local motion."""
    recent = deque(maxlen=COMPARE_FRAMES)
    group_start = group_last = None
    group_count = group_score = 0
    group_frame = None
    for number, frame in enumerate(frames):
        offset = number / FPS
        score = activity(recent[0], frame) if len(recent) == COMPARE_FRAMES else 0
        recent.append(frame)
        if score < 100:
            continue
        if group_last is not None and offset - group_last > 3:
            if group_count >= 2:
                yield group_start, group_frame
            group_start = None
        if group_start is None:
            group_start, group_count, group_score = offset, 0, 0
        group_count += 1
        group_last = offset
        if score > group_score:
            group_score, group_frame = score, frame.copy()
    if group_count >= 2:
        yield group_start, group_frame


def decoded_frames(video, ffmpeg):
    command = [ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-i', str(video),
               '-an', '-vf', 'fps=2,scale=320:180,format=gray', '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            frame = process.stdout.read(FRAME_BYTES)
            if not frame:
                break
            if len(frame) != FRAME_BYTES:
                raise ValueError('Partial decoded frame')
            yield np.frombuffer(frame, np.uint8).reshape(HEIGHT, WIDTH)
        error = process.stderr.read()
        if process.wait() != 0:
            raise RuntimeError(error.decode(errors='replace')[-2000:])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def build(video, ffmpeg):
    """Publish an index for one immutable compressed copy; failure never blocks downloads."""
    if read_index(video):
        return
    temporary = []
    event_records = []
    try:
        for number, (offset, frame) in enumerate(detect(decoded_frames(video, ffmpeg))):
            target = thumbnail_path(video, number)
            part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
            temporary.append(part)
            ok, encoded = cv2.imencode('.png', frame)
            if not ok:
                raise ValueError('Cannot encode thumbnail')
            part.write_bytes(encoded.tobytes())
            os.replace(str(part), str(target))
            event_records.append({'offset_seconds': round(offset, 1), 'thumbnail': target.name})
        record = {'recipe': RECIPE, 'video': video.name, 'state': 'ready',
                  'events': event_records, 'analyzed_at': time.time()}
    except Exception as error:
        logging.exception('Motion analysis failed for %s', video.name)
        record = {'recipe': RECIPE, 'video': video.name, 'state': 'failed',
                  'error': type(error).__name__, 'events': [], 'analyzed_at': time.time()}
    finally:
        for part in temporary:
            if part.exists():
                part.unlink()
    target = index_path(video)
    part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
    try:
        part.write_text(json.dumps(record))
        os.replace(str(part), str(target))
        keep = {event['thumbnail'] for event in event_records} if record['state'] == 'ready' else set()
        for thumb in video.parent.glob(video.stem + '.event-*.png'):
            if thumb.name not in keep:
                thumb.unlink()
    finally:
        if part.exists():
            part.unlink()
