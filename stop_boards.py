"""Windows用。パスで所属を確認したボードのプロセスツリーだけを終了する。"""
import ctypes
from ctypes import wintypes
import json
import ntpath
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PYTHON_NAMES = {'python.exe', 'pythonw.exe', 'py.exe', 'pyw.exe'}
SCRIPTS = {'launch_boards.py', 'bousai_board.py', 'kaze_board.py'}
BATCHES = {'start_board.bat', 'run_server.bat'}


def command_args(command):
    """引用符・空白を含むWindowsのコマンドラインを引数に戻す。"""
    if not command:
        return []
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def processes():
    # 固定コマンド。プロジェクトパスをPowerShellコードへ埋め込まない。
    result = subprocess.run([
        'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        'ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process | '
        'Select-Object ProcessId,ParentProcessId,Name,CommandLine)'
    ], capture_output=True, encoding='utf-8-sig', check=True, creationflags=HIDDEN)
    return json.loads(result.stdout)


def norm(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def script_argument(process):
    args = command_args(process.get('CommandLine'))
    name = (process.get('Name') or '').lower()
    if name in PYTHON_NAMES:
        for arg in args[1:]:
            # コード文字列やモジュール名の中にパスがあっても対象にしない。
            if arg in ('-c', '-m', '--'):
                return None
            if not arg.startswith('-'):
                return arg if ntpath.basename(arg).lower() in SCRIPTS else None
    if name == 'cmd.exe':
        # 対話シェルは終了しない。バッチ1個を実行する専用cmdだけを対象にする。
        for i, arg in enumerate(args[1:], 1):
            if arg.lower() == '/c':
                tail = args[i + 1:]
                if len(tail) == 1 and ntpath.basename(tail[0]).lower() in BATCHES:
                    return tail[0]
                return None
    return None


def board_roots(snapshot, directory=HERE):
    scripts = {norm(ntpath.join(str(directory), s)) for s in SCRIPTS | BATCHES}
    profiles = {norm(ntpath.join(str(directory), p)) for p in ('.edge-bousai', '.edge-wind')}
    selected = {}
    script_args = {}
    by_pid = {p['ProcessId']: p for p in snapshot}
    for process in snapshot:
        pid = process['ProcessId']
        script = script_argument(process)
        if script:
            script_args[pid] = script
            if ntpath.isabs(script) and norm(script) in scripts:
                selected[pid] = process
        if (process.get('Name') or '').lower() == 'msedge.exe':
            args = command_args(process.get('CommandLine'))
            for i, arg in enumerate(args[1:], 1):
                profile = arg.partition('=')[2] if arg.startswith('--user-data-dir=') else (
                    args[i + 1] if arg == '--user-data-dir' and i + 1 < len(args) else None)
                if profile and ntpath.isabs(profile) and norm(profile) in profiles:
                    selected[pid] = process
    # 旧start_board.batの相対パスのランチャーも、所属確認済みの子がある場合だけ含める。
    changed = True
    while changed:
        changed = False
        for process in list(selected.values()):
            parent = process['ParentProcessId']
            script = script_args.get(parent)
            if (parent not in selected and script and not ntpath.isabs(script)
                    and norm(script) in SCRIPTS | BATCHES):
                selected[parent] = by_pid[parent]
                changed = True
    # 親を /T で止めれば子も止まる。監視側を残してサーバーだけ終了しない。
    return [p for pid, p in selected.items() if p['ParentProcessId'] not in selected]


def stop(directory=HERE):
    for _ in range(3):
        roots = board_roots(processes(), directory)
        if not roots:
            print('Board servers and dedicated Edge windows are stopped.')
            return 0
        for process in roots:
            pid = process['ProcessId']
            # 終了直前にも所属を再確認。終了済みPIDの再利用で他のアプリを巻き込まない。
            current = next((p for p in board_roots(processes(), directory)
                            if p['ProcessId'] == pid and p['CommandLine'] == process['CommandLine']), None)
            if current is None:
                continue
            result = subprocess.run(['taskkill.exe', '/PID', str(pid), '/T', '/F'],
                                    capture_output=True, creationflags=HIDDEN)
            print(f"Stop {process['Name']} PID {pid}: {'OK' if result.returncode == 0 else 'retry'}")
        time.sleep(.2)
    remaining = board_roots(processes(), directory)
    if remaining:
        print('Could not stop all board processes. Run with the same permissions as the boards.', file=sys.stderr)
        return 1
    print('Board servers and dedicated Edge windows are stopped.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(stop())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Could not inspect or stop board processes: {exc}', file=sys.stderr)
        sys.exit(1)
