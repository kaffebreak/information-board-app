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


if __name__ == "__main__":
    unittest.main()
