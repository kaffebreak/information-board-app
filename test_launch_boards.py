"""起動対象の選択を検証する。実際のサーバーやブラウザは起動しない。"""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import launch_boards as launcher


class LauncherTests(unittest.TestCase):
    def launch(self, cfg, running=False):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{}'
        with patch.object(launcher.sys, 'argv', ['launch_boards.py']), \
             patch.object(launcher, 'config', **(
                 {'side_effect': cfg} if isinstance(cfg, Exception) else {'return_value': cfg})), \
             patch.object(launcher, 'edge_path', return_value='msedge.exe'), \
             patch.object(launcher.urllib.request, 'urlopen', **(
                 {'return_value': response} if running else {'side_effect': OSError('not running')})) as probe, \
             patch.object(launcher.subprocess, 'Popen') as spawn, \
             patch.object(launcher.threading, 'Thread') as thread:
            self.code = launcher.main()
        return probe, spawn, thread

    def test_disabled_wind_skips_its_server_browser_and_probe(self):
        probe, spawn, thread = self.launch({'enable_wind': False})
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0], 'http://127.0.0.1:8765/api/config')
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][-2:], ['--server', 'bousai'])
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs['args'][1:3], ('bousai', 8765))

    def test_enabled_and_unspecified_wind_launch_both_boards(self):
        for cfg in [{}, {'enable_wind': True}]:
            with self.subTest(cfg=cfg):
                probe, spawn, thread = self.launch(cfg)
                self.assertEqual(probe.call_count, 2)
                self.assertEqual([c.args[0][-1] for c in spawn.call_args_list], ['bousai', 'wind'])
                self.assertEqual([c.kwargs['args'][1] for c in thread.call_args_list], ['bousai', 'wind'])

    def test_disabled_wind_does_not_validate_unused_settings(self):
        for port in [8765, 'unused']:
            with self.subTest(port=port):
                self.launch({'enable_wind': False, 'wind_port': port, 'wind_window_position': 'unused'})

    def test_enabled_wind_rejects_conflicting_ports(self):
        with self.assertRaisesRegex(ValueError, 'wind_port'):
            self.launch({'enable_wind': True, 'wind_port': 8765})

    def test_rejects_non_boolean_setting(self):
        for value in ['false', 'true', 0, 1, None]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'enable_wind'):
                self.launch({'enable_wind': value})

    def test_existing_server_is_reused(self):
        probe, spawn, thread = self.launch({'enable_wind': False}, running=True)
        spawn.assert_not_called()
        thread.assert_called_once()
        self.assertEqual(self.code, 0)
    def test_broken_config_opens_only_bousai_and_reports_it(self):
        with patch.object(launcher.sys, 'stderr', new_callable=io.StringIO) as err:
            probe, spawn, thread = self.launch(launcher.ConfigError('config.json の書き方に誤りがあります'))
        self.assertEqual(self.code, 1)
        self.assertEqual([c.args[0][-1] for c in spawn.call_args_list], ['bousai'])
        self.assertEqual([c.kwargs['args'][1] for c in thread.call_args_list], ['bousai'])
        self.assertIn('書き方に誤り', err.getvalue())

    def test_config_file_states(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'HERE', Path(folder)):
            self.assertEqual(launcher.config(), {})          # 無ければ既定値で起動する
            path = Path(folder) / 'config.json'
            for text in ['{broken', '[1, 2]']:
                path.write_text(text, encoding='utf-8')
                with self.subTest(text=text), self.assertRaises(launcher.ConfigError):
                    launcher.config()
            path.write_text('﻿{"enable_wind": false}', encoding='utf-8')
            self.assertEqual(launcher.config(), {'enable_wind': False})


if __name__ == '__main__':
    unittest.main()
