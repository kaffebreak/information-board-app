"""画面の回帰確認。開発時のみ Playwright と Chromium/Edge を使用（外部通信なし）。"""
from pathlib import Path
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "画面テストには Playwright が必要です")
class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1920, "height": 1080})
        self.page.route("**/*", lambda route: route.abort())

    def tearDown(self):
        self.page.close()

    def load(self, name):
        self.page.set_content(Path(__file__).with_name(name).read_text(encoding="utf-8"))

    def test_long_rail_details_preserve_all_statuses_and_subway_font(self):
        self.load("bousai_board.html")
        for width, height in [(1920, 1080), (1280, 720)]:
            self.page.set_viewport_size({"width": width, "height": height})
            for alarm in [False, True]:
                for long_details in ["jr", "all"]:
                    with self.subTest(width=width, alarm=alarm, details=long_details):
                        result = self.page.evaluate("""({alarm, longDetails}) => {
                          document.body.classList.toggle('alarm', alarm);
                          const line = (name, code) => ({name,code,color:'#888',level:'stop',status:'運転見合わせ',detail:''});
                          const rail = {
                            jr:{lines:[{...line('東海道線','CA'),sub:'豊橋～米原'},line('武豊線','CE')]},
                            subway:{lines:[line('名城線・名港線','M')]},
                            meitetsu:{lines:[line('名古屋本線','NH'),line('河和線','KC'),line('常滑線','TA')]},
                            aonami:line('あおなみ線','')
                          };
                          renderRail(rail);
                          const measure = () => {
                            const box=document.getElementById('rail').getBoundingClientRect();
                            return {font:getComputedStyle(document.querySelectorAll('#rail .row .st')[2]).fontSize,
                              states:[...document.querySelectorAll('#rail .row .st')].map(el=>{
                                const r=el.getBoundingClientRect();
                                return {text:el.textContent,visible:r.height>0 && r.top>=box.top && r.bottom<=box.bottom+1 && r.left>=box.left && r.right<=box.right+1};
                              }), more:document.querySelectorAll('#rail .more').length};
                          };
                          const baseline=measure();
                          const text='地震の影響により運転を見合わせています。安全確認後に再開予定です。'.repeat(200);
                          rail.jr.lines[0].detail=text;
                          if(longDetails==='all'){
                            [rail.jr,rail.subway,rail.meitetsu].forEach(g=>g.lines.forEach(l=>l.detail=text));
                            rail.aonami.detail=text;
                          }
                          renderRail(rail);
                          const long=measure();
                          rail.jr.lines.forEach(l=>l.detail='短い詳細');
                          renderRail(rail);
                          return {baseline,long,restored:document.querySelector('#rail .dt').getBoundingClientRect().height>0};
                        }""", {"alarm": alarm, "longDetails": long_details})
                        self.assertEqual(len(result['long']['states']), 7)
                        self.assertTrue(all(s['visible'] for s in result['long']['states']), result)
                        self.assertEqual(result['baseline']['font'], result['long']['font'])
                        self.assertEqual(result['long']['more'], 0)
                        self.assertTrue(result['restored'])

    def test_rose_missing_note_includes_missing_rows_but_not_calm(self):
        self.load("kaze_board.html")
        for missing in [0, 1]:
            result = self.page.evaluate("""missing => {
              const s=demoStatus('normal').stations[0];
              s.rose.missing_count=missing;
              return panel(s,10).includes('・欠測あり');
            }""", missing)
            self.assertEqual(result, bool(missing))
    def test_missing_rail_and_road_data_is_not_shown_as_normal(self):
        self.load("bousai_board.html")
        result = self.page.evaluate("""() => {
          const line = (name, level, status) => ({name, code:'X', color:'#888', level, status, detail:'詳細'});
          const fresh = {updated:new Date().toISOString(), stale:false, failing:false};
          const old = {updated:new Date(Date.now() - 3600e3).toISOString(), stale:true, failing:true};
          const never = {updated:null, stale:true, failing:false};
          const broken = {updated:null, stale:true, failing:true};
          renderRail({jr:{lines:[line('東海道線','normal','平常運転'), line('武豊線','stop','運転見合わせ')], src:old},
                      subway:{lines:[], src:never}, meitetsu:{lines:[], src:broken},
                      aonami:{level:'normal', status:'平常運転', src:fresh}});
          const dot = el => getComputedStyle(el, '::before').backgroundColor;
          return {
            rows:[...document.querySelectorAll('#rail .row')].map(r => [r.classList.contains('unknown') ? 'unknown' : r.classList[1],
                                                                       r.querySelector('.st').textContent]),
            empties:[...document.querySelectorAll('#rail .empty')].map(e => [e.classList.contains('pending'), e.textContent]),
            flags:[...document.querySelectorAll('#rail .gh')].map(g => g.querySelector('.flag')?.textContent || ''),
            green:[...document.querySelectorAll('#rail .empty')].some(e => dot(e) === 'rgb(70, 185, 140)'),
            road:[never, broken, old, fresh].map(src => {
              const box = document.createElement('div');
              box.innerHTML = itemsHtml([], '情報はありません', src);
              return [box.firstElementChild.classList.contains('pending'), box.textContent];
            })
          };
        }""")
        self.assertEqual(result["rows"], [["unknown", "判定できません"], ["stop", "運転見合わせ"], ["normal", "平常運転"]])
        self.assertEqual(result["empties"], [[True, "取得中"], [True, "判定できません"]])
        self.assertTrue(result["flags"][0].startswith("取得できていません（最終"))
        self.assertEqual(result["flags"][1:], ["取得待ち", "取得できていません", ""])
        self.assertFalse(result["green"])
        self.assertEqual(result["road"], [[True, "取得中"], [True, "判定できません"], [True, "判定できません"],
                                          [False, "情報はありません"]])

    def test_wind_offline_banner_waits_like_bousai_board(self):
        self.load("kaze_board.html")
        result = self.page.evaluate("""async () => {
          await new Promise(resolve => setTimeout(resolve, 200));   // 最初の取得が失敗するのを待つ
          const afterOneFailure = document.body.classList.contains('offline');
          lastSuccess = Date.now() - 61000;
          render(lastStatus);
          return {afterOneFailure, afterAMinute: document.body.classList.contains('offline')};
        }""")
        self.assertEqual(result, {"afterOneFailure": False, "afterAMinute": True})
    def wind(self, script):
        self.load("kaze_board.html")
        self.page.wait_for_timeout(200)   # 最初の取得失敗による描画を待ってから差し替える
        return self.page.evaluate(script)

    def test_wind_states_are_labelled_in_words(self):
        result = self.wind("""() => {
          const view = s => {
            const box = document.createElement('div');
            box.innerHTML = panel(s, 10);
            const st = box.firstElementChild, text = sel => st.querySelector(sel)?.textContent || '';
            return {cls: st.className.split(' ').filter(Boolean), badge: text('.badge'), retry: text('.retry'),
                    dir: text('.direction'), speed: text('.speed'), secondary: text('.secondary'),
                    maxOver: st.querySelector('.stat').classList.contains('over'),
                    ghosts: [...st.querySelectorAll('.ghost')].map(g => +g.getAttribute('opacity')),
                    needle: !!st.querySelector('.needle')};
          };
          const none = {...demoStatus('normal').stations[1], latest: null, observed_at: null, stale: true,
                        unknown: true, series: [], maximum: null, average: null, numeric_count: 0};
          return {calm: view(demoStatus('normal').stations[0]), normal: view(demoStatus('normal').stations[1]),
                  high: view(demoStatus('high').stations[1]), stale: view(demoStatus('1').stations[1]),
                  retry: view(demoStatus('retry').stations[0]), missing: view(demoStatus('retry').stations[1]),
                  none: view(none)};
        }""")
        calm = result["calm"]
        self.assertEqual((calm["dir"], calm["speed"], calm["badge"], calm["needle"]), ("なし", "風弱く", "", False))
        self.assertEqual(len(set(calm["ghosts"])), 1)          # 針に見える濃い線を作らない
        self.assertLessEqual(max(calm["ghosts"]), .12)
        normal = result["normal"]
        self.assertEqual((normal["speed"], normal["secondary"], normal["badge"]), ("2", "約4 kt ・ 風力 2", ""))
        self.assertNotIn("high", normal["cls"])
        self.assertIn("high", result["high"]["cls"])
        self.assertIn("注意ライン以上", result["high"]["badge"])
        self.assertTrue(result["high"]["maxOver"])
        self.assertTrue(result["stale"]["badge"].startswith("判定できません"))
        self.assertIn("最終観測", result["stale"]["badge"])
        self.assertEqual((result["retry"]["retry"], result["retry"]["badge"]), ("取得再試行中", ""))
        self.assertIn("caution", result["retry"]["cls"])
        missing = result["missing"]
        self.assertEqual((missing["dir"], missing["speed"], missing["retry"]), ("欠測", "欠測", ""))
        self.assertIn("最新の観測が欠測", missing["badge"])
        self.assertIn("観測データなし", result["none"]["badge"])

    def test_wind_caution_and_stale_are_visible_from_a_distance(self):
        result = self.wind("""() => {
          const measure = mode => {
            lastStatus = demoStatus(mode);
            render(lastStatus);
            const st = document.querySelectorAll('.station')[1], css = (el, prop) => getComputedStyle(el)[prop];
            return {outline: css(st, 'boxShadow'), speed: css(st.querySelector('.speed'), 'color'),
                    max: css(st.querySelector('.stat-value'), 'color'), chart: css(st.querySelector('.chart'), 'opacity'),
                    limit: st.querySelector('.limit').getAttribute('opacity')};
          };
          return {normal: measure('normal'), high: measure('high'), stale: measure('1')};
        }""")
        amber = "rgb(240, 180, 41)"
        high, normal, stale = result["high"], result["normal"], result["stale"]
        self.assertIn(amber, high["outline"])
        self.assertEqual((high["speed"], high["max"], high["limit"]), (amber, amber, "1"))
        self.assertEqual((normal["outline"], normal["limit"]), ("none", "0.5"))
        # 古い値は注意ラインを超えていても琥珀色にせず、図ごと沈める
        self.assertNotIn(amber, stale["outline"] + stale["speed"] + stale["max"])
        self.assertEqual(stale["chart"], "0.45")

    def test_wind_chart_labels_do_not_collide(self):
        result = self.wind("""() => {
          const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
          const cases = [];
          for (const speed of [10, 12, 17, 18, 19, 23, 30]) {
            for (const index of [0, 3, 7, 12]) {
              const status = demoStatus('high'), s = status.stations[1];
              s.series.forEach(r => { r.speed = Math.min(r.speed, 5); });
              s.series[index].speed = speed;
              s.maximum = s.series[index];
              render(status);
              const st = document.querySelectorAll('.station')[1];
              const label = st.querySelector('.max-label').getBoundingClientRect();
              const chart = st.querySelector('.chart').getBoundingClientRect();
              cases.push({speed, index,
                vane: [...st.querySelectorAll('.vane')].some(v => hit(label, v.getBoundingClientRect())),
                limit: hit(label, st.querySelector('.limit text').getBoundingClientRect()),
                inside: label.left >= chart.left - 1 && label.right <= chart.right + 1 && label.bottom <= chart.bottom});
            }
          }
          return cases;
        }""")
        for case in result:
            with self.subTest(speed=case["speed"], index=case["index"]):
                self.assertFalse(case["vane"])
                self.assertFalse(case["limit"])
                self.assertTrue(case["inside"])

    def test_wind_rose_period_does_not_touch_north_label(self):
        result = self.wind("""() => [0, 0.5, 6.95, 13.0, 23.4, 24].map(hours => {
          const status = demoStatus('normal');
          Object.assign(status.stations[1].rose, {hours, samples: hours ? 10 : 0});
          render(status);
          const rose = document.querySelectorAll('.rose')[1];
          const period = rose.querySelector('.rose-period'), north = [...rose.querySelectorAll('text')].find(t => t.textContent === '北');
          return [period.textContent, Math.round(north.getBoundingClientRect().left - period.getBoundingClientRect().right)];
        })""")
        self.assertEqual([text for text, _ in result],
                         ['分布は蓄積待ち', '分布は蓄積中', '過去6時間の分布', '過去13時間の分布', '過去23時間の分布', '過去24時間の分布'])
        self.assertTrue(all(gap >= 8 for _, gap in result), result)

    def test_wind_unit_does_not_move_with_the_digits(self):
        result = self.wind("""() => [11, 18, 88].map(speed => {
          const status = demoStatus('normal');
          status.stations[1].latest.speed = speed;
          render(status);
          return Math.round(document.querySelectorAll('.station')[1].querySelector('.unit').getBoundingClientRect().left);
        })""")
        self.assertEqual(len(set(result)), 1, result)

    def test_wind_board_shifts_slightly_against_burn_in(self):
        result = self.wind("""() => {
          const real = Date.now, seen = [];
          try {
            for (let k = 0; k < ORBIT.length; k++) {
              Date.now = () => k * 600000;
              fit();
              const board = document.getElementById('board');
              seen.push([parseFloat(board.style.left), parseFloat(board.style.top)]);
            }
          } finally { Date.now = real; }
          return seen;
        }""")
        self.assertEqual(len({tuple(p) for p in result}), 8)
        self.assertTrue(all(abs(v) <= 2 for p in result for v in p), result)


if __name__ == "__main__":
    unittest.main()
