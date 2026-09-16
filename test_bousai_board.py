"""防災ボードの判定を検証する（ネットワーク不要）。

取得関数を差し替え、2026-09 に確認した実データと同じ形の試験データを渡す。
発令条件は依頼者の指定なので、判定を変えるときはここも合わせて見直すこと。
"""
import datetime as dt
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import bousai_board as b

NOW = dt.datetime(2026, 9, 16, 12, 0, tzinfo=b.JST)


def ago(minutes, now=NOW):
    return (now - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


class Router:
    """URL の一部 → 返す値（例外なら送出）。登録のない URL は試験の誤りとして止める"""

    def __init__(self, routes):
        self.routes = routes

    def __call__(self, url, **kw):
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"想定外の取得: {url}")


class BoardTest(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(b, "now_jst", return_value=NOW),
                  patch.dict(b._WARN_PARTS, clear=True),
                  patch.dict(b._detail_cache, clear=True),
                  patch.dict(b.STATE, clear=True),
                  patch.dict(b.CFG, quake_hold_hours=24,
                             jr_lines=["東海道線(豊橋～米原)", "武豊線"],
                             subway_lines={"M_LINE": "名城線・名港線"},
                             meitetsu_lines=["名古屋本線", "河和線", "常滑線"])):
            p.start()
            self.addCleanup(p.stop)

    def routes(self, getter="get_json", **routes):
        p = patch.object(b, getter, Router(routes))
        p.start()
        self.addCleanup(p.stop)


# ----------------------------------------------------------------------
# 地震
# ----------------------------------------------------------------------
def quake(eid, minutes, maxi, prefs=(), ift="発表", ttl="震源・震度情報", name=None):
    """quake/data/list.json の1電文。prefs は (県コード, 県内最大震度)"""
    item = {"eid": eid, "rdt": ago(minutes), "at": ago(minutes + 3), "ttl": ttl, "ift": ift,
            "anm": "三重県南東沖", "mag": "7.0", "maxi": maxi,
            "json": name or f"{eid}_{minutes}_VXSE53_1.json"}
    if prefs:
        item["int"] = [{"code": c, "maxi": m} for c, m in prefs]
    return item


def quake_detail(*areas):
    """詳細電文。areas は (県コード, 細分区域コード, 名称, 最大震度)"""
    prefs = {}
    for pc, code, name, mi in areas:
        prefs.setdefault(pc, []).append({"Code": code, "Name": name, "MaxInt": mi})
    return {"Body": {"Intensity": {"Observation": {"Pref": [
        {"Code": pc, "Area": a} for pc, a in prefs.items()]}}}}


