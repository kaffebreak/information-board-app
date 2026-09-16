"""終了対象の分離と、監視プロセスを含む終了をWindows上で検証する。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import stop_boards as stop


@unittest.skipUnless(os.name == 'nt', 'Windows専用')
class StopTests(unittest.TestCase):
    directory = r'C:\Board Files\app'

    def process(self, pid, name, args, parent=0):
        return dict(ProcessId=pid, ParentProcessId=parent, Name=name,
                    CommandLine=subprocess.list2cmdline([name, *args]))

    def test_only_exact_project_scripts_and_profiles_are_selected(self):
        snapshot = [
            self.process(1, 'python.exe', [self.directory + r'\launch_boards.py', '--server', 'wind']),
            self.process(2, 'python.exe', [self.directory + r'\kaze_board.py'], parent=1),
            self.process(3, 'msedge.exe', ['--user-data-dir=' + self.directory + r'\.edge-wind']),
            self.process(4, 'python.exe', [r'C:\other\bousai_board.py']),
            self.process(5, 'python.exe', ['-c', 'print(' + repr(self.directory + r'\kaze_board.py') + ')']),
            self.process(6, 'msedge.exe', ['--user-data-dir=' + self.directory + r'\.edge-wind-other']),
            self.process(7, 'msedge.exe', ['--profile-directory=Default']),
            self.process(8, 'python.exe', ['bousai_board.py']),
            self.process(9, 'python.exe', [self.directory + r'\kaze_board.py.bak']),
            self.process(10, 'python.exe', ['-u', self.directory + r'\bousai_board.py']),
            self.process(11, 'msedge.exe', ['--user-data-dir', self.directory + r'\.edge-bousai']),
        ]
        self.assertEqual({p['ProcessId'] for p in stop.board_roots(snapshot, self.directory)}, {1, 3, 10, 11})

    def test_relative_launcher_is_owned_only_via_confirmed_child(self):
        snapshot = [self.process(1, 'python.exe', ['launch_boards.py']),
                    self.process(2, 'python.exe', [self.directory + r'\launch_boards.py', '--server', 'wind'], parent=1),
                    self.process(3, 'python.exe', ['launch_boards.py'])]
        self.assertEqual([p['ProcessId'] for p in stop.board_roots(snapshot, self.directory)], [1])

    def test_interactive_shell_and_code_mentions_are_not_owned(self):
        snapshot = [self.process(1, 'cmd.exe', ['/k', self.directory + r'\start_board.bat']),
                    self.process(2, 'cmd.exe', ['/c', 'echo', self.directory + r'\run_server.bat']),
                    self.process(3, 'cmd.exe', ['/c', self.directory + r'\run_server.bat'])]
        self.assertEqual([p['ProcessId'] for p in stop.board_roots(snapshot, self.directory)], [3])

    def test_no_process_is_success_and_does_not_call_taskkill(self):
        with patch.object(stop, 'processes', return_value=[]), patch.object(stop.subprocess, 'run') as run:
            self.assertEqual(stop.stop(self.directory), 0)
            run.assert_not_called()

    def test_changed_pid_is_not_terminated(self):
        owned = self.process(1, 'python.exe', [self.directory + r'\launch_boards.py'])
        unrelated = self.process(1, 'python.exe', [r'C:\other\launch_boards.py'])
        with patch.object(stop, 'processes', side_effect=[[owned], [unrelated], [unrelated]]), \
             patch.object(stop.subprocess, 'run') as run, patch.object(stop.time, 'sleep'):
            self.assertEqual(stop.stop(self.directory), 0)
            run.assert_not_called()

    def test_persistent_stop_failure_is_reported(self):
        owned = self.process(1, 'python.exe', [self.directory + r'\launch_boards.py'])
        with patch.object(stop, 'processes', return_value=[owned]), \
             patch.object(stop.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(stop.time, 'sleep'):
            self.assertEqual(stop.stop(self.directory), 1)

    def test_real_supervisor_tree_stops_and_unrelated_python_survives(self):
        with tempfile.TemporaryDirectory(prefix='board stop test ') as folder:
            root = Path(folder)
            child_path = root / 'bousai_board.py'
            child_path.write_text('import time\ntime.sleep(120)\n', encoding='utf-8')
            (root / 'unrelated.py').write_text(child_path.read_text(encoding='utf-8'), encoding='utf-8')
            (root / 'launch_boards.py').write_text(
                'import pathlib, subprocess, sys, time\n'
                'root=pathlib.Path(__file__).parent\n'
                'while True:\n'
                '    child=subprocess.Popen([sys.executable,str(root/"bousai_board.py")],creationflags=subprocess.CREATE_NO_WINDOW)\n'
                '    (root/"child.pid").write_text(str(child.pid))\n'
                '    child.wait()\n'
                '    time.sleep(.1)\n', encoding='utf-8')
            supervisor = subprocess.Popen([sys.executable, str(root/'launch_boards.py')], creationflags=stop.HIDDEN)
            unrelated = subprocess.Popen([sys.executable, str(root/'unrelated.py')], creationflags=stop.HIDDEN)
            try:
                deadline = time.monotonic() + 10
                while not (root/'child.pid').exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((root/'child.pid').exists())
                child_pid = int((root/'child.pid').read_text())
                self.assertEqual(stop.stop(root), 0)
                supervisor.wait(timeout=5)
                self.assertIsNone(unrelated.poll())
                self.assertNotIn(child_pid, {p['ProcessId'] for p in stop.processes()})
                self.assertEqual(stop.stop(root), 0)
            finally:
                if supervisor.poll() is None:
                    subprocess.run(['taskkill.exe', '/PID', str(supervisor.pid), '/T', '/F'],
                                   capture_output=True, creationflags=stop.HIDDEN)
                    supervisor.wait(timeout=5)
                unrelated.terminate()
                unrelated.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
