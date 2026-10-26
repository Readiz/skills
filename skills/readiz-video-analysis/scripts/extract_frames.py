#!/usr/bin/env python3
"""Extract local frames with ffmpeg; never imply visual review or exact source PTS."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--times', required=True, nargs='+', type=float)
    parser.add_argument('--origin-offset', type=float, default=0)
    parser.add_argument('--source-url', default='')
    args = parser.parse_args()
    if not all(math.isfinite(t) and t >= 0 for t in [*args.times, args.origin_offset]):
        parser.error('Times and offset must be finite and nonnegative')
    source = args.input.resolve(strict=True)
    probe = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_format', '-show_streams',
        '-of', 'json', str(source)], timeout=60))
    if not any(s.get('codec_type') == 'video' for s in probe['streams']):
        parser.error('Input has no video stream')
    duration = float(probe['format']['duration'])
    if any(t >= duration for t in args.times):
        parser.error('Requested time is outside input duration')
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {'input': str(source), 'input_sha256': digest(source),
                'source_url': args.source_url, 'duration': duration,
                'origin_offset_seconds': args.origin_offset,
                'timing_status': 'requested times; source alignment needs verification',
                'frames': []}

    def save():
        temp = args.output / 'manifest.tmp'
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        os.replace(temp, args.output / 'manifest.json')

    save()
    for index, seconds in enumerate(dict.fromkeys(args.times)):
        target = args.output / f'frame-{index:04d}.png'
        pending = args.output / f'frame-{index:04d}.pending.png'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n',
                        '-ss', str(seconds), '-i', str(source), '-map', '0:v:0',
                        '-frames:v', '1', str(pending)], check=True, timeout=120)
        if not pending.is_file() or pending.stat().st_size == 0:
            raise RuntimeError(f'No frame at requested time {seconds}')
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror',
                        '-i', str(pending), '-f', 'null', '-'], check=True, timeout=30)
        os.replace(pending, target)
        manifest['frames'].append({
            'file': target.name, 'sha256': digest(target),
            'requested_local_seconds': seconds,
            'candidate_source_seconds': args.origin_offset + seconds,
            'visual_review': 'pending'})
        save()
    print(json.dumps({'manifest': str(args.output / 'manifest.json'),
                      'frames': len(manifest['frames'])}))


if __name__ == '__main__':
    main()
