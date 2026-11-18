#!/usr/bin/env python3
"""Decrypt and verify the artifact of .github/workflows/amethyst-ram-seed.yml on the PC (Linux or macOS). Reads files, writes only below --out.

  decrypt-verify-seed.py ARTIFACT_DIR --out OUTDIR [--packer packed_ramdisk.py]

ARTIFACT_DIR holds what the workflow uploaded (ram-seed.tar.gz.enc, seed-public.json, SHA256SUMS-encrypted), as downloaded and unzipped from the
Actions page. The passphrase is read from the environment variable ARTIFACT_PASSPHRASE or typed at a prompt: it is never taken from the command line,
written to disk or printed. Checks:
  * the encrypted file and seed-public.json match SHA256SUMS-encrypted; the decrypted SHA256SUMS matches every unpacked file; the decrypted
    seed-public.json equals the uploaded one
  * the packer used for verification is the pinned one (sha256 efb5fac2...)
  * each packed seed: valid DBRDISK1 header and extents, logical size 2 GiB, packed size <= 8 MiB, raw image has NXSB at byte 32, an APFS volume
    superblock (APSB) whose UUID equals seed-public.json's volume_uuid, and the sha256/size recorded in seed-public.json
  * HEURISTIC content checks (Linux cannot read APFS files; the CI job did the real read-only mount checks): the watchdog and skipsetup seeds contain the
    launchd override text for com.apple.watchdogd, the skipsetup seed contains the Setup preference keys, the clean seed contains neither
  * the watchdog and skipsetup seeds differ from the clean seed in a small number of 4 KiB pages only (a wholesale rewrite would be suspicious)
It then prints the kernel-config and payload lines to use. Exit status 1 on any failed check.
"""
import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

PACKER_SHA256 = 'efb5fac21ae94dcb1aecd47da2b73a7112b487e43692ac6396858552d27a0e48'
DEFAULT_PACKER = Path.home() / 'unixnu/UniXNU/drivers/DarwinBoot/Test/packed_ramdisk.py'
LOGICAL = 2 * 1024**3
MAX_PACKED = 8 * 1024 * 1024
PAGE = 4096
ok = True


def check(name, cond, detail=''):
    global ok
    ok &= bool(cond)
    print(('PASS ' if cond else 'FAIL ') + name + (f' : {detail}' if detail != '' else ''))
    return bool(cond)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_sums(path):
    sums = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            digest, _, name = line.partition('  ')
            sums[name.strip().lstrip('*')] = digest
    return sums


