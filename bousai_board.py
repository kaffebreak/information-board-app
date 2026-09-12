#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bousai_board.py  防災・交通情報モニタ（サーバー側）

  起動:  python bousai_board.py
  表示:  ブラウザで http://127.0.0.1:8765/ を全画面表示（start_board.bat 参照）
  動作:  Python 3.9 以上、標準ライブラリのみ

  取得元
    気象庁          地震・長周期地震動・津波・警報（新防災気象情報 r8 形式）
    JR東海          在来線運行情報 JSON
    名古屋市交通局  地下鉄運行情報 JSON
    名鉄            運行情報ページ（HTML）
    あおなみ線      運行情報ページ（HTML）
    NEXCO中日本     アイハイウェイ 規制・渋滞情報 JSON（伊勢湾岸道）
    名古屋国道事務所 規制情報・緊急情報（国道23号 愛知県側）
"""
from __future__ import annotations

import datetime as dt
import gzip
import html
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

JST = dt.timezone(dt.timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bousai-board/1.0"

log = logging.getLogger("board")

# ======================================================================
# 設定（config.json があれば上書き）
# ======================================================================
DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    # 空欄なら地図パネルは出さない
    "google_maps_api_key": "",
    # 地震・長周期地震動を「発生から何時間」表示し続けるか
    "quake_hold_hours": 24,
    # JR東海の表示路線（JRサイトの表記。東海道線だけは区間付きで指定）
    "jr_lines": ["東海道線(豊橋～米原)", "武豊線"],
    # 名古屋市営地下鉄（rosen_id: 表示名）
    # 名城線と名港線は交通局のデータ上ひとつ（M_LINE）で配信される
    "subway_lines": {"M_LINE": "名城線・名港線"},
    # 名鉄の表示線区（運行情報ページの文章に線区名が出たら、その線区を異常表示）
    "meitetsu_lines": ["名古屋本線", "河和線", "常滑線"],
    # 伊勢湾岸道の監視区間（IC/JCT名の地名部分）
    "isewangan_east": "東海",
    "isewangan_west": "みえ川越",
    # 国道23号（保留中。true にすると名古屋国道事務所の規制・緊急情報を表示）
    "enable_route23": False,
    "route23_notes": [],
    # 取得間隔（秒）
    "intervals": {
        "quake": 20,
        "ltpgm": 60,
        "tsunami": 20,
        "warning": 60,
        "jr": 60,
        "subway": 60,
        "meitetsu": 90,
        "aonami": 120,
        "ihighway": 90,
        "meikoku": 180,
    },
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                user = json.load(f)
            for k, v in user.items():
                # intervals だけは項目単位の上書き。subway_lines などは「表示するもの」の指定なので丸ごと置き換える
                if k == "intervals" and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:  # 設定ミスで止まらないように
            log.error("config.json の読み込みに失敗: %s（既定値で起動）", e)
    return cfg


CFG = load_config()

# ======================================================================
# 監視対象（依頼条件そのもの。通常は変更不要）
# ======================================================================
JMA = "https://www.jma.go.jp/bosai/"

# 警報: 愛知県全域 + 三重県北中部（一次細分区域）
TARGET_CLASS10 = {"230010": "愛知県西部", "230020": "愛知県東部", "240010": "三重県北中部"}

# 地震情報の細分区域（震度・長周期地震動）: 愛知県全域 + 三重県北部・中部
QUAKE_AREAS = {"450": "愛知県東部", "451": "愛知県西部", "460": "三重県北部", "461": "三重県中部"}

# 津波予報区（愛知県・三重県北中部の沿岸）
TSUNAMI_AREAS = ["伊勢・三河湾", "愛知県外海"]

# 新防災気象情報（2026年5月29日運用開始）のコード
LV5_CODES = {"33": "レベル5大雨特別警報", "39": "レベル5土砂災害特別警報", "38": "レベル5高潮特別警報"}
LV4_CODES = {"43": "レベル4大雨危険警報", "49": "レベル4土砂災害危険警報", "48": "レベル4高潮危険警報"}
WX_SPECIAL_CODES = {"35": "暴風特別警報", "32": "暴風雪特別警報", "36": "大雪特別警報", "37": "波浪特別警報"}
FLOOD_LV5 = {"51", "53"}
FLOOD_LV4 = {"40", "41"}

INT_RANK = {"1": 1, "2": 2, "3": 3, "4": 4, "5-": 5, "5+": 6, "6-": 7, "6+": 8, "7": 9}
INT_JA = {"1": "1", "2": "2", "3": "3", "4": "4", "5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強", "7": "7"}

# ======================================================================
# 共通ユーティリティ
# ======================================================================
_http_cache: dict = {}
_http_lock = threading.Lock()


def http_get(url: str, timeout: int = 20, conditional: bool = True) -> bytes:
    """GET（gzip対応）。conditional=True なら ETag/Last-Modified で差分取得。"""
    headers = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.5", "Accept-Encoding": "gzip"}
    cached = None
    if conditional:
        with _http_lock:
            cached = _http_cache.get(url)
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("lm"):
                headers["If-Modified-Since"] = cached["lm"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            etag, lm = r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return cached["body"]
        raise
    if conditional and (etag or lm):
        with _http_lock:
            _http_cache[url] = {"etag": etag, "lm": lm, "body": body}
    return body


def get_text(url: str, **kw) -> str:
    return http_get(url, **kw).decode("utf-8-sig", errors="replace")


def get_json(url: str, **kw):
    return json.loads(get_text(url, **kw))


def bust(url: str) -> str:
    """キャッシュ回避用のクエリを付ける（JMA以外で使用）"""
    return url + ("&" if "?" in url else "?") + "_=%d" % int(time.time() * 1000)


_detail_cache: dict = {}
_detail_lock = threading.Lock()


def get_detail(url: str):
    """内容が変わらない詳細電文用のキャッシュ付き取得"""
    with _detail_lock:
        if url in _detail_cache:
            return _detail_cache[url]
    d = get_json(url, conditional=False)
    with _detail_lock:
        if len(_detail_cache) > 300:
            _detail_cache.clear()
        _detail_cache[url] = d
    return d


class Daily:
    """1日1回程度の再取得で足りるマスタ類"""

    def __init__(self, loader, ttl=6 * 3600, retry=600):
        self.loader, self.ttl, self.retry = loader, ttl, retry
        self.value, self.at, self.lock = None, 0.0, threading.Lock()

    def get(self):
        with self.lock:
            if self.value is None or time.time() - self.at > self.ttl:
                try:
                    self.value = self.loader()
                    self.at = time.time()
                except Exception:
                    if self.value is None:
                        raise
                    # 呼ばれるたびに再試行して警告を出し続けないよう、retry 秒後まで前回値で待つ
                    self.at = time.time() - self.ttl + self.retry
                    log.warning("マスタ再取得に失敗（前回値を使用。%d分後に再試行）", self.retry // 60)
            return self.value


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def parse_iso(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=JST)
        return t.astimezone(JST)
    except ValueError:
        return None


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def fmt_dt(t):
    return f"{t.month}/{t.day} {t:%H:%M}" if t else ""


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def nfkc(s) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).replace(" ", "").replace("\u3000", "")


def strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|dt|dd|tr|h\d|section)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def irank(v) -> int:
    return INT_RANK.get(str(v or "").strip(), 0)


def ija(v) -> str:
    return INT_JA.get(str(v or ""), str(v or ""))


def ja_of(arr, key="name") -> str:
    for x in arr or []:
        if isinstance(x, dict) and x.get("lang") == "ja":
            return x.get(key) or ""
    return ""


# ======================================================================
# 気象庁
# ======================================================================
AREA_CONST = Daily(lambda: get_json(JMA + "common/const/area.json"), ttl=24 * 3600)


def class10_of(code: str, area: dict):
    if code in area["class10s"]:
        return code
    c20 = area["class20s"].get(code)
    if c20:
        c15 = area["class15s"].get(c20.get("parent"))
        if c15:
            return c15.get("parent")
    return None


def _final_reports(lst, has_data) -> list:
    """list.json（1地震に複数の電文）を地震ごとにまとめ、最終報を採用する

    同じ eid に 震度速報 → 震源に関する情報 → 震源・震度情報 と続報が入り、震度は後の電文で
    修正されることがある。判定に使う値（震度・階級）を持つ最新の電文を確定値とし、それより
    新しい取消電文があれば誤報の取り消しとみなして地震ごと外す。
    震源名とマグニチュードは持っている最新の電文から取る（震度速報には無い）。
    返り値: [{"eid", "at", "anm", "mag", "final": 採用した電文}]（発生の新しい順）
    """
    by = {}
    for x in lst:
        by.setdefault(x.get("eid") or x.get("json"), []).append(x)
    events = []
    for eid, reps in by.items():
        # 発表時刻が同じ電文があるので、取消を先に見るところまで決めておく（並び順で結果が変わらないように）
        reps.sort(key=lambda x: (x.get("rdt") or "", x.get("ift") == "取消"), reverse=True)
        final = None
        for x in reps:  # 新しい順
            if x.get("ift") == "取消":
                break
            if has_data(x):
                final = x
                break
        if final is None:
            continue
        t = parse_iso(final.get("at")) or parse_iso(final.get("rdt"))
        if t is None:
            continue
        events.append({"eid": eid, "at": t, "final": final,
                       "anm": next((x["anm"] for x in reps if x.get("anm")), ""),
                       "mag": next((x["mag"] for x in reps if x.get("mag")), "")})
    events.sort(key=lambda e: e["at"], reverse=True)
    return events


def src_quake() -> dict:
    lst = get_json(JMA + "quake/data/list.json")
    now = now_jst()
    hold = dt.timedelta(hours=float(CFG["quake_hold_hours"]))

    national, local = [], []
    latest_any = latest_local = None
    for ev in _final_reports(lst, lambda x: bool(x.get("maxi") or x.get("int"))):
        fin = ev["final"]
        base = {"at": iso(ev["at"]), "anm": ev["anm"], "mag": ev["mag"], "maxi": fin.get("maxi") or ""}
        prefs = [p for p in fin.get("int") or [] if p.get("code") in ("23", "24")]
        if latest_any is None and irank(base["maxi"]) > 0:
            latest_any = base
        if latest_local is None and prefs:
            best = max(prefs, key=lambda p: irank(p.get("maxi")))
            latest_local = dict(base, pref=("愛知県" if best["code"] == "23" else "三重県"), pref_maxi=best.get("maxi"))
        if now - ev["at"] > hold:
            continue
        if irank(base["maxi"]) >= INT_RANK["6-"]:
            national.append(base)

        # 愛知県・三重県北中部で震度5弱以上か（最終報の値で判定）
        if not any(irank(p.get("maxi")) >= INT_RANK["5-"] for p in prefs):
            continue
        areas = []
        try:
            d = get_detail(JMA + "quake/data/" + fin["json"])
            obs = ((d.get("Body") or {}).get("Intensity") or {}).get("Observation") or {}
            for pref in as_list(obs.get("Pref")):
                for a in as_list(pref.get("Area")):
                    if a.get("Code") in QUAKE_AREAS and irank(a.get("MaxInt")) >= INT_RANK["5-"]:
                        areas.append({"name": a.get("Name") or QUAKE_AREAS[a["Code"]], "int": a.get("MaxInt")})
        except Exception as e:
            log.warning("地震詳細の取得に失敗: %s", e)
            for p in prefs:
                if irank(p.get("maxi")) >= INT_RANK["5-"]:
                    if p.get("code") == "23":
                        areas.append({"name": "愛知県", "int": p.get("maxi")})
                    elif p.get("code") == "24":
                        areas.append({"name": "三重県（区域は確認中）", "int": p.get("maxi")})
        if areas:
            local.append(dict(base, areas=areas))

    return {"national": national, "local": local, "latest_any": latest_any, "latest_local": latest_local}


def src_ltpgm() -> dict:
    lst = get_json(JMA + "ltpgm/data/list.json")
    now = now_jst()
    hold = dt.timedelta(hours=float(CFG["quake_hold_hours"]))
    hits = []
    for ev in _final_reports(lst, lambda x: x.get("lg") is not None):
        if now - ev["at"] > hold:
            continue
        areas = []
        for g in ev["final"].get("lg") or []:
            lg = str(g.get("maxLg") or "0")
            if g.get("code") in QUAKE_AREAS and lg.isdigit() and int(lg) >= 3:
                areas.append({"name": QUAKE_AREAS[g["code"]], "lg": lg})
        if areas:
            hits.append({"at": iso(ev["at"]), "anm": ev["anm"], "mag": ev["mag"], "areas": areas})
    return {"hits": hits}


def _height_text(mh: dict) -> str:
    if not isinstance(mh, dict):
        return ""
    h = mh.get("TsunamiHeight")
    if isinstance(h, dict):
        h = h.get("description") or h.get("value") or ""
    if h:
        h = str(h)
        num = h.replace("+", "").replace("<", "").replace(">", "").replace("≧", "")
        s = f"{num}m" if re.fullmatch(r"[\d.]+", num) else num
        if ">" in h:
            s += "超"
        elif "<" in h:
            s += "未満"
        return s
    cond = mh.get("Condition")
    return str(cond) if cond else ""


def src_tsunami() -> dict:
    lst = get_json(JMA + "tsunami/data/list.json")
    cands = [x for x in as_list(lst) if "VTSE41" in (x.get("json") or "")]
    if not cands:
        return {"items": [], "report_at": None}
    cands.sort(key=lambda x: x.get("rdt") or "", reverse=True)
    latest = cands[0]
    rt = parse_iso(latest.get("rdt"))
    if latest.get("ift") == "取消" or (rt and now_jst() - rt > dt.timedelta(days=3)):
        return {"items": [], "report_at": iso(rt)}
    d = get_detail(JMA + "tsunami/data/" + latest["json"])
    fc = ((d.get("Body") or {}).get("Tsunami") or {}).get("Forecast") or {}
    items = []
    for it in as_list(fc.get("Item")):
        area = (it.get("Area") or {}).get("Name", "")
        if area not in TSUNAMI_AREAS:
            continue
        kind = (((it.get("Category") or {}).get("Kind")) or {}).get("Name", "")
        items.append({"area": area, "kind": kind.replace("：発表", ""), "height": _height_text(it.get("MaxHeight"))})
    return {"items": items, "report_at": iso(rt)}


def src_warning() -> dict:
    area = AREA_CONST.get()
    lv5, lv4, wx = {}, {}, {}
    latest = None

    def add(bucket, name, c10name, c20name):
        b = bucket.setdefault(name, {"regions": [], "places": []})
        if c10name and c10name not in b["regions"]:
            b["regions"].append(c10name)
        if c20name and c20name not in b["places"]:
            b["places"].append(c20name)

    for pref in ("230000", "240000"):
        data = get_json(JMA + f"warning/data/r8/{pref}.json")
        for rep in as_list(data):
            if rep.get("infoType") == "取消":
                continue
            t = parse_iso(rep.get("reportDatetime"))
            if t and (latest is None or t > latest):
                latest = t
            w = rep.get("warning") or {}
            for key in ("class10Items", "class20Items"):
                for item in w.get(key) or []:
                    code = item.get("areaCode", "")
                    c10 = class10_of(code, area)
                    if c10 not in TARGET_CLASS10:
                        continue
                    c10name = TARGET_CLASS10[c10]
                    c20name = area["class20s"].get(code, {}).get("name") if key == "class20Items" else None
                    for k in item.get("kinds") or []:
                        kc, st = str(k.get("code") or ""), k.get("status") or ""
                        if not kc or st == "解除":
                            continue
                        if kc in LV5_CODES:
                            add(lv5, LV5_CODES[kc], c10name, c20name)
                        elif kc in LV4_CODES:
                            add(lv4, LV4_CODES[kc], c10name, c20name)
                        elif kc in WX_SPECIAL_CODES:
                            add(wx, WX_SPECIAL_CODES[kc], c10name, c20name)

    # 河川氾濫（指定河川洪水予報）
    fl = get_json(JMA + "flood/data/r8/flood_xml.json")
    for n in as_list(fl):
        item = n.get("item") or {}
        kc = str(item.get("code") or "")
        c10s = [c for c in (n.get("class10Codes") or []) if c in TARGET_CLASS10]
        if not c10s or kc not in FLOOD_LV5 | FLOOD_LV4:
            continue
        river = ""
        for key in ("riverName", "name", "title", "headTitle"):
            v = n.get(key) or item.get(key)
            if isinstance(v, str) and v:
                river = v
                break
        name = ("レベル5氾濫特別警報" if kc in FLOOD_LV5 else "レベル4氾濫危険警報")
        bucket = lv5 if kc in FLOOD_LV5 else lv4
        for c in c10s:
            add(bucket, name, TARGET_CLASS10[c], river or None)

    def pack(b):
        return [{"name": k, "regions": v["regions"], "places": v["places"]} for k, v in b.items()]

    return {"lv5": pack(lv5), "lv4": pack(lv4), "wx": pack(wx), "report_at": iso(latest)}


# ======================================================================
# 鉄道
# ======================================================================
JR = "https://traininfo.jr-central.co.jp/zairaisen/data/"
JR_MASTER = Daily(lambda: get_json(JR + "hp_senku_master_ja.json"), ttl=24 * 3600)


def rail_level(status_names):
    if any("見合わせ" in s for s in status_names):
        return "stop"
    if status_names:
        return "delay"
    return "normal"


def _jr_line(target: str, master: list):
    """config の路線指定を路線マスタと突き合わせ、(マスタ行, 表示用の基本項目) を返す"""
    nt = nfkc(target)
    m = next((x for x in master
              if nfkc(f"{x['ryokakuSenkuMei']}({x['ryokakuSenkuKaishiShuryoEki']})") == nt
              or nfkc(x["ryokakuSenkuMei"]) == nt), None)
    name = m["ryokakuSenkuMei"] if m else target.split("(")[0]
    return m, {"name": name, "sub": m["ryokakuSenkuKaishiShuryoEki"] if (m and name == "東海道線") else "",
               "code": (m or {}).get("ekiNumberKigobu", ""), "color": (m or {}).get("ryokakuSenkuColorCdchi", "#888")}


def src_jr() -> dict:
    master = JR_MASTER.get().get("lst", [])
    # JRサイト自身が「提供停止（メンテナンス）」を出しているときは、その旨をそのまま出す
    # 提供状態は運行情報の本体ではないので、取れなくても通常処理に進む
    try:
        svc = get_json(bust(JR + "hp_service_jotai_kanri.json"), conditional=False)
    except Exception as e:
        log.warning("JRの提供状態が取得できません（通常処理を続行）: %s", e)
        svc = {}
    if str((svc or {}).get("serviceJotai", "0")) != "0":
        lines = [dict(_jr_line(t, master)[1], level="offhours", status="情報提供停止中", detail="")
                 for t in CFG["jr_lines"]]
        return {"lines": lines, "info": "JR東海の運行情報はメンテナンスのため提供停止中です"}
    d = get_json(bust(JR + "trainInfo/json/unkou.json"), conditional=False)
    events = d.get("events") or []
    msgs = d.get("message_info") or []
    aligned = len(msgs) == len(events)
    lines = []
    for target in CFG["jr_lines"]:
        m, base = _jr_line(target, master)
        name = base["name"]
        keys = {nfkc(target), nfkc(name)}
        if m:
            keys.add(nfkc(f"{m['ryokakuSenkuMei']}({m['ryokakuSenkuKaishiShuryoEki']})"))
        statuses, details = [], []
        for i, e in enumerate(events):
            imp = [nfkc(x.get("name")) for x in e.get("imp_line") or [] if x.get("lang") == "ja"]
            if not keys.intersection(imp):
                continue
            st = [x.get("name") for x in e.get("status") or [] if x.get("lang") == "ja" and x.get("name")]
            statuses += st
            msg = ""
            if aligned:
                msg = ja_of((msgs[i] or {}).get("delivery_msg"), "message")
            if not msg:
                sec, cause = ja_of(e.get("accident_sec")), ja_of(e.get("cause"))
                msg = "　".join(v for v in (sec, cause) if v)
            extra = ja_of(e.get("prospect_txt")) or e.get("resume_txt") or ""
            text = "　".join(v for v in ((st[0] if st else ""), msg, extra) if v)
            if text:
                details.append(text)
        # 同じ路線に「遅れ」と「運転見合わせ」が並ぶことがある。level（見合わせがあれば stop）と
        # 表示する status が食い違わないよう、見合わせを優先する
        status = next((s for s in statuses if "見合わせ" in s), statuses[0] if statuses else "平常運転")
        lines.append(dict(base, level=rail_level(statuses), status=status, detail=" / ".join(details)))
    return {"lines": lines}


SUBWAY_COLORS = {"H_LINE": "#F5A200", "M_LINE": "#8B4FA0", "T_LINE": "#0099D9", "S_LINE": "#E60012", "K_LINE": "#E85298"}
SUBWAY_CODES = {"H_LINE": "H", "M_LINE": "M", "T_LINE": "T", "S_LINE": "S", "K_LINE": "K"}
SUBWAY_RANK = {"N_T_ICO": 0, "W_T_ICO": 1, "C_T_ICO": 2, "E_T_ICO": 3}


def src_subway() -> dict:
    d = get_json(bust("https://www.kotsu.city.nagoya.jp/datas/latest_traffic.json"), conditional=False)
    lines = []
    for rid, name in CFG["subway_lines"].items():
        rows = [x for x in d if x.get("rosen_id") == rid]

        def rank(x):
            r = SUBWAY_RANK.get(x.get("icon_cd"), -1)
            if x.get("icon_cd") == "N_T_ICO" and x.get("traffic_title") != "平常運行":
                r += 0.5
            return r

        best = max(rows, key=rank) if rows else None
        if not best:
            lines.append({"name": name, "code": SUBWAY_CODES.get(rid, ""), "color": SUBWAY_COLORS.get(rid, "#888"),
                          "level": "unknown", "status": "情報なし", "detail": ""})
            continue
        icon, title = best.get("icon_cd"), best.get("traffic_title") or ""
        level = {"W_T_ICO": "delay", "C_T_ICO": "delay", "E_T_ICO": "stop"}.get(icon, "normal")
        if level == "normal" and title and title != "平常運行":
            level = "info"
        detail = ""
        if level != "normal":
            detail = "　".join(v for v in (best.get("traffic_section"), best.get("traffic_cause"),
                                          best.get("traffic_message")) if v)
        # 交通局は「平常運行」（バスと共通の言い方）。表示は鉄道各社に合わせて「平常運転」に統一する
        status = "平常運転" if level == "normal" else (title or "運行情報あり")
        lines.append({"name": name, "code": SUBWAY_CODES.get(rid, ""), "color": SUBWAY_COLORS.get(rid, "#888"),
                      "level": level, "status": status, "detail": detail})
    return {"lines": lines}


MEITETSU_CODES = {"名古屋本線": "NH", "常滑線": "TA", "河和線": "KC", "犬山線": "IY", "瀬戸線": "ST"}
# 名鉄の全線区。事由の先頭が線区名かどうかの判定に使う（「強風のため名古屋本線 …」を線区名と誤認しないため）
MEITETSU_LINES = ("名古屋本線", "豊川線", "西尾線", "蒲郡線", "三河線", "豊田線", "常滑線", "空港線", "築港線",
                  "河和線", "知多新線", "犬山線", "各務原線", "広見線", "小牧線", "津島線", "尾西線", "竹鼻線",
                  "羽島線", "瀬戸線")
# 完全に止まっていれば赤（stop）、一部運休・遅れ・運転再開など動いていれば黄（delay）。
# 各社自身の分類に合わせている（2026-09-13 に各社サイトの語彙を確認）：
#   JR東海  運転見合わせ ／ 遅れあり・運休あり・遅れ・運休あり（JRのサイトも「運休あり」は遅延側に集約）
#   名鉄    emLv01「運転見合せ」 ／ emLv02「遅延・一部運休」（名鉄は「見合せ」と送り仮名なしで書く）
#   あおなみ線  自由文「全区間において運転を見合わせております」
# よって「運休」は赤の条件に入れない。「見合せ／見合わせ」の表記ゆれは吸収する
STOP_RE = re.compile(r"見合わ?せ|不通")
# 備考欄の定型文（画面に出しても判断材料にならない）
MT_NOISE_RE = re.compile(r"列車走行位置|特別車両券|払いもどし|ご利用の駅によっては|をご覧ください|をご確認ください")


def _em_table(block: str) -> dict:
    """異常時ブロックの表を {見出し: 中身のHTML} で返す（路線／理由／備考）"""
    rows = {}
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", block, re.S):
        body = tr.group(1)
        th = re.search(r"<th[^>]*>(.*?)</th>", body, re.S)
        td = re.search(r"<td[^>]*>(.*?)</td>", body, re.S)
        if th and td:
            rows[nfkc(strip_tags(th.group(1)))] = td.group(1)
    return rows


def _em_items(part: str) -> list:
    """表のセルから1件ずつ取り出す（<dt>/<li> 単位、<br> は別の件として分ける）"""
    items = []
    chunks = [m.group(2) for m in re.finditer(r"<(dt|li)\b[^>]*>(.*?)</\1>", part or "", re.S)] or [part or ""]
    for chunk in chunks:
        for piece in re.split(r"(?i)<br\s*/?>", chunk):
            t = " ".join(strip_tags(piece).split())
            if t and not MT_NOISE_RE.search(t) and t not in items:
                items.append(t)
    return items


def _em_line_of(item: str) -> str:
    """「三河線 土橋駅～上挙母駅間 人身事故」→「三河線」

    先頭の語に含まれる既知の線区名だけを返す（「知多新線・河和線」→そのまま、「全線」→「全線」、
    「強風のため名古屋本線」→「名古屋本線」、線区名を含まなければ空）
    """
    m = re.match(r"(\S+?線)(?:\s|$)", item)
    if not m:
        return ""
    tok = m.group(1)
    if tok == "全線":
        return tok
    found = sorted((ln for ln in MEITETSU_LINES if ln in tok), key=tok.index)
    return "・".join(found)


def _em_blocks(seg: str) -> list:
    """運行情報ページの異常時ブロックを読む

    平常時  <p class="emLv00">15分以上の列車の遅れはございません。</p>
    異常時  <div class="emInfo emLv02"> の中に
              <h2>遅延・一部運休</h2>                       … 状態
              <ul class="emListLine"><li>名古屋本線</li>…   … 対象線区（波及して遅れている線区も含む）
              表の「理由」  <dt>三河線 土橋駅～上挙母駅間 人身事故</dt> … 事由（発生線区が先頭）
              表の「備考」  <span>…再開しました。<br>…</span>             … 補足（1件ずつ<br>区切り）
            振替輸送・バス代行は「路線」がなく「区間」の表になる
    レベル番号（名鉄の CSS の定義。停止判定はこれを優先する）
            01 運転見合せ  02 遅延・一部運休  03 振替輸送  04 バス代行輸送  05 その他項目欄
    """
    out = []
    for bm in re.finditer(r'class="emInfo\s+emLv(\d+)"(.*?)(?=class="emInfo\s+emLv\d+"|\Z)', seg, re.S):
        if bm.group(1) == "00":
            continue
        body = bm.group(2)
        hm = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.S)
        rows = _em_table(body)
        names = []
        if "路線" in rows:
            lm = re.search(r'<ul class="emListLine">(.*?)</ul>', rows["路線"], re.S)
            raw = re.findall(r"<li[^>]*>(.*?)</li>", lm.group(1), re.S) if lm else strip_tags(rows["路線"]).splitlines()
            names = [n for n in (" ".join(strip_tags(x).split()) for x in raw) if n]
        out.append({
            "level": bm.group(1),
            "state": " ".join(strip_tags(hm.group(1)).split()) if hm else "運行情報あり",
            "names": names,
            "reasons": _em_items(rows.get("理由", "")),
            "notes": _em_items(rows.get("備考", "")),
            "sections": _em_items(rows.get("区間", "")),
        })
    return out


def _em_stop(block: dict) -> bool:
    """ブロックが運転見合せか。名鉄のレベル番号（emLv01）を優先し、見出しの文言は保険"""
    return block.get("level") == "01" or bool(STOP_RE.search(block["state"]))


def _cause_short(item: str) -> str:
    """「三河線 土橋駅～上挙母駅間 人身事故」→「三河線 人身事故」"""
    parts = item.split()
    line = _em_line_of(item) or parts[0]
    return f"{line} {parts[-1]}" if len(parts) >= 2 else item


def src_meitetsu() -> dict:
    """名鉄は線区別のデータがないため、運行情報ページのHTML構造から読み取る

    「路線」には事故の起きた線区だけでなく、直通運転で遅れが波及した線区も並ぶ。
    表示線区ごとに、事由がその線区のものか（直接）、他線区からの波及かを分けて出す。
    """
    h = get_text(bust("https://top.meitetsu.co.jp/em/"), conditional=False)
    m = re.search(r'<div class="layEm">(.*?)<div class="wrapEmAdd01"', h, re.S)
    if not m:
        m = re.search(r"<main>(.*?)</main>", h, re.S)
    seg = m.group(1) if m else ""

    blocks = _em_blocks(seg)
    # 平常の証拠（定型文 か emLv00）が無ければ、ページ構造が変わったとみなす
    if not blocks and "遅れはございません" not in strip_tags(seg) and not re.search(r'class="[^"]*emLv00"', seg):
        raise RuntimeError("名鉄の運行情報を読み取れません（ページ構造の変更）")

    targets = [x for x in (CFG.get("meitetsu_lines") or []) if x]
    is_target = lambda ln: bool(ln) and any(t in ln or ln in t for t in targets)
    # 名鉄サイトは 0:31〜4:59 に平常時の文言を「運行情報サービスの提供時間は、AM5:00〜AM0:30です。」に
    # 差し替える。ただし差し替えはページ内のJSが #descriptionText を書き換えているだけで、取得した
    # HTMLには常に emLv00 の平常文言が入っている（JSは実行されないので構造変更の例外にはならない）。
    # そのためサイトのJSと同じ条件をここで判定する。境界の「> 30」（0:30 は時間内）もJSに合わせる
    now = now_jst()
    off_hours = (1 <= now.hour < 5) or (now.hour == 0 and now.minute > 30)
    lines = []
    for name in targets:
        row = {"name": name, "code": MEITETSU_CODES.get(name, ""), "color": "#E60012",
               "level": "normal", "status": "平常運転", "detail": ""}
        hits = [b for b in blocks if any(n == "全線" or name in n for n in b["names"])]
        # 同じ線区が「運転見合せ」と「遅延」の両方に載ることがある。深刻な方を先頭にして、
        # status（先頭ブロックの見出し）と level（全ブロックのOR）が食い違わないようにする
        hits.sort(key=lambda b: 0 if _em_stop(b) else 1)
        if off_hours and not blocks:
            row.update(level="offhours", status="情報提供時間外")
        elif hits:
            stop = any(_em_stop(b) for b in hits)
            direct = [r for b in hits for r in b["reasons"] if name in _em_line_of(r) or _em_line_of(r) == "全線"]
            own_notes = [n for b in hits for n in b["notes"] if name in _em_line_of(n)]
            if direct:
                detail = "　".join(dict.fromkeys(direct + own_notes))
                stop = stop or any(STOP_RE.search(x) for x in direct)
            else:  # 他線区で起きた事由の波及
                causes = [_cause_short(r) for b in hits for r in b["reasons"] if _em_line_of(r)]
                # 「強風のため」など線区名で始まらない事由は波及元が書けないので、そのまま出す
                plain = [r for b in hits for r in b["reasons"] if not _em_line_of(r)]
                parts = ["、".join(dict.fromkeys(causes)) + "の影響"] if causes else []
                parts += list(dict.fromkeys(plain))
                detail = "　".join(parts)
                if own_notes:
                    detail = "　".join([detail] + own_notes) if detail else "　".join(own_notes)
            row.update(level="stop" if stop else "delay", status=hits[0]["state"], detail=detail[:200])
        lines.append(row)

    msgs = []
    affected_targets = any(l["level"] != "normal" for l in lines)
    # 表示していない線区で起きた事由（波及元の場所が分かるように1回だけ出す）
    # 線区名で始まらない事由と「全線 …」は、影響を受けた線区の行にそのまま出しているので繰り返さない
    other_reasons = [r for b in blocks for r in b["reasons"]
                     if not is_target(_em_line_of(r)) and _em_line_of(r) != "全線"
                     and (_em_line_of(r) or not affected_targets)]
    if other_reasons:
        shown = list(dict.fromkeys(other_reasons))
        msgs.append(("事由：" if affected_targets else "表示中以外の線区：") + "／".join(shown[:3]) + (" ほか" if len(shown) > 3 else ""))
    elif not affected_targets:
        others = [n for b in blocks for n in b["names"] if n != "全線" and not is_target(n)]
        if others:
            others = list(dict.fromkeys(others))
            msgs.append("表示中以外の線区で遅れ・運休があります（%s）" % "・".join(others[:8] + (["ほか"] if len(others) > 8 else [])))
    # 振替輸送・バス代行など（終了済みの区間は出さない）
    for b in blocks:
        if b["names"]:
            continue
        live = [x for x in (b["sections"] or b["notes"] or b["reasons"]) if "終了" not in x]
        if live:
            msgs.append(f"{b['state']}：" + "／".join(live[:3]) + (" ほか" if len(live) > 3 else ""))

    notice = ""
    try:
        notice = strip_tags(get_text(bust("https://top.meitetsu.co.jp/tokudashi/tokudashi.html"), conditional=False)).strip()
    except Exception:
        pass
    info = "名鉄の運行情報の提供時間は 5:00〜0:30 です" if (off_hours and not blocks) else ""
    return {"lines": lines, "other": " ／ ".join(msgs)[:300], "info": info, "notice": notice[:300]}


def src_aonami() -> dict:
    h = get_text(bust("https://www.aonamiline.co.jp/railinfo"), conditional=False)
    text = ""
    m = re.search(r'<table class="delay_table">(.*?)</table>', h, re.S)
    if m:
        row = re.search(r"<td>(.*?)</td>", m.group(1), re.S)
        if row:
            text = strip_tags(row.group(1))
    if not text:
        m = re.search(r'<span class="rit02">(.*?)</span>', h, re.S)
        text = strip_tags(m.group(1)) if m else ""
    # どちらの形でも読めなければ、平常と異常の区別がつかない。名鉄と同じく構造変更として扱う
    # （「遅延」で出すと、実際には平常なのに黄色く見えてしまう）
    if not text:
        raise RuntimeError("あおなみ線の運行情報を読み取れません（ページ構造の変更）")
    normal = "平常通り" in text
    level = "normal" if normal else ("stop" if STOP_RE.search(text) else "delay")
    return {"level": level, "status": "平常運転" if normal else (text.split("\n")[0][:40] or "情報なし"),
            "detail": "" if normal else " ".join(text.split("\n"))[:300]}


# ======================================================================
# 道路
# ======================================================================
IH = "https://ihighway.jp/datas/json/"
IH_CATS = {  # 表示名, 深刻度
    "closed": ("通行止", "stop"), "ramp": ("出入口閉鎖", "stop"),
    "accident": ("事故", "delay"), "jam": ("渋滞", "delay"), "oneLane": ("片側交互通行", "delay"),
    "broken": ("故障車", "delay"), "falling": ("落下物", "delay"),
    "snowChain": ("チェーン規制", "delay"), "snowTires": ("冬用タイヤ規制", "delay"),
    "rainCaution": ("大雨警戒", "delay"),
    "laneRestriction": ("車線規制", "info"), "underRegulation": ("規制中", "info"), "speed": ("速度規制", "info"),
    "underAdjustment": ("調整中", "info"), "snowPlow": ("除雪作業", "info"), "antifreeze": ("凍結防止剤散布", "info"),
}
LEVEL_ORDER = {"stop": 0, "delay": 1, "info": 2, "normal": 3, "unknown": 4}


def _ic_base(name: str) -> str:
    s = nfkc(name)
    s = re.sub(r"PA|SA|スマート|JCT|IC|・", "", s)
    return s


def _load_isewangan_points():
    d = get_json(IH + "icInfoApp.json")
    pts = {}
    for area in d.values():
        if not isinstance(area, dict):
            continue
        for x in area.values():
            if isinstance(x, dict) and x.get("roadName") == "伊勢湾岸道" and "pointX" in x:
                pts[_ic_base(x["icName"])] = x["pointX"]
    if len(pts) < 5:
        raise RuntimeError("伊勢湾岸道のIC情報が取得できません")

    def mid(a, b):
        return (pts[a] + pts[b]) / 2 if a in pts and b in pts else None

    ranges = {k: (v, v) for k, v in pts.items()}
    extra = {
        "名港東大橋": ("東海", "名港潮見"), "名港中央大橋": ("名港潮見", "名港中央"), "名港西大橋": ("名港中央", "飛島"),
        "木曽川": ("弥富木曽岬", "湾岸長島"), "揖斐川": ("湾岸長島", "湾岸桑名"), "長良川": ("湾岸長島", "湾岸桑名"),
    }
    for k, (a, b) in extra.items():
        v = mid(a, b)
        if v is not None:
            ranges[k] = (v, v)
    if "東海" in pts and "飛島" in pts:
        ranges["名港トリトン"] = (min(pts["東海"], pts["飛島"]), max(pts["東海"], pts["飛島"]))
    if "みえ朝日" in pts:
        ranges["四日市"] = (pts["みえ朝日"] - 25, pts["みえ朝日"] - 25)
    return ranges


IW_POINTS = Daily(_load_isewangan_points, ttl=24 * 3600)


def _title_range(title: str, ranges: dict):
    t = nfkc(title)
    xs = []
    for key in sorted(ranges, key=len, reverse=True):
        if key and key in t:
            xs += list(ranges[key])
            t = t.replace(key, "")
    return (min(xs), max(xs)) if xs else None


def _coord_range(coord):
    xs = []
    for c in as_list(coord):
        if isinstance(c, dict):
            for k in ("startX", "endX"):
                if isinstance(c.get(k), (int, float)):
                    xs.append(c[k])
    return (min(xs), max(xs)) if xs else None


def _ih_title(title: str, info: dict) -> str:
    """渋滞は「〇〇付近を先頭に 3km」の形にする（distance は km）"""
    km = str(info.get("distance") or "").strip()
    if not km or km in ("0", "0.0"):
        return title
    if not title:
        return km + "km"
    return f"{title} {km}km" if "→" in title else f"{title}を先頭に {km}km"


def _ih_detail(info: dict) -> str:
    """規制内容・事由と、渋滞の通過所要時間をまとめる"""
    parts = [str(info[k]) for k in ("detail", "reason") if info.get(k)]
    p = info.get("passing") or {}
    if p.get("time"):
        sec = f"（{p['section']}）" if p.get("section") else ""
        parts.append(f"通過 {p['time']}{sec}")
    return "　".join(parts)


def src_ihighway() -> dict:
    ranges = IW_POINTS.get()
    east = ranges.get(nfkc(CFG["isewangan_east"]))
    west = ranges.get(nfkc(CFG["isewangan_west"]))
    # 設定のIC名が NEXCO の表記と一致しないとき（「東海JCT」など）は理由が分かるように止める
    missing = [CFG[k] for k, v in (("isewangan_east", east), ("isewangan_west", west)) if v is None]
    if missing:
        raise RuntimeError("伊勢湾岸道のIC名が見つかりません（config.json）: %s" % "、".join(missing))
    lo, hi = min(west[0], east[0]) - 4, max(west[1], east[1]) + 4
    d = get_json(IH + "traffic.json")
    items, seen = [], set()

    def iter_infos(entry):
        for i in entry.get("info") or []:
            yield i, i.get("coordinate")
        for ic in entry.get("ic") or []:
            for i in ic.get("info") or []:
                yield i, ic.get("coordinate") or i.get("coordinate")

    for area in d.values():
        if not isinstance(area, dict):
            continue
        for group in ("trafficInfo", "otherTrafficInfo"):
            for cat, roads in (area.get(group) or {}).items():
                if not isinstance(roads, list):
                    continue
                label, level = IH_CATS.get(cat, (cat, "info"))
                for r in roads:
                    if nfkc(r.get("roadName")) != "伊勢湾岸道":
                        continue
                    for info, coord in iter_infos(r):
                        title = info.get("title") or ""
                        rng = _coord_range(coord) or _title_range(title, ranges)
                        if rng and (rng[1] < lo or rng[0] > hi):
                            continue  # 監視区間外
                        key = (cat, title, info.get("direction"))
                        if key in seen:
                            continue
                        seen.add(key)
                        items.append({"kind": label, "level": level, "title": _ih_title(title, info),
                                      "direction": (info.get("direction") or "").replace("上り：", "上り ").replace("下り：", "下り "),
                                      "detail": _ih_detail(info), "located": bool(rng)})
    items.sort(key=lambda x: (LEVEL_ORDER.get(x["level"], 9), x["title"]))
    return {"items": items}


MK = "https://www.cbr.mlit.go.jp/meikoku/cms/"


def src_meikoku() -> dict:
    items = []
    k = get_text(bust(MK + "kisei/?view=index"), conditional=False)
    for li in re.finditer(r"<li([^>]*)>(.*?)</li>", k, re.S):
        attrs, body = li.group(1), li.group(2)
        if "none" in attrs or "_def" in attrs:
            continue
        txt = strip_tags(body).replace("\n", " ")
        if txt and "現在情報はありません" not in txt:
            items.append({"kind": "規制", "level": "delay", "title": txt[:120], "direction": "", "detail": ""})
    now = now_jst()
    g = get_text(bust(MK + "kinkyu/?view=index"), conditional=False)
    for li in re.finditer(r"<li[^>]*id=['\"](\d{14})['\"][^>]*>(.*?)</li>", g, re.S):
        try:
            t = dt.datetime.strptime(li.group(1), "%Y%m%d%H%M%S").replace(tzinfo=JST)
        except ValueError:
            continue
        if now - t > dt.timedelta(hours=24):
            continue
        m = re.search(r"title=['\"]([^'\"]+)['\"]", li.group(2))
        title = html.unescape(m.group(1)) if m else strip_tags(li.group(2))
        items.append({"kind": "緊急情報", "level": "info", "title": title[:120],
                      "direction": fmt_dt(t), "detail": "名古屋国道事務所"})
    return {"items": items}


# ======================================================================
# 収集スケジューラ
# ======================================================================
SOURCES = {
    # key: (関数, 表示名, 間隔キー)
    "quake": (src_quake, "気象庁 地震", "quake"),
    "ltpgm": (src_ltpgm, "気象庁 長周期", "ltpgm"),
    "tsunami": (src_tsunami, "気象庁 津波", "tsunami"),
    "warning": (src_warning, "気象庁 警報", "warning"),
    "jr": (src_jr, "JR東海", "jr"),
    "subway": (src_subway, "地下鉄", "subway"),
    "meitetsu": (src_meitetsu, "名鉄", "meitetsu"),
    "aonami": (src_aonami, "あおなみ線", "aonami"),
    "ihighway": (src_ihighway, "NEXCO中日本", "ihighway"),
    "meikoku": (src_meikoku, "名古屋国道", "meikoku"),
}
if not CFG.get("enable_route23"):
    SOURCES.pop("meikoku")

STATE: dict = {}
STATE_LOCK = threading.Lock()


def run_source(key: str):
    func, label, ikey = SOURCES[key]
    default = DEFAULT_CONFIG["intervals"].get(ikey, 60)
    try:
        interval = max(10, int(CFG["intervals"].get(ikey, default)))
    except (AttributeError, TypeError, ValueError):
        # ここで例外を出すとこの系統のスレッドだけが黙って止まる。既定値で続ける
        log.error("config.json の intervals.%s が数値ではありません（既定の %d 秒で続行）", ikey, default)
        interval = default
    while True:
        started = time.time()
        try:
            value = func()
            with STATE_LOCK:
                STATE[key] = {"value": value, "updated": time.time(), "error": None,
                              "failed_at": None, "interval": interval}
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            # 通信・構造変更（RuntimeError/OSError/ValueError）は一行で十分。それ以外はコードの不具合の
            # 可能性が高いので、原因を追えるように traceback も残す
            expected = isinstance(e, (RuntimeError, OSError, ValueError))
            log.warning("[%s] 取得失敗 %s", label, msg, exc_info=not expected)
            with STATE_LOCK:
                ent = STATE.setdefault(key, {"value": None, "updated": None, "interval": interval})
                ent.update(error=msg, failed_at=time.time(), interval=interval)
        time.sleep(max(3, interval - (time.time() - started)))


def source_view(key: str):
    with STATE_LOCK:
        ent = dict(STATE.get(key) or {})
    upd = ent.get("updated")
    interval = ent.get("interval") or 60
    stale = upd is None or (time.time() - upd) > max(interval * 3, 180)
    return ent.get("value"), {
        "key": key, "name": SOURCES[key][1],
        "updated": iso(dt.datetime.fromtimestamp(upd, JST)) if upd else None,
        "stale": stale, "error": ent.get("error"),
    }


# ======================================================================
# 表示用にまとめる
# ======================================================================
def _quake_line(e) -> str:
    t = parse_iso(e.get("at"))
    mag = f" M{e['mag']}" if e.get("mag") else ""
    return f"{fmt_dt(t)}　{e.get('anm') or '震源調査中'}{mag}"


def _join_places(w) -> str:
    regions = "・".join(w["regions"])
    if w["places"]:
        pl = "、".join(w["places"][:6]) + (f" ほか{len(w['places']) - 6}" if len(w["places"]) > 6 else "")
        return f"{regions}（{pl}）"
    return regions


def build_alerts():
    q, sq = source_view("quake")
    lg, slg = source_view("ltpgm")
    ts, sts = source_view("tsunami")
    wa, swa = source_view("warning")
    hold = CFG["quake_hold_hours"]
    alerts = []

    # 1) 地震（愛知県・三重県北中部）
    lines = []
    for e in (q or {}).get("local", []):
        ar = "、".join(f"{a['name']} 震度{ija(a['int'])}" for a in e["areas"])
        lines.append({"main": ar, "sub": _quake_line(e)})
    for e in (lg or {}).get("hits", []):
        ar = "、".join(f"{a['name']} 階級{a['lg']}" for a in e["areas"])
        lines.append({"main": f"長周期地震動 {ar}", "sub": _quake_line(e)})
    note = ""
    ll = (q or {}).get("latest_local")
    if ll:
        note = f"県内の直近の揺れ　{fmt_dt(parse_iso(ll['at']))} {ll['anm'] or ''}　{ll['pref']} 震度{ija(ll['pref_maxi'])}"
    alerts.append({
        "id": "quake_local", "title": "地震", "scope": "愛知県・三重県北中部",
        "criteria": "震度5弱以上　長周期地震動階級3以上",
        "level": "alarm" if lines else "normal", "lines": lines, "note": note,
        "hold": f"発生から{hold}時間表示", "stale": sq["stale"] or slg["stale"],
    })

    # 2) 津波
    lines, level = [], "normal"
    for it in (ts or {}).get("items", []):
        big = "大津波警報" in it["kind"] and "解除" not in it["kind"]
        if big:
            level = "alarm"
        elif ("津波警報" in it["kind"] or "津波注意報" in it["kind"]) and "解除" not in it["kind"] and level != "alarm":
            level = "caution"
        else:
            continue
        h = f"　予想される最大波 {it['height']}" if it["height"] else ""
        lines.append({"main": it["kind"], "sub": f"{it['area']}{h}"})
    alerts.append({
        "id": "tsunami", "title": "津波", "scope": "伊勢・三河湾　愛知県外海",
        "criteria": "予想最大波 3m超（大津波警報）", "level": level, "lines": lines,
        "note": "", "stale": sts["stale"],
    })

    # 3) レベル5特別警報
    lines = [{"main": w["name"], "sub": _join_places(w)} for w in (wa or {}).get("lv5", [])]
    lines4 = [{"main": w["name"], "sub": _join_places(w)} for w in (wa or {}).get("lv4", [])]
    level = "alarm" if lines else ("caution" if lines4 else "normal")
    alerts.append({
        "id": "lv5", "title": "レベル5特別警報", "scope": "愛知県・三重県北中部",
        "criteria": "河川氾濫　大雨　土砂災害　高潮",
        "level": level, "lines": lines + lines4, "note": "", "stale": swa["stale"],
    })

    # 4) 特別警報（気象）
    lines = [{"main": w["name"], "sub": _join_places(w)} for w in (wa or {}).get("wx", [])]
    alerts.append({
        "id": "wx", "title": "特別警報（気象）", "scope": "愛知県・三重県北中部",
        "criteria": "暴風　波浪　暴風雪　大雪", "level": "alarm" if lines else "normal",
        "lines": lines, "note": "", "stale": swa["stale"],
    })

    # 5) 全国 震度6弱以上
    lines = [{"main": f"最大震度{ija(e['maxi'])}", "sub": _quake_line(e)} for e in (q or {}).get("national", [])]
    la = (q or {}).get("latest_any")
    note = f"直近の地震　{_quake_line(la)}　最大震度{ija(la['maxi'])}" if la else ""
    alerts.append({
        "id": "quake_national", "title": "全国の地震", "scope": "全国", "criteria": "震度6弱以上",
        "level": "alarm" if lines else "normal", "lines": lines, "note": note,
        "hold": f"発生から{hold}時間表示", "stale": sq["stale"],
    })
    return alerts


def build_status() -> dict:
    srcs = []

    def view(key):
        v, s = source_view(key)
        srcs.append(s)
        return v or {}, s

    jr, sjr = view("jr")
    sub, ssub = view("subway")
    mt, smt = view("meitetsu")
    ao, sao = view("aonami")
    ih, sih = view("ihighway")
    mk, smk = view("meikoku") if "meikoku" in SOURCES else ({}, None)
    for k in ("quake", "ltpgm", "tsunami", "warning"):
        view(k)

    return {
        "now": iso(now_jst()),
        "alerts": build_alerts(),
        "rail": {
            "jr": {"lines": jr.get("lines", []), "info": jr.get("info", ""), "src": sjr},
            "subway": {"lines": sub.get("lines", []), "src": ssub},
            "meitetsu": dict(mt, src=smt),
            "aonami": dict(ao, src=sao),
        },
        "road": {
            "isewangan": {"items": ih.get("items", []), "src": sih,
                          "section": f"{CFG['isewangan_east']}JCT〜{CFG['isewangan_west']}IC"},
            "route23": ({"items": mk.get("items", []), "src": smk,
                         "notes": [n for n in CFG.get("route23_notes") or [] if n]}
                        if "meikoku" in SOURCES else None),
        },
        "sources": srcs,
    }


# ======================================================================
# HTTP
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "bousai-board/1.0"

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                self._send(200, json.dumps(build_status(), ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path == "/api/config":
                cfg = {"google_maps_api_key": CFG.get("google_maps_api_key") or ""}
                self._send(200, json.dumps(cfg).encode(), "application/json")
            elif path in ("/", "/index.html", "/bousai_board.html"):
                with open(os.path.join(HERE, "bousai_board.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            log.error("HTTP処理エラー: %s", e)
            self._send(500, str(e).encode(), "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):  # アクセスログは出さない
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m/%d %H:%M:%S")
    # 常駐運用では画面を見ていないため、ログをファイルにも残す（再起動の繰り返しなどに気づけるように）
    try:
        fh = logging.handlers.RotatingFileHandler(os.path.join(HERE, "board.log"),
                                                  maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m/%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)
    except Exception as e:
        log.warning("ログファイルを開けません（画面表示のみ）: %s", e)
    # スレッドの想定外の停止は既定では stderr にしか出ない（最小化した窓では見えない）
    threading.excepthook = lambda a: log.error(
        "スレッドが停止しました\n%s", "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)).strip())
    for i, key in enumerate(SOURCES):
        th = threading.Thread(target=lambda k=key, d=i: (time.sleep(d * 0.7), run_source(k)), daemon=True)
        th.start()
    try:
        srv = ThreadingHTTPServer((CFG["host"], int(CFG["port"])), Handler)
    except OSError as e:
        # ポート使用中（二重起動）や権限不足。run_server.bat が再起動を繰り返すので、理由をログに残す
        log.error("起動できません（ポート %s が使用中か、権限がありません）: %s", CFG["port"], e)
        sys.exit(1)
    log.info("起動しました  http://%s:%s/", CFG["host"], CFG["port"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("終了します")


if __name__ == "__main__":
    main()
