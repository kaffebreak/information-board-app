#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名古屋・四日市 風向風速ボード。Python 3.9+ / 標準ライブラリのみ。"""
from __future__ import annotations

import copy
import datetime as dt
from html.parser import HTMLParser
import json
import logging
import logging.handlers
import math
import os
import re
import threading
import unicodedata
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from bousai_board import HERE, JST, Handler as BaseHandler, http_get, load_config, now_jst, parse_iso

log = logging.getLogger("wind")
CFG = load_config()
CFG.setdefault("wind_port", 8766)
CFG.setdefault("wind_interval", 60)
CFG.setdefault("wind_caution_ms", 10)
HISTORY_PATH = os.path.join(HERE, "kaze_history.json")
DIRECTIONS = "北 北北東 北東 東北東 東 東南東 南東 南南東 南 南南西 南西 西南西 西 西北西 北西 北北西".split()
STATIONS = {
    "nagoya": {"name": "名古屋", "detail": "高潮防波堤 中央堤東端", "minutes": 15,
               "url": "https://www6.kaiho.mlit.go.jp/nagoyako/kisyou/nagoyako_bw.html"},
    "yokkaichi": {"name": "四日市", "detail": "防波堤信号所", "minutes": 30,
                  "url": "https://www6.kaiho.mlit.go.jp/04kanku/yokkaichi/yokkaichiko_bkw_lt/kisyou/index.html"},
}
LOCK = threading.RLock()
STATE = {key: {"rows": [], "updated": None, "failing": False, "partial": False} for key in STATIONS}
HISTORY = {key: [] for key in STATIONS}
HISTORY_ERROR = False


def wind_log_handler(directory=HERE):
    # 別プロセスの防災ログとはファイルも世代バックアップも共有しない。
    return logging.handlers.RotatingFileHandler(
        os.path.join(directory, "kaze_board.log"), maxBytes=1_000_000,
        backupCount=3, encoding="utf-8", delay=True)


