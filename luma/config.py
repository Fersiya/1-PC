import json
import os
from pathlib import Path
import socket
import sys


APP_NAME = "LumaLink"
APP_SUBTITLE = "Синхронизация подсветки экрана"
WS_PORT = 8000
DISCOVERY_PORT = 8001

DEFAULT_CONFIG = {
    "device_id": "",
    "local_key": "",
    "device_uuid": "",
    "port": WS_PORT,
    "protocol_version": 4,
    "dp_switch": 1,
    "dp_mode": 2,
    "dp_colour": 24,
    "fps": 14,
    "brightness": 100,
    "smoothing": 45,
    "capture_mode": "fullscreen",
}


def get_config_path() -> Path:
    exe_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent  # папка с lumalink.py
    )
    local_cfg = exe_dir / "config.json"
    if local_cfg.exists():
        return local_cfg

    appdata = os.environ.get("APPDATA")
    base_dir = Path(appdata) / APP_NAME if appdata else Path.home() / f".{APP_NAME.lower()}"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "config.json"


CONFIG_PATH = get_config_path()


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_local_ips() -> list[str]:
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)
