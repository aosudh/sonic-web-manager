#!/usr/bin/env python3
"""Persistent, fixed-purpose cold-breakout worker; never accepts commands or paths."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = Path('/host/sonic-web/state/data/breakout')
ACTIVE = frozenset({'queued', 'applying', 'pending_reboot', 'verifying', 'rollback_applying', 'rollback_pending_reboot'})
ID_PATTERN = re.compile(r'[0-9a-f]{32}')


def checked_directory(identifier, base=BASE):
    if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
        raise ValueError('Invalid breakout operation identifier')
    if base.is_symlink() or base.resolve() != base or not base.is_dir():
        raise ValueError('Unsafe breakout state directory')
    directory = base / identifier
    if directory.is_symlink() or directory.resolve() != directory or not directory.is_dir():
        raise ValueError('Invalid breakout operation directory')
    if os.name == 'posix' and (base.stat().st_uid != 0 or directory.stat().st_uid != 0):
        raise ValueError('Breakout state must be owned by root')
    return directory


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Unsafe breakout state file')
    return json.loads(path.read_text(encoding='utf-8'))


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def status(directory, state, message, *, verified=False, **extra):
    record = {'id': directory.name, 'status': state, 'message': message,
              'verified': verified, 'updated_at': datetime.now(timezone.utc).isoformat(), **extra}
    destination = directory / 'status.json'
    temporary = directory / 'status.new'
    if destination.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise ValueError('Unsafe operation status path')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        if hasattr(os, 'O_DIRECTORY'):
            directory_fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return record


@contextmanager
def exclusive(base=BASE):
    import fcntl
    fd = os.open(base / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def apply_operation(directory):
    from server import port_breakout
    before = read_json(directory / 'status.json')
    if before.get('status') != 'queued':
        raise ValueError('Operation is not queued; refusing to repeat a hardware write')
    plan = read_json(directory / 'plan.json')
    status(directory, 'applying', '正在备份并写入拆分启动配置；业务将在冷重启时中断。')
    try:
        port_breakout.stage(plan, directory)
    except Exception as error:
        status(directory, 'failed', '拆分配置准备失败，未发起重启：' + str(error)[:1500])
        raise
    status(directory, 'pending_reboot', '启动配置已准备，正在重启交换机；请等待设备恢复后重新登录。', boot_id_before=boot_id())
    # Permit the Web response to reach the browser before SONiC stops services.
    time.sleep(5)
    try:
        result = subprocess.run(['/usr/local/bin/reboot'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=180, check=False)
        if result.returncode:
            raise RuntimeError('SONiC reboot returned ' + str(result.returncode))
    except (FileNotFoundError, PermissionError) as error:
        # exec never started; file restoration is safe on this unchanged boot.
        restored = False
        try:
            result = port_breakout.rollback(directory)
            restored = result.get('success') is True and result.get('requires_reboot') is False
        except Exception:
            pass
        status(directory, 'rolled_back' if restored else 'failed', '重启请求失败；' + ('已恢复原启动文件。' if restored else '启动文件恢复未确认，请检查任务备份。')
               + str(error)[:500], files_restored=restored)
        raise
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        # Timeout/nonzero exit may follow a queued reboot. Do not race shutdown
        # by restoring boot files. The next boot verifier or explicit recovery
        # resolves this uncertain state.
        status(directory, 'pending_reboot', '重启命令结果尚未确认；已保留启动配置及备份，禁止继续配置，请等待启动或检查后恢复。'
               + str(error)[:500], boot_id_before=boot_id(), reboot_result_unknown=True)
        raise


def verify_operation(directory):
    from server import port_breakout
    previous = read_json(directory / 'status.json')
    if previous.get('status') not in ACTIVE:
        return previous
    current_boot = boot_id()
    if previous.get('status') in {'pending_reboot', 'rollback_pending_reboot'} and previous.get('boot_id_before') == current_boot:
        return previous
    status(directory, 'verifying', '设备已启动，正在核对拆分配置及 ASIC 端口。')
    deadline = time.monotonic() + 420
    last_error = '尚未获得硬件验证结果'
    while time.monotonic() < deadline:
        try:
            result = port_breakout.verify(directory)
            if result.get('verified') is True:
                restored = result.get('restored') is True
                return status(directory, 'rolled_back' if restored else 'succeeded',
                              '原端口配置已恢复并通过 ASIC 验证。' if restored else '拆分模式已通过启动配置、运行配置和 ASIC 端口验证。',
                              verified=True, verification=result)
            last_error = str(result.get('message') or result.get('errors') or result)[:1500]
        except Exception as error:
            last_error = str(error)[:1500]
        time.sleep(15)
    return status(directory, 'failed', '重启后验证未通过；已保留原配置备份，请检查后恢复。' + last_error)


def rollback_operation(directory):
    """Explicit local administrator recovery; never selected by browser input."""
    from server import port_breakout
    previous = read_json(directory / 'status.json')
    if previous.get('status') in {'queued', 'applying', 'verifying', 'rolled_back'}:
        raise ValueError('This operation is not eligible for explicit rollback')
    status(directory, 'rollback_applying', '正在恢复已校验的原端口配置；其他配置写入暂停。')
    try:
        result = port_breakout.rollback(directory)
    except Exception as error:
        status(directory, 'failed', '恢复文件未完成，继续禁止配置写入：' + str(error)[:1500])
        raise
    if result.get('success') is not True:
        raise RuntimeError('Rollback did not confirm file restoration')
    if result.get('requires_reboot') is not True:
        return status(directory, 'rolled_back', '已恢复原启动文件，当前运行配置保持原样。', files_restored=True)
    status(directory, 'rollback_pending_reboot', '已准备原端口配置，正在重启以完成恢复。', boot_id_before=boot_id())
    subprocess.run(['/usr/local/bin/reboot'], stdin=subprocess.DEVNULL, capture_output=True,
                   text=True, timeout=180, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument('--operation')
    command.add_argument('--rollback-operation')
    command.add_argument('--verify-pending', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('This worker must run as root on SONiC')
    os.umask(0o077)
    if args.verify_pending and not BASE.exists():
        return
    if BASE.is_symlink() or BASE.resolve() != BASE or not BASE.is_dir():
        raise ValueError('Unsafe breakout state directory')
    with exclusive():
        if args.operation:
            apply_operation(checked_directory(args.operation))
        elif args.rollback_operation:
            rollback_operation(checked_directory(args.rollback_operation))
        else:
            for directory in sorted(BASE.iterdir()):
                if ID_PATTERN.fullmatch(directory.name):
                    checked_directory(directory.name)
                    if (directory / 'status.json').exists():
                        verify_operation(directory)


if __name__ == '__main__':
    main()
