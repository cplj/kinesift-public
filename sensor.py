"""Linux sidecar sensor, supplied by the host; never imported by the payload."""
import ctypes
import json
import os
from pathlib import Path
import struct
import sys
import time


def main():
    root = Path('/proc') / sys.argv[1] / 'root'
    watches = json.loads(sys.argv[2])
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'inotify_init1 failed')
    paths = {}

    def emit(kind, **fields):
        print(json.dumps(dict(kind=kind, mono_ns=time.monotonic_ns(), **fields)), flush=True)

    def watch(path):
        if path.is_symlink():
            return
        if len(paths) >= 256:
            raise RuntimeError('watch directory limit reached')
        wd = libc.inotify_add_watch(fd, os.fsencode(path), 0x00000FFF)
        if wd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_add_watch failed')
        paths[wd] = str(path)

    for name in watches:
        base = root / name.lstrip('/')
        if base.is_symlink() or not base.is_dir():
            raise ValueError('watch root must be an existing non-symlink directory')
        watch(base)
        for parent, dirs, _ in os.walk(base, followlinks=False):
            for name in dirs:
                watch(Path(parent) / name)
    emit('ready', watched_directories=len(paths))
    previous = None
    previous_net = None
    last = start = time.monotonic()
    ticks, max_gap = 0, 0
    while True:
        if not (root / 'proc').exists():
            emit('target_exited')
            break
        now = time.monotonic()
        max_gap = max(max_gap, now - last)
        last = now
        ticks += 1
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            data = b''
        offset = 0
        while offset < len(data):
            wd, mask, cookie, size = struct.unpack_from('iIII', data, offset)
            name = os.fsdecode(data[offset + 16:offset + 16 + size].split(b'\0')[0])
            offset += 16 + size
            if mask & (0x4000 | 0x8000 | 0x400 | 0x800):
                emit('source_error', source='inotify', mask=mask)
            path = paths.get(wd, '') + ('/' + name if name else '')
            relative = '/' + str(Path(path).relative_to(root)) if path else None
            emit('filesystem', path=relative, mask=mask, cookie=cookie)
            if mask & 0x40000000 and mask & (0x100 | 0x80):
                try:
                    watch(Path(path))
                except FileNotFoundError:
                    emit('source_error', source='new_directory_disappeared')
        try:
            processes = []
            for pid in (root / 'proc').iterdir():
                if pid.name.isdigit():
                    try:
                        processes.append((pid.name, (pid / 'comm').read_text().strip()))
                    except (FileNotFoundError, ProcessLookupError):
                        continue
            processes.sort()
            if processes != previous:
                emit('processes', processes=processes)
                previous = processes
        except OSError as exc:
            if not (root / 'proc').exists():
                emit('target_exited')
                break
            emit('source_error', source='proc', error=str(exc))
        if now - start >= .5:
            try:
                net = {}
                for line in (Path('/proc') / sys.argv[1] / 'net/dev').read_text().splitlines()[2:]:
                    interface, counters = line.split(':', 1)
                    values = counters.split()
                    net[interface.strip()] = dict(rx_bytes=int(values[0]), tx_bytes=int(values[8]))
                if net != previous_net:
                    emit('network_counters', interfaces=net, previous=previous_net)
                    previous_net = net
            except OSError as exc:
                if (root / 'proc').exists():
                    emit('source_error', source='network', error=str(exc))
            emit('cadence', hz=ticks / (now - start), max_gap_ms=max_gap * 1000)
            start, ticks, max_gap = now, 0, 0
        time.sleep(.01)


if __name__ == '__main__':
    main()
