#!/usr/bin/env python3
"""Read Claude session registry and selected process/service metadata, without auth or writes."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def run_readonly(args):
    try:
        value = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return value.returncode, value.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ''


def process_metadata(pid, run=run_readonly):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return {'liveness': 'unknown', 'model_argument': None}
    code, output = run(['/bin/ps', '-p', str(pid), '-o', 'args='])
    if code is None or (code and code != 1):
        return {'liveness': 'unknown', 'model_argument': None}
    if code == 1:
        # ps can return 1 for a denied lookup too. A second existence check
        # differentiates inaccessible from definitely exited where possible.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {'liveness': 'exited', 'model_argument': None}
        except (PermissionError, OSError):
            return {'liveness': 'unknown', 'model_argument': None}
        return {'liveness': 'unknown', 'model_argument': None}
    if not output.strip():
        return {'liveness': 'unknown', 'model_argument': None}
    try:
        words = shlex.split(output)
    except ValueError:
        return {'liveness': 'live', 'model_argument': None}
    model = None
    if '--model' in words and words.index('--model') + 1 < len(words):
        candidate = words[words.index('--model') + 1]
        if re.fullmatch(r'[A-Za-z0-9_.:/\[\]-]{1,256}', candidate):
            model = candidate
    return {'liveness': 'live', 'model_argument': model}


def service_metadata(label, run=run_readonly):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', label):
        raise ValueError('Invalid launchd label')
    code, output = run(['/bin/launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + label])
    if code != 0:
        return {'label': label, 'query': 'unavailable'}
    allowed = ('state', 'pid', 'runs', 'last exit code', 'working directory', 'run interval')
    result = {'label': label, 'query': 'ok'}
    for line in output.splitlines():
        match = re.fullmatch(r'\t([^\t=]+?) = (.*)', line)
        if match and match[1] in allowed:
            result.setdefault(match[1], match[2])
    return result


def inspect(config_dir, project=None, labels=(), run=run_readonly):
    errors = []
    settings_path = config_dir / 'settings.json'
    default_model = None
    if settings_path.exists():
        try:
            default_model = json.loads(settings_path.read_text()).get('model')
        except (OSError, ValueError, AttributeError):
            errors.append({'source': 'settings.json', 'kind': 'unreadable-or-invalid'})
    registry = config_dir / 'sessions'
    sessions = []
    try:
        files = sorted(registry.glob('*.json'))
        if not registry.is_dir():
            errors.append({'source': 'sessions', 'kind': 'registry-not-found'})
    except OSError:
        files = []
        errors.append({'source': 'sessions', 'kind': 'registry-unreadable'})
    for path in files:
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError('not-an-object')
        except (OSError, ValueError):
            errors.append({'source': path.name, 'kind': 'unreadable-or-invalid'})
            continue
        cwd = data.get('cwd')
        if project is not None and (not isinstance(cwd, str) or Path(cwd).resolve() != project.resolve()):
            continue
        row = {key: data.get(key) for key in
               ('pid', 'sessionId', 'bridgeSessionId', 'cwd', 'status', 'statusUpdatedAt', 'kind', 'entrypoint')}
        row.update(process_metadata(data.get('pid'), run))
        sessions.append(row)
    return {
        'registered_sessions_only': True,
        'model_argument_is_startup_evidence_only': True,
        'config_dir': str(config_dir),
        'default_model': default_model,
        'sessions': sessions,
        'services': [service_metadata(label, run) for label in labels],
        'errors': errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path,
                        default=Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')))
    parser.add_argument('--project', type=Path)
    parser.add_argument('--launchd-label', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(inspect(args.config_dir, args.project, args.launchd_label), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