class QuakeTests(BoardTest):
    def test_revised_down_report_is_final(self):
        # 震度速報で5弱 → 震源・震度情報で4に下方修正。最終報の4で判定する
        self.routes(**{"quake/data/list.json": [
            quake("E1", 10, "5-", [("23", "5-")], ttl="震度速報"),
            quake("E1", 5, "4", [("23", "4")])]})
        r = b.src_quake()
        self.assertEqual(r["local"], [])
        self.assertEqual((r["latest_local"]["pref"], r["latest_local"]["pref_maxi"]), ("愛知県", "4"))

    def test_hypocenter_only_report_does_not_hide_intensity(self):
        # 震源に関する情報（震度なし）が後に来ても、震度を持つ電文を確定値にする
        self.routes(**{"quake/data/list.json": [
            quake("E1", 10, "5+", [("23", "5+")], ttl="震度速報", name="E1_int.json"),
            quake("E1", 8, "", ttl="震源に関する情報")],
            "E1_int.json": quake_detail(("23", "451", "愛知県西部", "5+"))})
        self.assertEqual(b.src_quake()["local"][0]["areas"], [{"name": "愛知県西部", "int": "5+"}])

    def test_cancellation_removes_event_regardless_of_order(self):
        for order in (1, -1):
            with self.subTest(order=order):
                reports = [quake("E1", 5, "5+", [("23", "5+")]), quake("E1", 5, "", ift="取消")][::order]
                self.routes(**{"quake/data/list.json": reports})
                r = b.src_quake()
                self.assertEqual((r["local"], r["latest_local"], r["latest_any"]), ([], None, None))

    def test_later_cancellation_removes_event(self):
        self.routes(**{"quake/data/list.json": [
            quake("E1", 10, "6-", [("23", "6-")]), quake("E1", 5, "", ift="取消")]})
        r = b.src_quake()
        self.assertEqual((r["local"], r["national"]), ([], []))

    def test_only_target_areas_count(self):
        # 三重県南部（462）の5弱は対象外。愛知県西部の5強だけを出す
        self.routes(**{"quake/data/list.json": [quake("E1", 5, "5+", [("23", "5+"), ("24", "5-")], name="d.json")],
                       "d.json": quake_detail(("23", "451", "愛知県西部", "5+"), ("24", "460", "三重県北部", "4"),
                                              ("24", "462", "三重県南部", "5-"))})
        local = b.src_quake()["local"]
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0]["areas"], [{"name": "愛知県西部", "int": "5+"}])

    def test_mie_south_only_is_not_alarm(self):
        self.routes(**{"quake/data/list.json": [quake("E1", 5, "5-", [("24", "5-")], name="d.json")],
                       "d.json": quake_detail(("24", "462", "三重県南部", "5-"))})
        self.assertEqual(b.src_quake()["local"], [])

    def test_all_confirmed_area_codes_are_targets(self):
        self.assertEqual(set(b.QUAKE_AREAS), {"450", "451", "460", "461"})
        self.routes(**{"quake/data/list.json": [quake("E1", 5, "5-", [("24", "5-")], name="d.json")],
                       "d.json": quake_detail(("24", "461", "三重県中部", "5-"))})
        self.assertEqual(b.src_quake()["local"][0]["areas"], [{"name": "三重県中部", "int": "5-"}])

    def test_detail_failure_falls_back_to_prefecture_on_safe_side(self):
        self.routes(**{"quake/data/list.json": [quake("E1", 5, "5-", [("23", "5-"), ("24", "5-")], name="d.json")],
                       "d.json": OSError("timed out")})
        with self.assertLogs("board", "WARNING"):
            areas = b.src_quake()["local"][0]["areas"]
        self.assertEqual(areas, [{"name": "愛知県", "int": "5-"}, {"name": "三重県（区域は確認中）", "int": "5-"}])

    def test_national_threshold_and_hold_period(self):
        self.routes(**{"quake/data/list.json": [
            quake("E1", 30, "6-", [("27", "6-")]),
            quake("E2", 20, "5+", [("27", "5+")]),
            quake("E3", 25 * 60, "7", [("27", "7")]),
            quake("E4", 5, "1", [("43", "1")])]})
        r = b.src_quake()
        self.assertEqual([e["maxi"] for e in r["national"]], ["6-"])
        self.assertEqual(r["latest_any"]["maxi"], "1")
        self.assertIsNone(r["latest_local"])

    def test_long_period_ground_motion(self):
        item = {"eid": "E1", "rdt": ago(5), "at": ago(8), "ift": "発表", "anm": "三重県南東沖", "mag": "7.0",
                "lg": [{"code": "451", "maxLg": "3"}, {"code": "462", "maxLg": "4"}, {"code": "450", "maxLg": "2"}]}
        old = dict(item, eid="E0", rdt=ago(25 * 60), at=ago(25 * 60 + 3))
        self.routes(**{"ltpgm/data/list.json": [item, old]})
        hits = b.src_ltpgm()["hits"]
        self.assertEqual([h["areas"] for h in hits], [[{"name": "愛知県西部", "lg": "3"}]])


# ----------------------------------------------------------------------
# 津波
# ----------------------------------------------------------------------
def tsunami_item(area, kind, height=None):
    it = {"Area": {"Name": area}, "Category": {"Kind": {"Name": kind}}}
    if height is not None:
        it["MaxHeight"] = {"TsunamiHeight": height}
    return it