def load_packer(path):
    spec = importlib.util.spec_from_file_location('packed_ramdisk', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apfs_volumes(extents):
    """UUID/name of every APFS volume superblock (APSB) found in the extents (the offsets are byte offsets in the raw container)."""
    found, nxsb = {}, False
    for offset, data in extents:
        data = bytes(data)
        if offset == 0 and data[32:36] == b'NXSB':
            nxsb = True
        for at in range(0, len(data) - PAGE + 1, PAGE):
            if data[at + 32:at + 36] == b'APSB':
                u = str(uuid.UUID(bytes=data[at + 240:at + 256])).upper()
                name = data[at + 704:at + 960].split(b'\0')[0].decode('utf8', 'replace')
                found[u] = name
    return nxsb, found


def pages(extents):
    result = {}
    for offset, data in extents:
        data = bytes(data)
        for at in range(0, len(data), PAGE):
            result[offset + at] = hashlib.sha256(data[at:at + PAGE]).digest()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('artifact', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--packer', type=Path, default=DEFAULT_PACKER)
    args = ap.parse_args()
    if args.out.exists():
        sys.exit(f'STOP: {args.out} already exists; choose a new directory')
    enc = args.artifact / 'ram-seed.tar.gz.enc'
    public = args.artifact / 'seed-public.json'
    for p in (enc, public):
        if not p.is_file():
            sys.exit(f'STOP: {p} not found')
    outer = args.artifact / 'SHA256SUMS-encrypted'
    if outer.is_file():
        sums = read_sums(outer)
        for p in (enc, public):
            check(f'{p.name} matches SHA256SUMS-encrypted', sums.get(p.name) == sha256(p))
    passphrase = os.environ.get('ARTIFACT_PASSPHRASE') or getpass.getpass('ARTIFACT_PASSPHRASE: ')
    args.out.mkdir(parents=True)
    tar_path = args.out / 'ram-seed.tar.gz'
    result = subprocess.run(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000', '-pass', 'env:ARTIFACT_PASSPHRASE',
                             '-in', str(enc), '-out', str(tar_path)], env=dict(os.environ, ARTIFACT_PASSPHRASE=passphrase),
                            capture_output=True, text=True)
    passphrase = None
    if result.returncode:
        tar_path.unlink(missing_ok=True)
        sys.exit('STOP: decryption failed (wrong passphrase or a damaged file); nothing was extracted')
    try:
        with tarfile.open(tar_path) as archive:
            names = archive.getnames()
            if any(n.startswith('/') or '..' in Path(n).parts for n in names):
                sys.exit('STOP: unsafe member name in the archive; nothing extracted')
            archive.extractall(args.out)
    except tarfile.TarError as error:
        sys.exit(f'STOP: the decrypted data is not a valid archive ({error}); the passphrase is probably wrong')
    tar_path.unlink()
    print('decrypted to', args.out)
    inner = read_sums(args.out / 'SHA256SUMS')
    for name, digest in inner.items():
        check(f'unpacked {name} matches the inner SHA256SUMS', (args.out / name).is_file() and sha256(args.out / name) == digest)
    meta = json.loads((args.out / 'seed-public.json').read_text())
    check('decrypted seed-public.json equals the uploaded one', (args.out / 'seed-public.json').read_bytes() == public.read_bytes())
    want_uuid = str(uuid.UUID(meta['volume_uuid'])).upper()

    check('packer is the pinned one', args.packer.is_file() and sha256(args.packer) == PACKER_SHA256, str(args.packer))
    packer = load_packer(args.packer)
    extents_by = {}
    for name, info in meta['seeds'].items():
        path = args.out / info['file']
        blob = path.read_bytes()
        check(f'{name}: sha256 and size equal seed-public.json', hashlib.sha256(blob).hexdigest() == info['sha256'] and len(blob) == info['bytes'])
        check(f'{name}: packed size <= 8 MiB', len(blob) <= MAX_PACKED, f'{len(blob)} B')
        try:
            logical, extents = packer.regions(blob)
        except ValueError as error:
            check(f'{name}: valid DBRDISK1 packing', False, str(error))
            continue
        extents_by[name] = extents
        check(f'{name}: logical size is 2 GiB', logical == LOGICAL, logical)
        nxsb, volumes = apfs_volumes(extents)
        check(f'{name}: raw container starts with NXSB', nxsb)
        check(f'{name}: an APFS volume with UUID {want_uuid} exists', want_uuid in volumes, volumes)
        blob_all = b''.join(bytes(d) for _, d in extents)
        wd = b'com.apple.watchdogd' in blob_all
        setup = b'SetupFinishedAllSteps' in blob_all
        kind = info['kind']
        if kind == 'clean':
            check(f'{name}: (heuristic) no watchdog override text, no Setup preference keys', not setup and b'com.apple.watchdog</key>' not in blob_all)
        if kind in ('watchdog', 'skipsetup'):
            check(f'{name}: (heuristic) launchd override text for com.apple.watchdogd present', wd)
        if kind == 'skipsetup':
            check(f'{name}: (heuristic) Setup preference keys present', setup)
    if 'clean' in extents_by:
        base = pages(extents_by['clean'])
        for name in ('watchdog', 'skipsetup'):
            if name in extents_by:
                other = pages(extents_by[name])
                changed = sum(1 for k in set(base) | set(other) if base.get(k) != other.get(k))
                check(f'{name} differs from clean in only a few pages', changed < 2000, f'{changed} pages of 4 KiB')
    if ok:
        print('\nALL CHECKS PASSED. Next:')
    print(f'  kernel config (overlay): CONFIG_XNU_OWNER_DATA_MOUNT=y  CONFIG_XNU_OWNER_DATA_VOLUME_UUID="{want_uuid}"')
    print('  board.toml [payload.adt]: packed_data_ramdisk = true ; persistent_data = false')
    for name, info in meta['seeds'].items():
        print(f'  {name:9s} {args.out / info["file"]}  ({info["bytes"]} B, sha256 {info["sha256"][:16]}...)  ->  unixnu payload --data-ramdisk <that file>')
    (args.out / 'seed-verified.json').write_text(json.dumps(dict(volume_uuid=want_uuid, all_checks_passed=bool(ok)), indent=2) + '\n')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
