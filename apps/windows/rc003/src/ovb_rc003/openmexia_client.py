"""OpenMexia 本机联动客户端（纯 Python，不依赖 Qt）。

OpenMexia 启动后会在 ``%APPDATA%\\OpenMexia\\mote-deck.json`` 写一份连接信息
（回环地址、随机端口、随机 token）；本模块只做三件事：读那份文件、调它的
HTTP 接口、把遥控器按键动作丢给后台线程执行，绝不阻塞 Raw Input 回调。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

PROTOCOL_NAME = "openmexia-local-api"
PROTOCOL_VERSION = 1
DISCOVERY_FILE_NAME = "mote-deck.json"
OPENMEXIA_APP_DIR = "OpenMexia"
REQUEST_TIMEOUT_SECONDS = 3.0
# 连接后自动加的那条软件配置：名字显示在软件栏，进程名让它前台时也能对上。
APP_PROFILE_NAME = "OpenMexia"
PROCESS_NAME = "openmexia.exe"


class OpenMexiaError(RuntimeError):
    """OpenMexia 没连上、或者拒绝了这次请求。"""


def appdata_root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    return Path(base)


def discovery_path() -> Path:
    """OpenMexia 写在本机的那份连接信息。"""

    return appdata_root() / OPENMEXIA_APP_DIR / DISCOVERY_FILE_NAME


@dataclass(frozen=True)
class OpenMexiaControl:
    """遥控器上可选的一个「设备 · 操作」。"""

    device: str
    device_name: str
    op: str
    label: str


@dataclass(frozen=True)
class OpenMexiaDevice:
    id: str
    name: str
    type: str
    online: bool
    controls: Tuple[OpenMexiaControl, ...]


class OpenMexiaClient:
    """读 discovery 文件 + 调本地 HTTP 接口。"""

    def __init__(
        self,
        discovery_file: Optional[Path] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._discovery_file = (
            Path(discovery_file) if discovery_file else discovery_path()
        )
        self._timeout = timeout

    def discovery(self) -> Optional[Dict[str, Any]]:
        """返回有效的连接信息，没有或文件损坏时返回 ``None``。"""

        try:
            data = json.loads(self._discovery_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("protocol") != PROTOCOL_NAME:
            return None
        try:
            host = str(data["host"]).strip()
            port = int(data["port"])
            token = str(data["token"]).strip()
        except (KeyError, TypeError, ValueError):
            return None
        if not host or port <= 0 or not token:
            return None
        return {
            "host": host,
            "port": port,
            "token": token,
            "appVersion": str(data.get("appVersion", "")),
        }

    def _request(
        self, method: str, path: str, payload: Optional[dict] = None
    ) -> Dict[str, Any]:
        info = self.discovery()
        if info is None:
            raise OpenMexiaError("没有找到 OpenMexia 的连接信息，请先启动 OpenMexia")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://{info['host']}:{info['port']}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {info['token']}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            message = ""
            try:
                message = str(json.loads(exc.read().decode("utf-8", "replace")).get("error", ""))
            except (OSError, ValueError, AttributeError):
                message = ""
            raise OpenMexiaError(message or f"OpenMexia 返回 {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OpenMexiaError("连不上 OpenMexia，请确认它正在运行") from exc
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise OpenMexiaError("OpenMexia 返回了无法解析的内容") from exc
        return value if isinstance(value, dict) else {}

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/status")

    def devices(self) -> List[OpenMexiaDevice]:
        payload = self._request("GET", "/v1/devices")
        result: List[OpenMexiaDevice] = []
        raw_devices = payload.get("devices")
        if not isinstance(raw_devices, list):
            return result
        for raw in raw_devices:
            if not isinstance(raw, dict):
                continue
            device_id = str(raw.get("id") or "").strip()
            if not device_id:
                continue
            device_name = str(raw.get("name") or "").strip()
            controls: List[OpenMexiaControl] = []
            raw_controls = raw.get("controls")
            if isinstance(raw_controls, list):
                for raw_control in raw_controls:
                    if not isinstance(raw_control, dict):
                        continue
                    op = str(raw_control.get("op") or "").strip()
                    op_label = str(raw_control.get("label") or "").strip()
                    if not op or not op_label:
                        continue
                    controls.append(
                        OpenMexiaControl(
                            device=device_id,
                            device_name=device_name,
                            op=op,
                            label=f"{device_name or device_id} · {op_label}",
                        )
                    )
            result.append(
                OpenMexiaDevice(
                    id=device_id,
                    name=device_name,
                    type=str(raw.get("type") or "").strip(),
                    online=raw.get("online") is not False,
                    controls=tuple(controls),
                )
            )
        return result

    def perform(self, device: str, op: str) -> None:
        self._request("POST", "/v1/action", {"device": device, "op": op})

    def say(self, text: str) -> None:
        """把一句识别出来的话交给 OpenMexia 自己解析成设备动作。"""

        self._request("POST", "/v1/say", {"text": text})


class OpenMexiaRunner:
    """后台线程执行按键动作（网络请求不能跑在输入回调里）。"""

    def __init__(self, client: Optional[OpenMexiaClient] = None) -> None:
        self._client = client or OpenMexiaClient()
        self._queue: "queue.Queue[Optional[Tuple[str, str, str]]]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="openmexia-actions", daemon=True
        )
        self._thread.start()

    def submit(self, device: str, op: str) -> None:
        """排队执行一个设备动作；调用方立即返回。"""

        self._queue.put(("action", str(device), str(op)))

    def submit_text(self, text: str) -> None:
        """排队执行一句语音指令；调用方立即返回。"""

        value = str(text).strip()
        if value:
            self._queue.put(("say", value, ""))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, first, second = item
            try:
                if kind == "say":
                    self._client.say(first)
                else:
                    self._client.perform(first, second)
            except OpenMexiaError as exc:
                _LOGGER.info(
                    "openmexia %s failed: first=%s second=%s reason=%s",
                    kind,
                    first,
                    second,
                    exc,
                )
            except BaseException as exc:  # noqa: BLE001 - 不能拖垮输入线程
                _LOGGER.warning(
                    "openmexia %s error: first=%s error_type=%s",
                    kind,
                    first,
                    type(exc).__name__,
                )

    def close(self) -> None:
        self._queue.put(None)