class TsunamiTests(BoardTest):
    def load(self, items, minutes=10, ift="発表"):
        self.routes(**{"tsunami/data/list.json": [
            {"json": "t_VTSE41.json", "rdt": ago(minutes), "ift": ift},
            {"json": "t_VTSE51.json", "rdt": ago(1), "ift": "発表"}],   # 津波情報は判定に使わない
            "t_VTSE41.json": {"Body": {"Tsunami": {"Forecast": {"Item": items}}}}})
        return b.src_tsunami()

    def test_target_areas_and_heights(self):
        r = self.load([tsunami_item("伊勢・三河湾", "大津波警報：発表", {"description": "５ｍ", "value": "5"}),
                       tsunami_item("愛知県外海", "津波警報", {"value": "3"}),
                       tsunami_item("三重県南部", "大津波警報", {"value": "10"})])
        self.assertEqual(r["items"], [{"area": "伊勢・三河湾", "kind": "大津波警報", "height": "５ｍ"},
                                      {"area": "愛知県外海", "kind": "津波警報", "height": "3m"}])

    def test_cancelled_or_old_report_is_empty(self):
        item = [tsunami_item("伊勢・三河湾", "大津波警報")]
        self.assertEqual(self.load(item, ift="取消")["items"], [])
        self.assertEqual(self.load(item, minutes=4 * 24 * 60)["items"], [])

    def test_no_forecast_report(self):
        self.routes(**{"tsunami/data/list.json": []})
        self.assertEqual(b.src_tsunami(), {"items": [], "report_at": None})


# ----------------------------------------------------------------------
# 警報（新防災気象情報 r8）
# ----------------------------------------------------------------------
AREA = {
    "class10s": {"230010": {}, "230020": {}, "240010": {}, "240020": {}},
    "class15s": {"230011": {"parent": "230010"}, "230021": {"parent": "230020"},
                 "240011": {"parent": "240010"}, "240021": {"parent": "240020"}},
    "class20s": {"2310000": {"name": "名古屋市", "parent": "230011"},
                 "2320100": {"name": "豊橋市", "parent": "230021"},
                 "2420100": {"name": "津市", "parent": "240011"},
                 "2420300": {"name": "伊勢市", "parent": "240021"}},
}
QUIET = [(None, "発表警報・注意報はなし")]


def report(dtype, c20=(), c10=(), info="発表"):
    """warning/data/r8/{県}.json の1電文（配列には電文種別ごとに最新1件が並ぶ。2026-09-16 確認）"""
    def items(pairs):
        return [{"areaCode": code, "kinds": [{"code": k, "status": s} for k, s in kinds]} for code, kinds in pairs]
    return {"dataTypeCode": dtype, "infoType": info, "reportDatetime": ago(5),
            "warning": {"class10Items": items(c10), "class20Items": items(c20)}}


