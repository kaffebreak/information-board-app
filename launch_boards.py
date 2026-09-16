"""設定で有効なサーバーの応答を待ち、各画面を独立して起動する。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = Path(__file__).resolve().parent
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def config():
    try:
        return json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def edge_path():
    found = shutil.which("msedge")
    if found:
        return found
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        candidate = Path(root or ".") / "Microsoft/Edge/Application/msedge.exe"
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Microsoft Edge が見つかりません")


def supervise(script):
    while True:
        subprocess.call([sys.executable, str(HERE / script)], cwd=HERE, creationflags=HIDDEN)
        time.sleep(10)


def wait_and_open(browser, key, port, host, position):
    url = f"http://{host}:{port}/"
    while True:
        try:
            with urllib.request.urlopen(url + "api/config", timeout=2) as response:
                data = json.load(response)
            if key == "wind" and data.get("board") != "wind":
                raise ValueError("別のサーバーが応答しました")
            break
        except (OSError, ValueError):
            time.sleep(1)
    args = [browser, "--kiosk", url, "--edge-kiosk-type=fullscreen", "--no-first-run",
            "--user-data-dir=" + str(HERE / (".edge-" + key)), "--window-size=1920,1080"]
    if position is not None:
        args.append("--window-position=" + ",".join(map(str, position)))
    subprocess.Popen(args, cwd=HERE)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--server":
        supervise({"bousai": "bousai_board.py", "wind": "kaze_board.py"}[sys.argv[2]])
        return
    cfg = config()
    enable_wind = cfg.get("enable_wind", True)
    if type(enable_wind) is not bool:
        raise ValueError("enable_wind は true または false を指定してください")
    ports = {"bousai": int(cfg.get("port", 8765))}
    if enable_wind:
        ports["wind"] = int(cfg.get("wind_port", 8766))
        if ports["bousai"] == ports["wind"]:
            raise ValueError("port と wind_port は別にしてください")
    for key in ports:
        position = cfg.get(key + "_window_position")
        if position is not None and (not isinstance(position, list) or len(position) != 2 or any(type(x) is not int for x in position)):
            raise ValueError(key + "_window_position は [x, y] または null を指定してください")
    browser = edge_path()
    host = cfg.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    threads = []
    for key, port in ports.items():
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/config", timeout=2) as response:
                json.load(response)
        except (OSError, ValueError):
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--server", key], cwd=HERE,
                             creationflags=HIDDEN, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        thread = threading.Thread(target=wait_and_open, args=(browser, key, port, host, cfg.get(key + "_window_position")))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