class ObservationTable(HTMLParser):
    """セル内の span と空欄を維持し、観測表だけを抽出する。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row, self.cell = None, None


def parse_observations(document):
    parser = ObservationTable()
    parser.feed(document)
    observations = {}
    partial = False
    headers = False
    for cells in parser.rows:
        cells = [unicodedata.normalize("NFKC", c).strip() for c in cells]
        if cells[:4] == ["日付", "時刻", "風向", "風速"]:
            headers = True
            continue
        if not cells or not re.match(r"\d{4}/", cells[0]):
            continue
        if len(cells) != 4:
            partial = True
            continue
        try:
            at = dt.datetime.strptime(cells[0] + " " + cells[1], "%Y/%m/%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            partial = True
            continue
        direction = cells[2] if cells[2] in DIRECTIONS else None
        match = re.fullmatch(r"(\d+)\s*m(?:/s)?", cells[3])
        speed = int(match[1]) if match else None
        calm = cells[3] == "風弱く"
        kind = "calm" if calm else "normal" if speed is not None else "missing"
        if calm:
            direction = None
        if kind == "missing" or (kind == "normal" and direction is None):
            partial = True
        observations[at.isoformat()] = {
            "at": at.isoformat(), "direction": direction,
            "degrees": DIRECTIONS.index(direction) * 22.5 if direction else None,
            "speed": speed, "kind": kind,
        }
    if not headers or not observations:
        raise ValueError("観測表が見つかりません（ページ構造または日時の変更）")
    return {"rows": sorted(observations.values(), key=lambda r: r["at"]), "partial": partial}


def source(key):
    return parse_observations(http_get(STATIONS[key]["url"], conditional=True).decode("utf-8-sig"))


def src_nagoya():
    return source("nagoya")


def src_yokkaichi():
    return source("yokkaichi")


def merge_history(old, new, now):
    # 壁時計で保持期間を区切り、停止・取得失敗後にも古い履歴を24時間と呼ばない。
    cutoff = now - dt.timedelta(hours=24)
    merged = {r["at"]: r for r in old + new if cutoff <= parse_iso(r["at"]) <= now}
    return sorted(merged.values(), key=lambda r: r["at"])


def load_history(path=HISTORY_PATH):
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        clean = {}
        for key in STATIONS:
            rows = saved.get(key, [])
            for row in rows:
                if not isinstance(row, dict) or not parse_iso(row.get("at")):
                    raise ValueError("履歴の日時が不正")
                if row.get("kind") not in ("normal", "calm", "missing"):
                    raise ValueError("履歴の観測種別が不正")
                speed, direction = row.get("speed"), row.get("direction")
                if speed is not None and (type(speed) is not int or speed < 0):
                    raise ValueError("履歴の風速が不正")
                if direction is not None and direction not in DIRECTIONS:
                    raise ValueError("履歴の風向が不正")
                row["degrees"] = DIRECTIONS.index(direction) * 22.5 if direction else None
                if row["kind"] == "normal" and speed is None:
                    raise ValueError("履歴の風速がありません")
            clean[key] = merge_history([], rows, now_jst())
        with LOCK:
            HISTORY.update(clean)
            for key, rows in clean.items():
                STATE[key].update(rows=rows, failing=bool(rows))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        log.warning("風向履歴を読み込めません: %s", exc)


def persist_history(path=HISTORY_PATH):
    # 呼び出し側で LOCK を保持し、2局の更新による保存順序の逆転を防ぐ。
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(HISTORY, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def update_source(key):
    global HISTORY_ERROR
    try:
        result = SOURCES[key]()
        now = now_jst()
        if any(parse_iso(r["at"]) > now + dt.timedelta(minutes=2) for r in result["rows"]):
            raise ValueError("未来の観測時刻が含まれています")
        with LOCK:
            previous = STATE[key]["rows"]
            if previous and result["rows"][-1]["at"] < previous[-1]["at"]:
                raise ValueError("観測時刻が前回より古いため前回値を保持")
            STATE[key].update(result, updated=now.isoformat(), failing=False)
            for station in STATIONS:
                HISTORY[station] = merge_history(HISTORY[station], result["rows"] if station == key else [], now)
            try:
                persist_history()
                HISTORY_ERROR = False
            except OSError as exc:
                HISTORY_ERROR = True
                log.warning("風向履歴の保存失敗（観測の更新は継続）: %s", exc)
        log.info("%s 風向風速 更新 %s", STATIONS[key]["name"], result["rows"][-1]["at"])
    except Exception as exc:
        with LOCK:
            STATE[key]["failing"] = True
        log.warning("%s 取得失敗（前回値を保持）: %s", key, exc)


SOURCES = {"nagoya": src_nagoya, "yokkaichi": src_yokkaichi}


def run_source(key):
    while True:
        update_source(key)
        threading.Event().wait(max(15, float(CFG["wind_interval"])))


def beaufort(speed):
    # 気象庁 風力階級表 https://www.jma.go.jp/jma/kishou/know/yougo_hp/kaze.html
    return sum(speed >= boundary for boundary in (0.3, 1.6, 3.4, 5.5, 8, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7))


def station_view(key, state, history, now):
    meta = STATIONS[key]
    rows = state["rows"]
    latest = rows[-1] if rows else None
    end = parse_iso(latest["at"]) if latest else now
    age = max(0, (now - end).total_seconds() / 60) if latest else None
    unknown = latest is None or latest["kind"] == "missing"
    stale = latest is None or age > meta["minutes"] * 2 + 5
    failing = state["failing"] or (age is not None and age > meta["minutes"] + 5)
    start = end - dt.timedelta(hours=6)
    series = [r for r in rows if start <= parse_iso(r["at"]) <= end]
    # 弱風に数値はない。0はグラフ専用の表現で、統計は数値観測だけで計算する。
    numeric = [r for r in series if r["speed"] is not None]
    maximum = max(numeric, key=lambda r: (r["speed"], r["at"])) if numeric else None
    expected = 360 // meta["minutes"] + 1
    hist = merge_history([], history, now)
    valid = [r for r in hist if r["kind"] == "normal" and r["direction"] and r["speed"] is not None]
    bins = [[0, 0, 0] for _ in DIRECTIONS]
    for row in valid:
        bins[DIRECTIONS.index(row["direction"])][0 if row["speed"] < 5 else 1 if row["speed"] < 10 else 2] += 1
    frequencies = [[count / len(valid) * 100 for count in group] for group in bins] if valid else bins
    speed = latest["speed"] if latest else None
    span = min(24, (parse_iso(hist[-1]["at"]) - parse_iso(hist[0]["at"])).total_seconds() / 3600) if len(hist) > 1 else 0
    rose_expected = round(span * 60 / meta["minutes"]) + 1 if hist else 0
    # 弱風は正常な観測。欠測行と風向不明の数値行は、行数が揃っていても欠測と伝える。
    rose_missing = max(0, rose_expected - len(hist)) + sum(
        r["kind"] == "missing" or (r["kind"] == "normal" and not r["direction"])
        for r in hist)
    return dict(meta, id=key, latest=latest, observed_at=latest["at"] if latest else None,
                age_minutes=int(age) if age is not None else None,
                next_at=(end + dt.timedelta(minutes=meta["minutes"])).isoformat() if latest else None,
                updated=state["updated"], failing=bool(failing), partial=state["partial"],
                unknown=unknown, stale=stale,
                knots=round(speed * 1.94384, 1) if speed is not None else None,
                force=beaufort(speed) if speed is not None else None,
                series=series, start=start.isoformat(), end=end.isoformat(),
                maximum=maximum, average=round(sum(r["speed"] for r in numeric) / len(numeric), 1) if numeric else None,
                numeric_count=len(numeric), expected_count=expected,
                calm_count=sum(r["kind"] == "calm" for r in series),
                missing_count=expected - len([r for r in series if r["kind"] != "missing"]),
                rose={"bins": frequencies, "count": len(valid), "hours": round(span, 1),
                      "start": hist[0]["at"] if hist else None, "end": hist[-1]["at"] if hist else None,
                      "samples": len(hist), "expected": rose_expected, "missing_count": rose_missing,
                      "calm_count": sum(r["kind"] == "calm" for r in hist)})


def build_status(now=None):
    now = now or now_jst()
    with LOCK:
        states, histories, history_error = copy.deepcopy(STATE), copy.deepcopy(HISTORY), HISTORY_ERROR
    return {"now": now.isoformat(), "caution_ms": CFG["wind_caution_ms"], "history_error": history_error,
            "stations": [station_view(key, states[key], histories[key], now) for key in STATIONS]}


class Handler(BaseHandler):
    server_version = "kaze-board/1.0"

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/api/status", "/api/config"):
                value = build_status() if path == "/api/status" else {"board": "wind", "port": CFG["wind_port"]}
                self._send(200, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif path in ("/", "/index.html", "/kaze_board.html"):
                with open(os.path.join(HERE, "kaze_board.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("HTTP処理エラー")
            self._send(500, b"internal server error", "text/plain")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [wind] %(message)s")
    try:
        handler = wind_log_handler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [wind] %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError as exc:
        log.warning("ログを開けません: %s", exc)
    if int(CFG["wind_port"]) == int(CFG["port"]):
        raise ValueError("wind_port は防災ボードの port と別にしてください")
    threshold = float(CFG["wind_caution_ms"])
    if not math.isfinite(threshold) or not 0 < threshold <= 20:
        raise ValueError("wind_caution_ms は0より大きく20以下にしてください（固定軸）")
    CFG["wind_caution_ms"] = threshold
    interval = float(CFG["wind_interval"])
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("wind_interval は正の秒数を指定してください")
    CFG["wind_interval"] = max(15, interval)
    server = ThreadingHTTPServer((CFG["host"], int(CFG["wind_port"])), Handler)
    load_history()
    for key in STATIONS:
        threading.Thread(target=run_source, args=(key,), daemon=True).start()
    log.info("起動 http://%s:%s/", CFG["host"], CFG["wind_port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