class WarningTests(BoardTest):
    def setUp(self):
        super().setUp()
        p = patch.object(b.AREA_CONST, "get", return_value=AREA)
        p.start()
        self.addCleanup(p.stop)

    def load(self, aichi=(), mie=(), flood=()):
        value = lambda v: v if isinstance(v, Exception) else list(v)
        self.routes(**{"r8/230000.json": value(aichi), "r8/240000.json": value(mie),
                       "flood/data/r8/flood_xml.json": value(flood)})
        return b.src_warning()

    def test_levels_and_target_regions(self):
        r = self.load(
            aichi=[report("VPWW55", c20=[("2310000", [("33", "発表"), ("10", "継続")]), ("2320100", QUIET)]),
                   report("VPWW61", c10=[("230020", [("35", "継続")])])],
            mie=[report("VPWW55", c20=[("2420300", [("33", "発表")]), ("2420100", [("43", "継続")])])])
        self.assertEqual(r["lv5"], [{"name": "レベル5大雨特別警報", "regions": ["愛知県西部"], "places": ["名古屋市"]}])
        self.assertEqual(r["lv4"], [{"name": "レベル4大雨危険警報", "regions": ["三重県北中部"], "places": ["津市"]}])
        self.assertEqual(r["wx"], [{"name": "暴風特別警報", "regions": ["愛知県東部"], "places": []}])
        self.assertEqual((r["partial"], r["unknown"]), ([], []))

    def test_lifted_and_cancelled_reports_are_ignored(self):
        r = self.load(aichi=[report("VPWW55", c20=[("2310000", [("33", "解除")])]),
                             report("VPWW56", c20=[("2310000", [("38", "発表")])], info="取消")])
        self.assertEqual((r["lv5"], r["lv4"], r["wx"]), ([], [], []))

    def test_all_special_warning_codes(self):
        kinds = [(c, "発表") for c in list(b.LV5_CODES) + list(b.WX_SPECIAL_CODES)]
        r = self.load(aichi=[report("VPWW55", c20=[("2310000", kinds)])])
        self.assertEqual({w["name"] for w in r["lv5"]}, set(b.LV5_CODES.values()))
        self.assertEqual({w["name"] for w in r["wx"]}, set(b.WX_SPECIAL_CODES.values()))

    def test_river_flood_levels(self):
        # 河川名のキーは未確認（平常時は空配列）。ここでは名前が無くても地域で出ることを確かめる
        r = self.load(flood=[{"item": {"code": "51"}, "class10Codes": ["230010", "240020"]},
                             {"item": {"code": "41"}, "class10Codes": ["240010"]},
                             {"item": {"code": "53"}, "class10Codes": ["240020"]},
                             {"item": {"code": "30"}, "class10Codes": ["230020"]}])
        self.assertEqual(r["lv5"], [{"name": "レベル5氾濫特別警報", "regions": ["愛知県西部"], "places": []}])
        self.assertEqual(r["lv4"], [{"name": "レベル4氾濫危険警報", "regions": ["三重県北中部"], "places": []}])

    def test_failed_part_keeps_previous_value_then_becomes_unknown(self):
        self.load(aichi=[report("VPWW55", c20=[("2310000", [("33", "発表")])])])
        with self.assertLogs("board", "WARNING"):
            r = self.load(aichi=OSError("timed out"))
        self.assertEqual(r["lv5"][0]["name"], "レベル5大雨特別警報")      # 前回値で継続
        self.assertEqual((r["partial"], r["partial_wx"], r["unknown"]), (["愛知県の警報"], ["愛知県の警報"], []))
        b._WARN_PARTS["230000"]["at"] -= b._warn_window() + 1          # 長く取れていない状態にする
        with self.assertLogs("board", "WARNING"):
            r = self.load(aichi=OSError("timed out"))
        self.assertEqual((r["unknown"], r["unknown_wx"]), (["愛知県の警報"], ["愛知県の警報"]))
        self.assertEqual(r["lv5"][0]["name"], "レベル5大雨特別警報")

    def test_river_failure_does_not_affect_weather_tile(self):
        with self.assertLogs("board", "WARNING"):
            r = self.load(flood=OSError("timed out"))
        self.assertEqual((r["partial"], r["unknown"]), (["河川情報"], ["河川情報"]))
        self.assertEqual((r["partial_wx"], r["unknown_wx"]), ([], []))

    def test_nothing_available_is_a_failure(self):
        with self.assertLogs("board", "WARNING"), self.assertRaises(RuntimeError):
            self.load(aichi=OSError("x"), mie=OSError("x"), flood=OSError("x"))


