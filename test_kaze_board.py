"""観測の意味・鮮度・履歴の境界を検証する（ネットワーク不要）。"""
import datetime as dt
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import kaze_board as k
import launch_boards as launcher


def document(rows):
    return '<table><tr><th>日付</th><th>時刻</th><th>風向</th><th>風速</th></tr>' + ''.join(
        '<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows) + '</table>'


class WindTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 16, 13, 40, tzinfo=k.JST)

    def row(self, at=None, speed=2, direction="南南東", kind="normal"):
        return dict(at=(at or self.now).isoformat(), speed=speed, direction=direction,
                    degrees=k.DIRECTIONS.index(direction)*22.5 if direction else None, kind=kind)

    def view(self, rows, key="nagoya", now=None, history=None):
        return k.station_view(key, dict(rows=rows, updated=None, partial=False, failing=False),
                              history or rows, now or self.now)

    def test_sort_midnight_and_nested_units(self):
        parsed=k.parse_observations(document([
            ['2026/09/16','00:25','南南東','12 <span>m</span>'],
            ['2026/09/15','23:55','北','2 m'],
            ['2026/09/16','00:25','南南東','12 m']]))
        self.assertEqual([r['speed'] for r in parsed['rows']], [2,12])
        self.assertEqual(parsed['rows'][1]['degrees'],157.5)

    def test_calm_and_missing_are_distinct(self):
        result=k.parse_observations(document([
            ['2026/09/16','13:40','-','風弱く'],
            ['2026/09/16','13:25','&nbsp;','&nbsp;'],
            ['2026/09/16','13:10','---','欠測']]))
        self.assertEqual([r['kind'] for r in result['rows']], ['missing','missing','calm'])
        self.assertIsNone(result['rows'][-1]['speed'])
        self.assertTrue(result['partial'])
        v=self.view([result['rows'][-1]])
        self.assertFalse(v['unknown'] or v['stale'])
        self.assertIsNone(v['average'])

    def test_structural_change_does_not_become_calm(self):
        with self.assertRaises(ValueError):
            k.parse_observations('<html>maintenance</html>')

    def test_each_freshness_boundary(self):
        for key,normal,stale in [('nagoya',20,35),('yokkaichi',35,65)]:
            for age,caution,old in [(normal,False,False),(normal+.01,True,False),(stale,True,False),(stale+.01,True,True)]:
                v=self.view([self.row()],key,self.now+dt.timedelta(minutes=age))
                self.assertEqual((v['failing'],v['stale']),(caution,old))

    def test_missing_latest_is_unknown_even_if_fresh(self):
        v=self.view([self.row(speed=None,direction=None,kind='missing')])
        self.assertTrue(v['unknown'])
        self.assertIsNone(v['maximum'])

    def test_statistics_exclude_weak_and_missing(self):
        rows=[self.row(self.now-dt.timedelta(hours=7),speed=30),
              self.row(self.now-dt.timedelta(hours=1),speed=4),
              self.row(self.now-dt.timedelta(minutes=30),None,None,'calm'),self.row(speed=2)]
        v=self.view(rows)
        self.assertEqual(v['maximum']['speed'],4)
        self.assertEqual(v['average'],3)
        self.assertEqual(v['numeric_count'],2)

    def test_rose_denominator_and_actual_span(self):
        rows=[self.row(self.now-dt.timedelta(hours=6),4,'北'),
              self.row(self.now-dt.timedelta(minutes=15),None,None,'calm'),self.row(speed=12)]
        v=self.view(rows)
        self.assertEqual(v['rose']['count'],2)
        self.assertEqual(v['rose']['hours'],6)
        self.assertEqual(v['rose']['bins'][0],[50,0,0])
        self.assertEqual(v['rose']['bins'][7],[0,0,50])

    def test_history_dedup_trim_and_corrections(self):
        old=[self.row(self.now-dt.timedelta(hours=25)),self.row(speed=2)]
        merged=k.merge_history(old,[self.row(speed=4)],self.now)
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['speed'],4)

    def test_failure_retains_last_observation(self):
        previous=dict(rows=[self.row()],updated=self.now.isoformat(),failing=False,partial=False)
        with patch.dict(k.STATE,nagoya=previous.copy()), patch.dict(k.SOURCES,nagoya=lambda: (_ for _ in ()).throw(OSError('offline'))):
            k.update_source('nagoya')
            self.assertEqual(k.STATE['nagoya']['rows'],previous['rows'])
            self.assertTrue(k.STATE['nagoya']['failing'])

    def test_history_persistence_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'history.json')
            with patch.dict(k.HISTORY,nagoya=[self.row()]), patch.dict(k.STATE), patch.object(k,'now_jst',return_value=self.now):
                k.persist_history(path)
                self.assertEqual(json.loads(Path(path).read_text(encoding='utf-8'))['nagoya'][0]['speed'],2)
                self.assertFalse(Path(path+'.tmp').exists())
                k.load_history(path)
                self.assertEqual(k.STATE['nagoya']['rows'][-1]['speed'],2)
                Path(path).write_text('{broken',encoding='utf-8')
                k.load_history(path)

    def test_beaufort_boundaries(self):
        for speed,force in [(0,0),(.3,1),(1.6,2),(5.5,4),(13.9,7),(17.2,8),(32.7,12)]:
            self.assertEqual(k.beaufort(speed),force)

    def test_old_response_retains_previous_rows(self):
        previous=dict(rows=[self.row()],updated=None,failing=False,partial=False)
        older=dict(rows=[self.row(self.now-dt.timedelta(minutes=15))],partial=False)
        with patch.dict(k.STATE,nagoya=previous.copy()), patch.dict(k.SOURCES,nagoya=lambda:older), patch.object(k,'now_jst',return_value=self.now):
            k.update_source('nagoya')
            self.assertEqual(k.STATE['nagoya']['rows'],previous['rows'])
            self.assertTrue(k.STATE['nagoya']['failing'])

    def test_storage_failure_does_not_discard_live_data(self):
        fresh=dict(rows=[self.row()],partial=False)
        with patch.dict(k.STATE), patch.dict(k.HISTORY), patch.object(k,'HISTORY_ERROR',False), patch.dict(k.SOURCES,nagoya=lambda:fresh), patch.object(k,'now_jst',return_value=self.now), patch.object(k,'persist_history',side_effect=OSError('disk full')):
            k.update_source('nagoya')
            self.assertEqual(k.STATE['nagoya']['rows'],fresh['rows'])
            self.assertFalse(k.STATE['nagoya']['failing'])
            self.assertTrue(k.HISTORY_ERROR)

    def test_rose_expires_while_source_is_down(self):
        row=self.row(self.now-dt.timedelta(hours=25))
        v=self.view([row])
        self.assertEqual(v['rose']['count'],0)
        self.assertEqual(v['rose']['hours'],0)
        self.assertEqual(v['latest']['speed'],2)
        self.assertTrue(v['stale'])

    def test_wind_log_rotation_preserves_open_bousai_log_and_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            board_path=Path(folder)/'board.log'
            original={board_path:'current log', **{Path(str(board_path)+'.'+str(i)):'backup '+str(i) for i in range(1,4)}}
            for path,content in original.items():
                path.write_text(content,encoding='utf-8')
            bousai=logging.handlers.RotatingFileHandler(board_path,maxBytes=40,backupCount=3,encoding='utf-8')
            handler=k.wind_log_handler(folder)
            handler.maxBytes=40
            try:
                for _ in range(8):
                    handler.emit(logging.LogRecord('wind',logging.INFO,'',0,'observation updated',(),None))
                for path,content in original.items():
                    self.assertEqual(path.read_text(encoding='utf-8'),content)
                self.assertTrue((Path(folder)/'kaze_board.log.3').exists())
                self.assertIn('observation updated',(Path(folder)/'kaze_board.log').read_text(encoding='utf-8'))
            finally:
                handler.close()
                bousai.close()

    def test_rose_missing_rows_and_absent_rows_are_counted_but_calm_is_not(self):
        for middle,expected in [(self.row(self.now-dt.timedelta(minutes=15),None,None,'missing'),1),
                                (self.row(self.now-dt.timedelta(minutes=15),2,None),1),
                                (self.row(self.now-dt.timedelta(minutes=15),None,None,'calm'),0),
                                (None,1)]:
            with self.subTest(middle=middle):
                rows=[self.row(self.now-dt.timedelta(minutes=30))]
                if middle:
                    rows.append(middle)
                rows.append(self.row())
                rose=self.view(rows)['rose']
                self.assertEqual(rose['expected'],3)
                self.assertEqual(rose['missing_count'],expected)
                self.assertEqual(rose['count'],2)

    def test_launcher_waits_for_correct_server_before_opening(self):
        from unittest.mock import MagicMock
        response=MagicMock()
        response.__enter__.return_value=response
        response.read.return_value=b'{"board":"wind"}'
        with patch.object(launcher.urllib.request,'urlopen',side_effect=[OSError('not ready'),response]), patch.object(launcher.time,'sleep') as sleep, patch.object(launcher.subprocess,'Popen') as spawn:
            launcher.wait_and_open('msedge.exe','wind',8766,'127.0.0.1',[-1920,0])
            sleep.assert_called_once_with(1)
            spawn.assert_called_once()
            self.assertIn('--window-position=-1920,0',spawn.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
