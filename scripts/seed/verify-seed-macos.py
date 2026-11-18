#!/usr/bin/env python3
"""macOS only (CI): check packed RAM Data seeds by expanding each one to a raw image, attaching it READ-ONLY and reading it back.

Usage: verify-seed-macos.py --packer PACKER.py --uuid APFS-VOLUME-UUID --json OUT.json NAME=FILE:KIND [...]
KIND is one of clean | watchdog | skipsetup. Checks per seed:
  every seed      raw image starts with NXSB at byte 32; diskutil says the volume UUID equals --uuid; /staged_system_apps is absent;
                  the packed file is at most 8 MiB (a larger one means nonzero blocks were left behind by a copy-then-delete)
  watchdog,skipsetup   db/com.apple.xpc.launchd/disabled.plist maps com.apple.watchdogd and com.apple.watchdog to true; disabled.migrated exists
  skipsetup       mobile/Library/Preferences/com.apple.purplebuddy.plist has SetupDone and SetupFinishedAllSteps true
  clean           disabled.plist does not map com.apple.watchdogd to true (the clean seed must not already carry the override)
Nothing is written to any image (attached read-only). Exit 1 on any failed check. Needs sudo for ownership-honoring mounts, like the editing scripts.
"""
import argparse
import hashlib
import importlib.util
import json
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_PACKED = 8 * 1024 * 1024


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--packer', type=Path, required=True)
    ap.add_argument('--uuid', required=True)
    ap.add_argument('--json', type=Path, required=True)
    ap.add_argument('seeds', nargs='+')
    args = ap.parse_args()
    if sys.platform != 'darwin':
        ap.error('macOS only')
    spec = importlib.util.spec_from_file_location('packed_ramdisk', args.packer)
    packer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packer)
    report, failed = {}, []
    for item in args.seeds:
        name, _, rest = item.partition('=')
        file, _, kind = rest.rpartition(':')
        path = Path(file)
        checks = {}
        blob = path.read_bytes()
        logical, extents = packer.regions(blob)
        checks['packed_bytes_at_most_8_MiB'] = len(blob) <= MAX_PACKED
        with tempfile.TemporaryDirectory(prefix='seedcheck-') as td:
            raw, mount = Path(td) / 'data.img', Path(td) / 'mount'
            mount.mkdir()
            with raw.open('xb') as stream:
                stream.truncate(logical)
                for offset, data in extents:
                    stream.seek(offset)
                    stream.write(data)
            with raw.open('rb') as stream:
                stream.seek(32)
                checks['nxsb_magic'] = stream.read(4) == b'NXSB'
            result = subprocess.run(['hdiutil', 'attach', '-plist', '-readonly', '-nobrowse', '-owners', 'on',
                                     '-mountpoint', str(mount), str(raw)], capture_output=True, check=True)
            try:
                info = plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', str(mount)]))
                checks['volume_uuid_matches'] = info['VolumeUUID'].upper() == args.uuid.upper()
                checks['no_staged_system_apps'] = not (mount / 'staged_system_apps').exists()
                policy = mount / 'db/com.apple.xpc.launchd/disabled.plist'
                disabled = plistlib.loads(policy.read_bytes()) if policy.is_file() and not policy.is_symlink() else {}
                if kind in ('watchdog', 'skipsetup'):
                    checks['watchdogd_disabled'] = disabled.get('com.apple.watchdogd') is True and disabled.get('com.apple.watchdog') is True
                    checks['migration_marker'] = (mount / 'db/com.apple.xpc.launchd/disabled.migrated').is_file()
                if kind == 'clean':
                    checks['clean_has_no_watchdog_override'] = disabled.get('com.apple.watchdogd') is not True
                if kind == 'skipsetup':
                    prefs = mount / 'mobile/Library/Preferences/com.apple.purplebuddy.plist'
                    values = plistlib.loads(prefs.read_bytes()) if prefs.is_file() else {}
                    checks['setup_done'] = values.get('SetupDone') is True and values.get('SetupFinishedAllSteps') is True
            finally:
                subprocess.run(['hdiutil', 'detach', str(mount)], check=False, capture_output=True)
        report[name] = dict(file=path.name, kind=kind, bytes=len(blob), sha256=sha256(path), logical_bytes=logical, checks=checks)
        for check, passed in checks.items():
            print(('PASS ' if passed else 'FAIL ') + f'{name}: {check}')
            if not passed:
                failed.append(f'{name}: {check}')
    args.json.write_text(json.dumps(report, indent=2) + '\n')
    if failed:
        print('FAILED:', '; '.join(failed))
        sys.exit(1)
    print('ALL SEED CHECKS PASSED')


if __name__ == '__main__':
    main()