# ----------------------------------------------------------------------
# 発令条件タイル・鮮度
# ----------------------------------------------------------------------
class AlertTests(BoardTest):
    def put(self, key, value, age=0.0, error=None):
        b.STATE[key] = {"value": value, "updated": time.time() - age, "error": error,
                        "failed_at": None, "interval": b.DEFAULT_CONFIG["intervals"][key]}

    def tiles(self):
        return {a["id"]: a for a in b.build_alerts()}

    def normal_sources(self):
        self.put("quake", {"national": [], "local": [], "latest_any": None, "latest_local": None})
        self.put("ltpgm", {"hits": []})
        self.put("tsunami", {"items": [], "report_at": None})
        self.put("warning", {"lv5": [], "lv4": [], "wx": [], "report_at": None, "partial": [], "partial_wx": [],
                             "unknown": [], "unknown_wx": []})

    def test_nothing_fetched_is_not_judged(self):
        self.assertTrue(all(a["stale"] and a["level"] == "normal" for a in self.tiles().values()))

    def test_normal(self):
        self.normal_sources()
        self.assertTrue(all(not a["stale"] and not a["failing"] and a["level"] == "normal"
                            for a in self.tiles().values()))

    def test_each_condition_raises_its_tile(self):
        self.normal_sources()
        ev = {"at": ago(5), "anm": "三重県南東沖", "mag": "7.0", "maxi": "6-"}
        self.put("quake", {"national": [ev], "local": [dict(ev, areas=[{"name": "愛知県西部", "int": "5-"}])],
                           "latest_any": ev, "latest_local": None})
        self.put("ltpgm", {"hits": [dict(ev, areas=[{"name": "三重県北部", "lg": "3"}])]})
        self.put("tsunami", {"items": [{"area": "愛知県外海", "kind": "津波注意報", "height": ""},
                                       {"area": "伊勢・三河湾", "kind": "大津波警報", "height": "5m"}]})
        w = {"name": "暴風特別警報", "regions": ["愛知県西部"], "places": []}
        self.put("warning", {"lv5": [dict(w, name="レベル5高潮特別警報")], "lv4": [], "wx": [w],
                             "partial": [], "partial_wx": [], "unknown": [], "unknown_wx": []})
        t = self.tiles()
        self.assertEqual({k: a["level"] for k, a in t.items()},
                         {"quake_local": "alarm", "tsunami": "alarm", "lv5": "alarm", "wx": "alarm",
                          "quake_national": "alarm"})
        self.assertEqual([l["main"] for l in t["quake_local"]["lines"]],
                         ["愛知県西部 震度5弱", "長周期地震動 三重県北部 階級3"])
        self.assertEqual([l["main"] for l in t["tsunami"]["lines"]], ["大津波警報", "津波注意報"])

    def test_reference_levels_are_caution_only(self):
        self.normal_sources()
        self.put("tsunami", {"items": [{"area": "伊勢・三河湾", "kind": "津波警報", "height": "3m"},
                                       {"area": "愛知県外海", "kind": "津波注意報解除", "height": ""}]})
        self.put("warning", {"lv5": [], "lv4": [{"name": "レベル4土砂災害危険警報", "regions": ["三重県北中部"],
                                                 "places": ["津市"]}], "wx": [],
                             "partial": [], "partial_wx": [], "unknown": [], "unknown_wx": []})
        t = self.tiles()
        self.assertEqual((t["tsunami"]["level"], len(t["tsunami"]["lines"])), ("caution", 1))
        self.assertEqual(t["lv5"]["level"], "caution")

    def test_freshness_states(self):
        self.put("quake", {})
        self.put("ltpgm", {}, error="URLError: timed out")
        self.put("tsunami", {}, age=181)
        self.put("warning", {"partial": ["河川情報"]})
        views = {k: b.source_view(k)[1] for k in ("quake", "ltpgm", "tsunami", "warning", "jr")}
        self.assertEqual((views["quake"]["stale"], views["quake"]["failing"]), (False, False))
        self.assertEqual((views["ltpgm"]["stale"], views["ltpgm"]["failing"]), (False, True))
        self.assertTrue(views["tsunami"]["stale"])
        self.assertEqual(views["warning"]["partial"], ["河川情報"])
        self.assertEqual((views["jr"]["stale"], views["jr"]["failing"], views["jr"]["updated"]), (True, False, None))


# ----------------------------------------------------------------------
# 鉄道
# ----------------------------------------------------------------------
def mt_page(*blocks):
    inner = "".join(blocks) or '<p class="emLv00" id="descriptionText">15分以上の列車の遅れはございません。</p>'
    return f'<html><body><main><div class="layEm">{inner}</div><div class="wrapEmAdd01">x</div></main></body></html>'


def mt_block(level, state, lines=None, reasons=(), notes=(), sections=()):
    """名鉄 運行情報ページの異常時ブロック（2026-09-11 に確認した実物の構造）"""
    rows = ""
    if lines is not None:
        rows += '<tr><th>路線</th><td><ul class="emListLine">' + "".join(f"<li>{x}</li>" for x in lines) + "</ul></td></tr>"
    if sections:
        rows += "<tr><th>区間</th><td><dl>" + "".join(f"<dt>{x}</dt>" for x in sections) + "</dl></td></tr>"
    if reasons:
        rows += "<tr><th>理由</th><td><dl>" + "".join(f"<dt>{x}</dt>" for x in reasons) + "</dl></td></tr>"
    if notes:
        rows += "<tr><th>備考</th><td><span>" + "<br>".join(notes) + "</span></td></tr>"
    return f'<div class="emInfo emLv{level}"><h2>{state}</h2><table>{rows}</table></div>'


class MeitetsuTests(BoardTest):
    def load(self, page, at=NOW):
        self.routes("get_text", **{"top.meitetsu.co.jp/em/": page, "tokudashi": OSError("none")})
        with patch.object(b, "now_jst", return_value=at):
            r = b.src_meitetsu()
        return r, {l["name"]: l for l in r["lines"]}

    def test_normal_and_off_hours_boundaries(self):
        for hour, minute, level in [(12, 0, "normal"), (0, 30, "normal"), (0, 31, "offhours"),
                                    (4, 59, "offhours"), (5, 0, "normal")]:
            with self.subTest(time=f"{hour}:{minute:02}"):
                r, lines = self.load(mt_page(), NOW.replace(hour=hour, minute=minute))
                self.assertEqual({l["level"] for l in lines.values()}, {level})
                self.assertEqual(bool(r["info"]), level == "offhours")

    def test_structure_change_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            self.load("<html><body>メンテナンス中</body></html>")

    def test_direct_stop(self):
        r, lines = self.load(mt_page(mt_block("01", "運転見合せ", ["名古屋本線"],
                                              reasons=["名古屋本線 神宮前駅～金山駅間 人身事故"],
                                              notes=["運転再開は12時30分頃の見込みです。", "列車走行位置をご覧ください。"])))
        self.assertEqual((lines["名古屋本線"]["level"], lines["名古屋本線"]["status"]), ("stop", "運転見合せ"))
        self.assertEqual(lines["名古屋本線"]["detail"], "名古屋本線 神宮前駅～金山駅間 人身事故")
        self.assertEqual((lines["河和線"]["level"], r["other"]), ("normal", ""))

    def test_level_number_decides_stop_without_wording(self):
        _, lines = self.load(mt_page(mt_block("01", "運行情報", ["常滑線"], reasons=["常滑線 大江駅構内 車両点検"])))
        self.assertEqual(lines["常滑線"]["level"], "stop")

    def test_stop_block_wins_over_delay_block(self):
        _, lines = self.load(mt_page(
            mt_block("02", "遅延・一部運休", ["常滑線"], reasons=["常滑線 大江駅～大同町駅間 踏切事故"]),
            mt_block("01", "運転見合せ", ["常滑線"], reasons=["常滑線 大江駅～大同町駅間 踏切事故"])))
        self.assertEqual((lines["常滑線"]["level"], lines["常滑線"]["status"]), ("stop", "運転見合せ"))

    def test_spillover_from_other_line(self):
        r, lines = self.load(mt_page(mt_block("02", "遅延・一部運休", ["名古屋本線", "三河線"],
                                              reasons=["三河線 土橋駅～上挙母駅間 人身事故"])))
        self.assertEqual((lines["名古屋本線"]["level"], lines["名古屋本線"]["detail"]), ("delay", "三河線 人身事故の影響"))
        self.assertEqual(r["other"], "事由：三河線 土橋駅～上挙母駅間 人身事故")

    def test_whole_network_and_reason_without_line_name(self):
        _, lines = self.load(mt_page(mt_block("01", "運転見合せ", ["全線"], reasons=["全線 地震"])))
        self.assertEqual({(l["level"], l["detail"]) for l in lines.values()}, {("stop", "全線 地震")})
        r, lines = self.load(mt_page(mt_block("02", "遅延・一部運休", ["河和線"], reasons=["強風のため 一部列車に遅れ"])))
        self.assertEqual(lines["河和線"]["detail"], "強風のため 一部列車に遅れ")
        self.assertEqual(r["other"], "")

    def test_off_hours_still_shows_abnormal_blocks(self):
        r, lines = self.load(mt_page(mt_block("01", "運転見合せ", ["河和線"], reasons=["河和線 地震"])),
                             NOW.replace(hour=2))
        self.assertEqual((lines["河和線"]["level"], lines["名古屋本線"]["level"], r["info"]), ("stop", "normal", ""))

    def test_substitute_transport_without_line_table(self):
        r, lines = self.load(mt_page(mt_block("03", "振替輸送", None,
                                              sections=["名古屋本線 豊橋駅～名鉄岐阜駅間", "瀬戸線 終了しました"])))
        self.assertEqual({l["level"] for l in lines.values()}, {"normal"})
        self.assertEqual(r["other"], "振替輸送：名古屋本線 豊橋駅～名鉄岐阜駅間")


class AonamiTests(BoardTest):
    def load(self, cell):
        page = f'<table class="delay_table"><tr><td>{cell}</td></tr></table>' if cell is not None else "<p>x</p>"
        self.routes("get_text", aonamiline=page)
        return b.src_aonami()

    def test_levels(self):
        self.assertEqual(self.load("現在、平常通り運転しております。")["level"], "normal")
        r = self.load("強風のため<br>全区間において運転を見合わせております。")
        self.assertEqual((r["level"], r["status"]), ("stop", "強風のため"))
        self.assertEqual(self.load("車両点検のため、遅れが発生しております。")["level"], "delay")

    def test_unreadable_page_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            self.load(None)


class JrTests(BoardTest):
    MASTER = {"lst": [
        {"ryokakuSenkuMei": "東海道線", "ryokakuSenkuKaishiShuryoEki": "豊橋～米原",
         "ekiNumberKigobu": "CA", "ryokakuSenkuColorCdchi": "#F77321"},
        {"ryokakuSenkuMei": "武豊線", "ryokakuSenkuKaishiShuryoEki": "大府～武豊",
         "ekiNumberKigobu": "CE", "ryokakuSenkuColorCdchi": "#F77321"}]}

    def setUp(self):
        super().setUp()
        p = patch.object(b.JR_MASTER, "get", return_value=self.MASTER)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def event(line, status):
        return {"imp_line": [{"lang": "ja", "name": line}], "status": [{"lang": "ja", "name": status}]}

    def load(self, unkou, service="0"):
        self.routes(hp_service_jotai_kanri={"serviceJotai": service}, **{"unkou.json": unkou})
        r = b.src_jr()
        return r, {l["name"]: l for l in r["lines"]}

    def test_normal(self):
        _, lines = self.load({"events": None, "message_info": None})
        self.assertEqual({(l["level"], l["status"]) for l in lines.values()}, {("normal", "平常運転")})
        self.assertEqual(lines["東海道線"]["sub"], "豊橋～米原")

    def test_stop_status_wins_over_delay(self):
        msg = lambda t: {"delivery_msg": [{"lang": "ja", "message": t}]}
        _, lines = self.load({"events": [self.event("東海道線(豊橋～米原)", "遅れ"), self.event("東海道線", "運転見合わせ")],
                              "message_info": [msg("大府駅で線路点検"), msg("地震の影響で全線")]})
        line = lines["東海道線"]
        self.assertEqual((line["level"], line["status"]), ("stop", "運転見合わせ"))
        self.assertEqual(line["detail"], "遅れ　大府駅で線路点検 / 地震の影響で全線")
        self.assertEqual(lines["武豊線"]["level"], "normal")

    def test_maintenance(self):
        r, lines = self.load({"events": None}, service="1")
        self.assertEqual({(l["level"], l["status"]) for l in lines.values()}, {("offhours", "情報提供停止中")})
        self.assertTrue(r["info"])


class SubwayTests(BoardTest):
    def load(self, rows):
        self.routes(**{"kotsu.city.nagoya.jp": rows})
        return b.src_subway()["lines"][0]

    def test_levels(self):
        row = lambda icon, title, **kw: dict(rosen_id="M_LINE", icon_cd=icon, traffic_title=title, **kw)
        self.assertEqual(self.load([row("N_T_ICO", "平常運行")])["status"], "平常運転")
        line = self.load([row("N_T_ICO", "平常運行"),
                          row("E_T_ICO", "運転見合わせ", traffic_section="全線", traffic_cause="地震")])
        self.assertEqual((line["level"], line["status"], line["detail"]), ("stop", "運転見合わせ", "全線　地震"))
        self.assertEqual(self.load([row("N_T_ICO", "お知らせ")])["level"], "info")
        self.assertEqual(self.load([])["level"], "unknown")


# ----------------------------------------------------------------------
# 起動
# ----------------------------------------------------------------------
class MainTests(unittest.TestCase):
    def test_startup_failure_is_logged_before_any_fetch(self):
        root = logging.getLogger()
        before = list(root.handlers)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(b, "HERE", folder), patch.object(b.logging, "basicConfig"), \
                patch.object(b.threading, "excepthook"), patch.object(b.threading, "Thread") as thread, \
                patch.object(b, "ThreadingHTTPServer", side_effect=OSError("address already in use")), \
                patch.object(b, "CONFIG_ERROR", "config.json の読み込みに失敗: 壊れた設定（既定値で起動）"):
            try:
                with self.assertRaises(SystemExit):
                    b.main()
            finally:
                for h in root.handlers[:]:
                    if h not in before:
                        root.removeHandler(h)
                        h.close()
            thread.assert_not_called()
            text = Path(folder, "board.log").read_text(encoding="utf-8")
        self.assertIn("address already in use", text)
        self.assertIn("壊れた設定", text)


if __name__ == "__main__":
    unittest.main()
