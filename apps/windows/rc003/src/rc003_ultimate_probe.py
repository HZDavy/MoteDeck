"""RC003「终极开麦测试向导」——把“语音键能不能不拆机改位置”的所有可能性一次测完。

这个程序只做诊断，不进正式产品路径。它会引导你按顺序跑完 14 步实验，
每一步自动判定并写入报告，最后导出 JSON + 可读文本，方便回传分析。

覆盖的实验：
  1  连接 + GATT 全表枚举（判定 ATVV 服务、小米私有 AF00 服务是否存在）
  2  ATVV 能力协商 0x03（PTT + HTT）
  3  ATVV 能力协商 0x00（On-request，旧探针用的那种）
  4  负对照：发无效 opcode（0x77 / 0xFF），看遥控器是不是“发什么都回”
  5  负对照：没开麦时发 MIC_CLOSE，看是否也有 AUDIO_STOP 回应
  6  主机主动 MIC_OPEN，2 秒后自动 MIC_CLOSE
  7  主机主动 MIC_OPEN，持续 10 秒不关
  7b 切回 PTT 模式（0x03）后，主机主动 MIC_OPEN 2 秒（对比 On-request 模式）
  8  物理按键基线（证明电平表本身能工作）
  9  按住物理键的同时主机发 MIC_CLOSE —— 区分“固件状态门”和“硬件门控”
  10 小米私有服务 AF00 / MMA 0xD0 帧（若第 1 步发现该服务）
  11 自由探测：任意特征读 / 写 / 订阅
  12 导出报告
  13 清理并断开

运行前请先退出正式桥接程序（托盘退出），否则同一台遥控器会被占用：
    cd d:\\无线麦\\windows-remote-mic-app\\apps\\windows\\rc003
    .\\.venv\\Scripts\\python.exe src\\rc003_ultimate_probe.py
"""

from __future__ import annotations

import asyncio
import json
import math
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ovb_rc003 import atvv_protocol as proto  # noqa: E402
from ovb_rc003 import atvv_session, ble_transport_winrt, identity  # noqa: E402

# ------------------------------------------------------------------ 常量

ATVV_SERVICE = proto.VOICE_SERVICE_UUID.casefold()
ATVV_TX = proto.VOICE_TX_UUID.casefold()
ATVV_AUDIO = proto.VOICE_AUDIO_UUID.casefold()
ATVV_CONTROL = proto.VOICE_CONTROL_UUID.casefold()

AF00_SERVICE = "0000af00-0000-1000-8000-00805f9b34fb"
AF05_CHAR = "0000af05-0000-1000-8000-00805f9b34fb"
AF06_CHAR = "0000af06-0000-1000-8000-00805f9b34fb"

REPORT_DIR = Path(r"d:\无线麦")

BLOCKS = 48
METER_WIDTH = 560
METER_HEIGHT = 56
UI_TICK_MS = 50
LOG_TICK_MS = 80
MAX_LOG_LINES = 1200

PROP_BITS = (
    ("READ", 0x2),
    ("WRITE", 0x8),
    ("WRITE_NO_RSP", 0x4),
    ("NOTIFY", 0x10),
    ("INDICATE", 0x20),
    ("BROADCAST", 0x1),
    ("EXTENDED", 0x80),
    ("RELIABLE_WRITE", 0x100),
    ("SIGNED_WRITE", 0x40),
    ("WRITABLE_AUX", 0x200),
)

# 小米 MMA 私有帧：FE DC BA + opcode(2) + length(2) + params + EF
MMA_FRAMES: List[tuple] = [
    ("MMA 00D0（语音开始，大端写法）", bytes.fromhex("FEDCBA00D00000EF")),
    ("MMA D000（语音开始，小端写法）", bytes.fromhex("FEDCBAD0000000EF")),
    ("MMA 40D0（语音开始，需回应位）", bytes.fromhex("FEDCBA40D00000EF")),
    ("MMA 00D3（长按开始）", bytes.fromhex("FEDCBA00D30000EF")),
    ("MMA 00D1（语音停止）", bytes.fromhex("FEDCBA00D10000EF")),
]


def short_uuid(value: Any) -> str:
    """把 128 位 UUID 压成蓝牙标准 16 位短名，方便人眼读表。"""
    text = str(value).casefold()
    if text.startswith("0000") and text.endswith("-0000-1000-8000-00805f9b34fb"):
        return text[4:8]
    return text


def prop_text(flags: int) -> str:
    names = [name for name, bit in PROP_BITS if flags & bit]
    return "|".join(names) if names else "NONE"


def hex_text(data: bytes, limit: int = 64) -> str:
    text = bytes(data).hex(" ").upper()
    if len(data) > limit:
        return text[: limit * 3] + f" …(共 {len(data)} B)"
    return text


# ------------------------------------------------------------------ 后台循环


class LoopThread:
    """在后台线程里跑一个独立 asyncio 事件循环，供 WinRT 异步调用使用。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="rc003-ultimate-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass


# ------------------------------------------------------------------ 计量与事件


class MeterState:
    """BLE 工作线程写入、Tk 线程读取的电平 / 计数快照。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rms = 0.0
        self.peak = 0
        self.frames = 0
        self.samples = 0
        self.last_audio_at = 0.0
        self.notifications = 0
        self.notification_bytes = 0
        self.mic_open = False
        self.caps_text = "-"

    def reset(self) -> None:
        with self.lock:
            self.rms = 0.0
            self.peak = 0
            self.frames = 0
            self.samples = 0
            self.last_audio_at = 0.0
            self.notifications = 0
            self.notification_bytes = 0

    def add_notification(self, nbytes: int) -> None:
        with self.lock:
            self.notifications += 1
            self.notification_bytes += int(nbytes)

    def add_samples(self, samples) -> None:
        if not samples:
            return
        peak = max(abs(sample) for sample in samples)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        with self.lock:
            self.rms = rms
            self.peak = max(self.peak, peak)
            self.frames += 1
            self.samples += len(samples)
            self.last_audio_at = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "rms": self.rms,
                "peak": self.peak,
                "frames": self.frames,
                "samples": self.samples,
                "last_audio_at": self.last_audio_at,
                "notifications": self.notifications,
                "notification_bytes": self.notification_bytes,
                "mic_open": self.mic_open,
            }


@dataclass
class LinkEvent:
    """一条链路层事件（控制通知 / 音频通知 / 任意特征通知）。"""

    seq: int
    t: float
    channel: str
    name: str
    opcode: Optional[int]
    payload: bytes
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "t": round(self.t, 4),
            "channel": self.channel,
            "name": self.name,
            "opcode": self.opcode,
            "payload_hex": bytes(self.payload).hex().upper(),
            "detail": self.detail,
        }


class WinRT:
    """把需要的 WinRT 投影集中到一个对象里，缺包时给出明确报错。"""

    def __init__(self) -> None:
        self.modules = ble_transport_winrt._import_winrt()
        try:
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattCharacteristicProperties,
                GattWriteOption,
            )
        except ImportError as exc:  # pragma: no cover
            raise ble_transport_winrt.WinRTUnavailableError(str(exc)) from exc
        self.props = GattCharacteristicProperties
        self.write_option = GattWriteOption


# ------------------------------------------------------------------ BLE 链路


class BleLink:
    """一个只读优先、可任意读写订阅的 GATT 链路封装。

    与产品用的 RC003BleSession 的区别：
      * 枚举**整张** GATT 表，而不是只找 ATVV；
      * 允许对任意特征做原始读 / 写 / 订阅，用于私有协议与负对照实验；
      * 音频字节在链路层计数（ATVVSession 在未开麦时会直接丢弃音频，
        只看它的返回值会把“硬件发了但主机丢掉了”误判成“根本没发”）。
    """

    def __init__(
        self,
        winrt: WinRT,
        meter: MeterState,
        log: Callable[[str], None],
    ) -> None:
        self.winrt = winrt
        self.meter = meter
        self.log = log
        self.device: Any = None
        self.device_name = ""
        self.services: List[Dict[str, Any]] = []
        self.chars: Dict[str, Any] = {}
        self.char_info: Dict[str, Dict[str, Any]] = {}
        self.tokens: Dict[str, Any] = {}
        self.session = atvv_session.ATVVSession()
        self.events: List[LinkEvent] = []
        self.version = 0
        self.session_id = 0
        self._seq = 0
        self._status_token: Any = None
        self.disconnected = False

    # ------------------------------------------------------------ 连接/枚举

    async def connect(self, candidate: identity.RC003Candidate) -> None:
        device = await self.winrt.modules.bluetooth_le_device.from_id_async(
            candidate.handle.id
        )
        if device is None:
            raise ConnectionError("无法打开选中的设备（可能被其它程序占用）")
        self.device = device
        self.device_name = candidate.name
        try:
            self._status_token = device.add_connection_status_changed(
                self._on_status_changed
            )
        except Exception as exc:  # noqa: BLE001 - 状态回调是可选增强
            self.log(f"连接状态回调注册失败（忽略）：{type(exc).__name__}")
        self.log(f"已打开设备：{candidate.name}")

    def _on_status_changed(self, sender, args) -> None:
        try:
            status = sender.connection_status
        except Exception:  # noqa: BLE001 - 老投影用 args.status
            try:
                status = args.status
            except Exception:  # noqa: BLE001
                return
        if int(status) != 0:  # 0 = CONNECTED
            self.disconnected = True
            self.log("⚠ BLE 连接已断开（遥控器可能休眠或超出范围）")

    async def enumerate_gatt(self) -> List[Dict[str, Any]]:
        """枚举整张 GATT 表：服务 -> 特征 -> 属性 -> 可读值。"""
        winrt = self.winrt
        getter = getattr(self.device, "get_gatt_services_with_cache_mode_async", None)
        if getter is None:  # pragma: no cover - 老投影兜底
            result = await self.device.get_gatt_services_async()
        else:
            result = await getter(winrt.modules.bluetooth_cache_mode.UNCACHED)
        if int(result.status) != 0:
            raise ConnectionError(f"GATT 服务枚举失败：status={result.status}")

        self.services = []
        self.chars = {}
        self.char_info = {}
        for service in result.services:
            service_uuid = str(service.uuid).casefold()
            entry: Dict[str, Any] = {
                "uuid": service_uuid,
                "short": short_uuid(service_uuid),
                "characteristics": [],
            }
            try:
                char_result = await service.get_characteristics_with_cache_mode_async(
                    winrt.modules.bluetooth_cache_mode.UNCACHED
                )
                characteristics = list(char_result.characteristics)
            except Exception as exc:  # noqa: BLE001 - 单个服务失败不影响整表
                self.log(f"服务 {short_uuid(service_uuid)} 特征枚举失败："
                         f"{type(exc).__name__}: {exc}")
                characteristics = []

            for char in characteristics:
                char_uuid = str(char.uuid).casefold()
                try:
                    flags = int(char.characteristic_properties)
                except Exception:  # noqa: BLE001
                    flags = 0
                info = {
                    "uuid": char_uuid,
                    "short": short_uuid(char_uuid),
                    "service": entry["short"],
                    "props": prop_text(flags),
                    "flags": flags,
                    "handle": int(getattr(char, "attribute_handle", 0) or 0),
                    "value": None,
                    "read_error": None,
                }
                entry["characteristics"].append(info)
                self.chars[char_uuid] = char
                self.char_info[char_uuid] = info

                if flags & 0x2:  # READ
                    try:
                        # 某些特征需要加密链路，读操作会一直挂着；限时 3 秒避免卡死向导。
                        value = await asyncio.wait_for(self.read_char(char), 3.0)
                        info["value"] = hex_text(value, 32)
                    except Exception as exc:  # noqa: BLE001 - 读失败很常见（需配对/加密）
                        info["read_error"] = f"{type(exc).__name__}: {exc}"
            self.services.append(entry)
        return self.services

    def describe_gatt(self) -> str:
        lines: List[str] = []
        for service in self.services:
            lines.append(f"[服务] {service['uuid']}  (短名 {service['short']})")
            for char in service["characteristics"]:
                lines.append(
                    f"    {char['uuid']}  h=0x{char['handle']:04X}  [{char['props']}]"
                )
                if char.get("value"):
                    lines.append(f"        值: {char['value']}")
                if char.get("read_error"):
                    lines.append(f"        读失败: {char['read_error']}")
        return "\n".join(lines)

    def has_service(self, text: str) -> bool:
        needle = str(text).casefold()
        return any(needle in service["uuid"] for service in self.services)

    def has_char(self, text: str) -> bool:
        needle = str(text).casefold()
        return any(needle in key for key in self.chars)

    def find_char(self, text: str) -> Any:
        needle = str(text).casefold()
        for key, char in self.chars.items():
            if needle in key:
                return char
        return None

    def list_char_labels(self) -> List[str]:
        labels: List[str] = []
        for service in self.services:
            for char in service["characteristics"]:
                labels.append(
                    f"{char['uuid']}  [{char['props']}]  (服务 {char['service']})"
                )
        return labels

    # ---------------------------------------------------------- 订阅/读/写

    async def subscribe(self, text: str, channel: str) -> bool:
        char = text if hasattr(text, "uuid") else self.find_char(text)
        if char is None:
            self.log(f"订阅失败：找不到特征 {text}")
            return False
        key = str(char.uuid).casefold()
        if key in self.tokens:
            return True
        token = char.add_value_changed(
            lambda _sender, args, _ch=channel, _k=key: self._on_notify(_ch, _k, args)
        )
        self.tokens[key] = token
        status = await char.write_client_characteristic_configuration_descriptor_async(
            self.winrt.modules.cccd_value.NOTIFY
        )
        if int(status) != 0:
            self.log(f"订阅 {short_uuid(key)} 失败：status={status}")
            return False
        self.log(f"已订阅 {short_uuid(key)} → 通道 {channel}")
        return True

    def _on_notify(self, channel: str, key: str, args) -> None:
        try:
            payload = bytes(args.characteristic_value)
        except Exception as exc:  # noqa: BLE001
            self.log(f"通知解析失败：{type(exc).__name__}")
            return
        if channel == "audio":
            self.meter.add_notification(len(payload))
            try:
                samples = self.session.handle_audio(payload)
            except Exception as exc:  # noqa: BLE001
                self.log(f"音频解码异常：{type(exc).__name__}: {exc}")
                samples = []
            self.meter.add_samples(samples)
            if self.meter.notifications <= 3 or self.meter.notifications % 200 == 0:
                self._record(channel, "AUDIO_DATA", None, payload,
                             f"第 {self.meter.notifications} 条通知")
            return
        if channel == "control":
            name, opcode, detail = self._parse_control(payload)
            self._record(channel, name, opcode, payload, detail)
            return
        self._record(channel, f"NOTIFY[{short_uuid(key)}]", None, payload, "")

    def _parse_control(self, payload: bytes) -> tuple:
        try:
            event = self.session.handle_control(payload)
        except Exception as exc:  # noqa: BLE001 - 非法/异常帧也要如实记录
            return ("CONTROL_ERROR", payload[0] if payload else None,
                    f"{type(exc).__name__}: {exc}")

        if isinstance(event, atvv_session.CapsReceived):
            caps = event.capabilities
            self.version = caps.version
            detail = (
                f"version=0x{caps.version:04X} codec=0x{caps.codecs:02X} "
                f"interaction=0x{caps.interaction:02X} frame={caps.frame_size} "
                f"rate={caps.sample_rate:.0f}Hz"
            )
            self.meter.caps_text = detail
            self.meter.mic_open = False
            return ("CAPS", proto.OPCODE_CAPS, detail)
        if isinstance(event, atvv_session.AudioStarted):
            self.session_id = event.session_id or 0
            self.meter.mic_open = True
            self.meter.reset()
            return ("AUDIO_START", proto.OPCODE_AUDIO_START,
                    f"session={event.session_id} reason={event.reason} codec={event.codec}")
        if isinstance(event, atvv_session.AudioStopped):
            self.meter.mic_open = False
            return ("AUDIO_STOP", proto.OPCODE_AUDIO_STOP, f"reason={event.reason}")
        if isinstance(event, atvv_session.MicButtonPressed):
            return ("MIC_BUTTON", proto.OPCODE_MIC_BUTTON, "遥控器报告话筒键被按下")
        if isinstance(event, atvv_session.AudioSynced):
            return ("AUDIO_SYNC", proto.OPCODE_AUDIO_SYNC, "")
        if isinstance(event, atvv_session.UnknownControl):
            return ("UNKNOWN_CONTROL", event.opcode, f"opcode=0x{event.opcode:02X}")
        return ("CONTROL_EVENT", None, repr(event))

    def _record(self, channel: str, name: str, opcode: Optional[int],
                payload: bytes, detail: str = "") -> None:
        with self.meter.lock:
            self._seq += 1
            seq = self._seq
        event = LinkEvent(seq=seq, t=time.monotonic(), channel=channel, name=name,
                          opcode=opcode, payload=payload, detail=detail)
        self.events.append(event)
        line = f"← [{channel}] {name} {hex_text(payload, 24)}"
        if detail:
            line += f"  {detail}"
        self.log(line)

    def mark(self) -> int:
        return len(self.events)

    def events_since(self, mark: int) -> List[LinkEvent]:
        return self.events[mark:]

    def count_since(self, mark: int, names) -> Dict[str, int]:
        wanted = {names} if isinstance(names, str) else set(names)
        counts: Dict[str, int] = {name: 0 for name in wanted}
        for event in self.events[mark:]:
            if event.name in counts:
                counts[event.name] += 1
        return counts

    async def wait_for(self, names, timeout: float, start: Optional[int] = None):
        """等待出现指定名称的事件，返回 (事件列表, 是否超时)。"""
        mark = self.mark() if start is None else start
        deadline = time.monotonic() + timeout
        wanted = {names} if isinstance(names, str) else set(names)
        while time.monotonic() < deadline:
            found = [e for e in self.events_since(mark) if e.name in wanted]
            if found:
                return found, False
            await asyncio.sleep(0.05)
        return [], True

    async def read_char(self, char: Any) -> bytes:
        result = await char.read_value_with_cache_mode_async(
            self.winrt.modules.bluetooth_cache_mode.UNCACHED
        )
        if int(result.status) != 0:
            raise ConnectionError(f"读取失败：status={result.status}")
        reader = self.winrt.modules.data_reader_factory(result.value)
        try:
            length = int(reader.unconsumed_buffer_length)
            # 用 read_byte 逐字节读，和 battery_monitor_winrt 里已跑通的写法保持一致。
            return bytes(reader.read_byte() for _ in range(length))
        finally:
            reader.close()

    async def read(self, text: str) -> bytes:
        char = text if hasattr(text, "uuid") else self.find_char(text)
        if char is None:
            raise ConnectionError(f"找不到特征：{text}")
        data = await self.read_char(char)
        self.log(f"← 读 {short_uuid(char.uuid)} = {hex_text(data, 48)}")
        return data

    async def write_char(self, char: Any, data: bytes,
                         without_response: Optional[bool] = None) -> None:
        flags = 0
        try:
            flags = int(char.characteristic_properties)
        except Exception:  # noqa: BLE001
            pass
        if without_response is None:
            without_response = bool(flags & 0x4) and not bool(flags & 0x8)
        writer = self.winrt.modules.data_writer_factory()
        try:
            writer.write_bytes(bytes(data))
            buffer = writer.detach_buffer()
            if without_response:
                # 需要强制“无应答”时才走带 option 的重载；否则用产品里已跑通的那条路径。
                method = getattr(char, "write_value_with_result_and_option_async", None)
                if method is not None:
                    result = await method(
                        buffer,
                        self.winrt.write_option.WRITE_WITHOUT_RESPONSE,
                    )
                else:
                    result = await char.write_value_with_result_async(buffer)
            else:
                result = await char.write_value_with_result_async(buffer)
        finally:
            writer.close()
        if int(result.status) != 0:
            raise ConnectionError(f"写入失败：status={result.status}")

    async def write(self, text: str, data: bytes,
                    without_response: Optional[bool] = None) -> None:
        char = text if hasattr(text, "uuid") else self.find_char(text)
        if char is None:
            raise ConnectionError(f"找不到特征：{text}")
        await self.write_char(char, data, without_response)
        self.log(f"→ 写 {short_uuid(char.uuid)}: {hex_text(data, 32)}"
                 f"{' (无应答)' if without_response else ''}")

    # ------------------------------------------------------------- ATVV 命令

    async def send_atvv(self, data: bytes, label: str) -> None:
        await self.write(ATVV_TX, data)
        self.log(f"   ↑ {label}")

    async def mic_open(self) -> None:
        await self.send_atvv(self.session.mic_open_command(), "MIC_OPEN (0x0C)")

    async def mic_close(self) -> None:
        await self.send_atvv(self.session.mic_close_command(), "MIC_CLOSE (0x0D)")

    # ---------------------------------------------------------------- 关闭

    async def close(self) -> None:
        for key, token in list(self.tokens.items()):
            char = self.chars.get(key)
            if char is None:
                continue
            try:
                await char.write_client_characteristic_configuration_descriptor_async(
                    self.winrt.modules.cccd_value.NONE
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                char.remove_value_changed(token)
            except Exception:  # noqa: BLE001
                pass
        self.tokens.clear()
        if self.device is not None and self._status_token is not None:
            try:
                self.device.remove_connection_status_changed(self._status_token)
            except Exception:  # noqa: BLE001
                pass
            self._status_token = None
        if self.device is not None:
            try:
                self.device.close()
            except Exception:  # noqa: BLE001
                pass
            self.device = None


# ------------------------------------------------------------------ 步骤框架


@dataclass
class StepResult:
    verdict: str
    ok: Optional[bool] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StepDef:
    key: str
    title: str
    hint: str
    kind: str  # auto / manual / freeform
    runner: Callable[[Any, "StepContext"], Any]


class StepContext:
    """把一个步骤需要的能力打包传给 runner，避免 runner 直接摸 Tk。"""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.loop = app.loop_thread.loop
        self._user_event: Optional[asyncio.Event] = None

    @property
    def link(self) -> BleLink:
        return self.app.link

    @property
    def meter(self) -> MeterState:
        return self.app.meter

    def log(self, text: str) -> None:
        self.app.log(text)

    def snapshot(self) -> Dict[str, Any]:
        return self.meter.snapshot()

    async def wait_user(self, prompt: str) -> None:
        """暂停步骤，等用户在界面上点“我已做完/结束观察”。"""
        event = asyncio.Event()
        self._user_event = event
        self.app.arm_user_button(prompt)
        try:
            await event.wait()
        finally:
            self._user_event = None
            self.app.disarm_user_button()

    def user_done(self) -> None:
        event = self._user_event
        if event is not None and not event.is_set():
            self.loop.call_soon_threadsafe(event.set)


# ------------------------------------------------------------ 步骤 1：连接枚举

async def step_connect(app, ctx: StepContext) -> StepResult:
    candidate = app.selected_candidate()
    if candidate is None:
        return StepResult("没有选中设备，请先在顶部刷新并选择 RC003。", ok=False)

    if app.link is not None:
        ctx.log("已有连接，先断开再重连。")
        await app.link.close()
        app.link = None

    link = BleLink(app.winrt, app.meter, app.log)
    app.link = link
    await link.connect(candidate)

    ctx.log("开始枚举整张 GATT 表（UNCACHED，会重新向设备读取）…")
    services = await link.enumerate_gatt()
    ctx.log(f"共 {len(services)} 个服务，"
            f"{sum(len(s['characteristics']) for s in services)} 个特征。")
    for line in link.describe_gatt().splitlines():
        ctx.log(line)

    has_atvv = link.has_service(ATVV_SERVICE[:8]) or link.has_char(ATVV_CONTROL[:8])
    has_af00 = link.has_service("af00")
    has_af05 = link.has_char("af05")
    has_af06 = link.has_char("af06")

    subscribed = []
    for text, channel in ((ATVV_CONTROL, "control"), (ATVV_AUDIO, "audio")):
        if link.find_char(text) is not None:
            if await link.subscribe(text, channel):
                subscribed.append(channel)
        else:
            ctx.log(f"⚠ 未找到 {channel} 特征（{text}）")

    parts = [f"ATVV 服务：{'有' if has_atvv else '无'}",
             f"AF00 私有服务：{'有' if has_af00 else '无'}",
             f"AF05/AF06：{'有' if (has_af05 or has_af06) else '无'}",
             f"已订阅：{','.join(subscribed) or '无'}"]
    verdict = "；".join(parts)
    if not has_atvv:
        verdict += "。没有 ATVV，后续 ATVV 步骤都会失败——这台设备可能不是走 ATVV。"
    return StepResult(verdict, ok=bool(has_atvv and subscribed), details={
        "has_atvv": has_atvv, "has_af00": has_af00,
        "has_af05": has_af05, "has_af06": has_af06,
        "subscribed": subscribed,
        "gatt": link.describe_gatt(),
        "services": services,
    })


# ---------------------------------------------------- 步骤 2/3：能力协商 A/B

async def _negotiate(app, ctx: StepContext, command: bytes, label: str) -> StepResult:
    link = ctx.link
    if link is None or link.find_char(ATVV_TX) is None:
        return StepResult("没有 ATVV TX 特征，无法协商。", ok=False)
    caps_before = link.session.capabilities
    mark = link.mark()
    ctx.log(f"发送 {label}: {hex_text(command)}")
    await link.send_atvv(command, label)
    _found, timed_out = await link.wait_for("CAPS", 3.0, mark)
    if timed_out:
        return StepResult(f"{label} 后 3 秒内没有收到 CAPS 回应。", ok=False,
                          details={"events": [e.to_dict() for e in link.events_since(mark)]})
    caps = link.session.capabilities
    detail = {
        "version": f"0x{caps.version:04X}",
        "codecs": f"0x{caps.codecs:02X}",
        "interaction": f"0x{caps.interaction:02X}",
        "frame_size": caps.frame_size,
        "sample_rate": caps.sample_rate,
        "version_changed": caps_before is None or caps.version != caps_before.version,
    }
    return StepResult(
        f"{label} 成功：{detail['version']} interaction={detail['interaction']} "
        f"frame={caps.frame_size} rate={caps.sample_rate:.0f}Hz",
        ok=True, details=detail,
    )


async def step_caps_ptt(app, ctx: StepContext) -> StepResult:
    return await _negotiate(app, ctx, proto.GET_CAPABILITIES_V10,
                            "GET_CAPABILITIES（PTT+HTT 0x03）")


async def step_caps_on_request(app, ctx: StepContext) -> StepResult:
    result = await _negotiate(app, ctx, proto.GET_CAPABILITIES_ON_REQUEST_V10,
                              "GET_CAPABILITIES（On-request 0x00）")
    caps = ctx.link.session.capabilities if ctx.link else None
    if caps is not None:
        interaction = caps.interaction
        if interaction == 0x00:
            result.verdict += "。interaction 变成 0x00，说明遥控器**接受**了 On-request 模式。"
        else:
            result.verdict += (f"。interaction 仍是 0x{interaction:02X}，"
                               "说明遥控器忽略了 On-request 请求，坚持自己的模式。")
        result.details["interaction_after"] = f"0x{interaction:02X}"
    return result


# -------------------------------------------------- 步骤 4：负对照 无效 opcode

async def step_invalid_opcode(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    collected: List[Dict[str, Any]] = []
    for payload in (bytes((0x77, 0x00, 0x00)), bytes((0xFF,))):
        mark = link.mark()
        ctx.log(f"负对照：发送无效 opcode {hex_text(payload)}")
        try:
            await link.send_atvv(payload, "无效 opcode")
        except Exception as exc:  # noqa: BLE001
            collected.append({"payload": hex_text(payload),
                              "write_error": f"{type(exc).__name__}: {exc}"})
            continue
        await asyncio.sleep(2.0)
        events = link.events_since(mark)
        collected.append({
            "payload": hex_text(payload),
            "events": [e.to_dict() for e in events],
            "names": sorted({e.name for e in events}),
        })

    names = {name for item in collected for name in item.get("names", [])}
    if not names:
        verdict = ("两次无效 opcode 都没有任何回应——遥控器不是“发什么都回”，"
                   "它的回应是针对 ATVV 语义的。")
        ok = True
    elif "AUDIO_START" in names:
        verdict = ("⚠ 无效 opcode 也触发了 AUDIO_START！说明开麦与命令内容无关，"
                   "任何写入都可能触发（这会推翻“主机 MIC_OPEN 有效”的结论）。")
        ok = False
    else:
        verdict = f"无效 opcode 引发了回应：{sorted(names)}（需要人工判断是否只是错误帧）。"
        ok = None
    return StepResult(verdict, ok=ok, details={"attempts": collected})


# ------------------------------------------ 步骤 5：负对照 未开麦时 MIC_CLOSE

async def step_close_when_idle(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    if ctx.snapshot()["mic_open"]:
        return StepResult("当前麦克风处于开启状态，本步需要在“未开麦”时做，请先完成关闭。",
                          ok=False)
    mark = link.mark()
    await link.mic_close()
    await asyncio.sleep(2.0)
    events = link.events_since(mark)
    names = sorted({e.name for e in events})
    if not names:
        verdict = ("未开麦时发 MIC_CLOSE 没有任何回应——说明遥控器的回应是**状态相关**的，"
                   "不是无条件回声。这是判断 ATVV 真实性的关键证据。")
        ok = True
    elif names == ["AUDIO_STOP"]:
        verdict = ("未开麦也回了 AUDIO_STOP——遥控器会无条件确认关闭命令，"
                   "因此“收到 AUDIO_STOP”本身不能作为开麦成功的证据。")
        ok = None
    else:
        verdict = f"未开麦时发 MIC_CLOSE 收到：{names}。"
        ok = None
    return StepResult(verdict, ok=ok,
                      details={"events": [e.to_dict() for e in events]})


# ---------------------------------------------- 主机开麦结果的统一判定函数

def classify_host_open(snapshot: Dict[str, Any], got_start: bool,
                       got_stop: bool) -> StepResult:
    frames = snapshot["frames"]
    samples = snapshot["samples"]
    notifications = snapshot["notifications"]
    nbytes = snapshot["notification_bytes"]
    peak = snapshot["peak"]
    detail = {
        "audio_start": got_start, "audio_stop": got_stop,
        "pcm_frames": frames, "pcm_samples": samples, "peak": peak,
        "raw_notifications": notifications, "raw_bytes": nbytes,
    }
    if not got_start:
        return StepResult(
            "主机 MIC_OPEN 后没有 AUDIO_START —— 遥控器根本不承认主机主动开麦。",
            ok=False, details=detail)
    if samples > 0 and peak > 0:
        return StepResult(
            f"主机开麦成功：收到 {frames} 帧 / {samples} 个 PCM 样本，峰值 {peak}。"
            "电平表有读数，说明确实在收音。",
            ok=True, details=detail)
    if notifications > 0:
        return StepResult(
            f"收到 AUDIO_START 且有 {notifications} 条音频通知 / {nbytes} 字节，"
            "但主机解不出 PCM —— 音频在流动，问题在解码/帧对齐，不在遥控器。",
            ok=None, details=detail)
    return StepResult(
        "只回了 AUDIO_START，但没有任何音频字节 —— 遥控器接受了命令、"
        "打开了状态机，却没有把麦克风数据送上来（典型的固件门控）。",
        ok=False, details=detail)


# -------------------------------------------------- 步骤 6：主机开麦 2 秒

async def step_host_open_2s(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    mark = link.mark()
    ctx.meter.reset()
    ctx.log("发送主机 MIC_OPEN，2 秒后自动 MIC_CLOSE。请在这 2 秒内对着遥控器说话。")
    await link.mic_open()
    started, _ = await link.wait_for("AUDIO_START", 3.0, mark)
    await asyncio.sleep(2.0)
    snapshot = ctx.snapshot()
    close_mark = link.mark()
    await link.mic_close()
    stopped, _ = await link.wait_for("AUDIO_STOP", 2.0, close_mark)
    result = classify_host_open(snapshot, bool(started), bool(stopped))
    result.details["events"] = [e.to_dict() for e in link.events_since(mark)]
    return result


# ------------------------------------------------ 步骤 7：主机开麦持续 10 秒

async def step_host_open_hold(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    mark = link.mark()
    ctx.meter.reset()
    ctx.log("发送主机 MIC_OPEN，**不自动关闭**，观察 10 秒。请持续说话。")
    await link.mic_open()
    started, _ = await link.wait_for("AUDIO_START", 3.0, mark)

    spontaneous_stop = False
    for second in range(10):
        await asyncio.sleep(1.0)
        snap = ctx.snapshot()
        ctx.log(f"  {second + 1:>2}s  PCM 帧={snap['frames']:<5} "
                f"原始通知={snap['notifications']:<5} "
                f"电平={snap['rms']:.0f}  麦克风开={snap['mic_open']}")
        if not snap["mic_open"] and started:
            spontaneous_stop = True
            ctx.log("  ⚠ 遥控器自己发来了 AUDIO_STOP（未等主机关闭）")
            break

    snapshot = ctx.snapshot()
    if snapshot["mic_open"]:
        close_mark = link.mark()
        await link.mic_close()
        await link.wait_for("AUDIO_STOP", 2.0, close_mark)
    result = classify_host_open(snapshot, bool(started), True)
    if spontaneous_stop:
        result.verdict += " 另外：遥控器在主机未关闭时自行停止（有内置时长上限）。"
    result.details["spontaneous_stop"] = spontaneous_stop
    result.details["events"] = [e.to_dict() for e in link.events_since(mark)]
    return result


# ------------------------------------- 步骤 7b：切回 PTT 后主机开麦（对比模式）

async def step_host_open_ptt(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    neg = await _negotiate(app, ctx, proto.GET_CAPABILITIES_V10,
                           "GET_CAPABILITIES（切回 PTT 0x03）")
    if not neg.ok:
        neg.verdict = "无法切回 PTT 模式，本步无法进行：" + neg.verdict
        return neg
    interaction = link.session.capabilities.interaction
    mark = link.mark()
    ctx.meter.reset()
    ctx.log(f"当前 interaction=0x{interaction:02X}。发送主机 MIC_OPEN，2 秒后自动 MIC_CLOSE。")
    await link.mic_open()
    started, _ = await link.wait_for("AUDIO_START", 3.0, mark)
    await asyncio.sleep(2.0)
    snapshot = ctx.snapshot()
    close_mark = link.mark()
    await link.mic_close()
    stopped, _ = await link.wait_for("AUDIO_STOP", 2.0, close_mark)
    result = classify_host_open(snapshot, bool(started), bool(stopped))
    result.verdict = f"[PTT 模式 interaction=0x{interaction:02X}] " + result.verdict
    result.details["interaction_before_open"] = f"0x{interaction:02X}"
    result.details["events"] = [e.to_dict() for e in link.events_since(mark)]
    return result


# ---------------------------------------------------- 步骤 8：物理按键基线

async def step_physical_baseline(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    ctx.log("本步用来证明电平表本身是好的：请用**物理语音键**录一段。")
    mark = link.mark()
    ctx.meter.reset()
    await ctx.wait_user("请按住遥控器语音键说 5 秒话，松开后点这个按钮结束本步")
    snapshot = ctx.snapshot()
    names = sorted({e.name for e in link.events_since(mark)})
    detail = {
        "pcm_frames": snapshot["frames"], "pcm_samples": snapshot["samples"],
        "peak": snapshot["peak"], "raw_notifications": snapshot["notifications"],
        "raw_bytes": snapshot["notification_bytes"], "events": names,
    }
    if snapshot["samples"] > 0:
        verdict = (f"物理链路正常：{snapshot['frames']} 帧 / {snapshot['samples']} 样本，"
                   f"峰值 {snapshot['peak']}。电平表和 BLE 音频通路都能工作，"
                   "因此后面步骤“没有音频”是遥控器的行为，不是本程序的问题。")
        ok = True
    elif snapshot["notifications"] > 0:
        verdict = ("物理按键触发了音频通知但解不出 PCM —— 解码链路有问题，"
                   "先解决这个再判断其它步骤。")
        ok = False
    else:
        verdict = ("物理按键也没有任何音频 —— 检查是否订阅成功、遥控器是否连着、"
                   "或者按键是否真的按住了。这一步不通过，后面的结论都不可信。")
        ok = False
    return StepResult(verdict, ok=ok, details=detail)


# ---------------------------------------- 步骤 9：按住物理键 + 主机 MIC_CLOSE

async def step_hold_and_close(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    ctx.log("本步区分两种门控：固件状态门（主机能关）还是硬件门控（主机关不掉）。")
    mark_press = link.mark()
    await ctx.wait_user("请【按住】遥控器语音键不要松手，按住后点这个按钮")
    pressed, _ = await link.wait_for(("MIC_BUTTON", "AUDIO_START"), 2.0, mark_press)
    ctx.log(f"按住期间检测到：{[e.name for e in pressed] or '（还没有事件）'}")

    ctx.meter.reset()
    mark_close = link.mark()
    ctx.log("现在发送主机 MIC_CLOSE，同时请**继续按住不要松手**。")
    await link.mic_close()
    stopped, _ = await link.wait_for("AUDIO_STOP", 2.0, mark_close)
    snap_at_close = ctx.snapshot()
    await asyncio.sleep(4.0)
    snap_after = ctx.snapshot()
    events_after = link.events_since(mark_close)

    await ctx.wait_user("现在可以松开按键了，松开后点这个按钮")
    mark_release = link.mark()
    await asyncio.sleep(2.0)
    events_release = link.events_since(mark_release)

    continued = snap_after["notifications"] > snap_at_close["notifications"]
    detail = {
        "pressed_events": [e.name for e in pressed],
        "audio_stop_on_close": bool(stopped),
        "notifications_at_close": snap_at_close["notifications"],
        "notifications_after_4s": snap_after["notifications"],
        "audio_continued_while_holding": continued,
        "pcm_samples_while_holding": snap_after["samples"],
        "events_after_close": [e.to_dict() for e in events_after],
        "events_after_release": [e.to_dict() for e in events_release],
    }
    if not stopped:
        verdict = ("主机 MIC_CLOSE 后没有 AUDIO_STOP —— 遥控器在物理按住期间"
                   "不接受主机的关闭命令，属于硬件/按键优先门控。")
        ok = False
    elif continued:
        verdict = ("遥控器回了 AUDIO_STOP，但音频字节还在继续上来 —— "
                   "状态机被改了，数据通路没被切断，仍是硬件/按键优先门控。")
        ok = False
    else:
        verdict = ("主机 MIC_CLOSE 立刻切断了物理按键的音频流 —— 属于固件状态门，"
                   "说明主机命令对麦克风有真实控制权（这对“改按键位置”是好消息）。")
        ok = True
    retrigger = [e.name for e in events_release
                 if e.name in ("MIC_BUTTON", "AUDIO_START")]
    if retrigger:
        verdict += f" 松手后又出现 {retrigger}（按键重触发）。"
        detail["release_retrigger"] = retrigger
    return StepResult(verdict, ok=ok, details=detail)


# ------------------------------------------------ 步骤 10：AF00 / MMA 私有协议

async def step_private_mma(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("未连接。", ok=False)
    if not link.has_char("af05"):
        return StepResult(
            "GATT 表里没有 AF05 特征 —— 这台遥控器不暴露小米 MMA 私有通道，"
            "或者 Windows 没有把它枚举出来（可能被系统 HID 驱动独占）。本步跳过。",
            ok=None, details={"af00_present": link.has_service("af00")})

    subscribed_af06 = False
    if link.find_char(AF06_CHAR) is not None:
        subscribed_af06 = await link.subscribe(AF06_CHAR, "af06")
    else:
        ctx.log("没有 AF06 通知特征，只能靠写入是否报错来判断。")

    attempts: List[Dict[str, Any]] = []
    any_response = False
    for label, frame in MMA_FRAMES:
        mark = link.mark()
        ctx.log(f"{label}: {hex_text(frame)}")
        item: Dict[str, Any] = {"frame": label, "hex": frame.hex().upper()}
        try:
            await link.write(AF05_CHAR, frame)
            item["write"] = "ok"
        except Exception as exc:  # noqa: BLE001
            item["write"] = f"{type(exc).__name__}: {exc}"
            ctx.log(f"   写入失败：{item['write']}")
            attempts.append(item)
            continue
        await asyncio.sleep(2.0)
        events = link.events_since(mark)
        item["events"] = [e.to_dict() for e in events]
        item["names"] = sorted({e.name for e in events})
        if item["names"]:
            any_response = True
        attempts.append(item)

    if any_response:
        verdict = ("私有通道有回应 —— 写入 AF05 后设备产生了通知/控制事件，"
                   "说明 MMA 通道是活的，值得继续深挖 0xD0 的参数格式。")
        ok = True
    else:
        verdict = ("AF05 全部写入成功但没有任何回应 —— 可能需要先完成小米的鉴权握手"
                   "（MMA 有 auth 流程），裸发 0xD0 会被静默丢弃。")
        ok = False
    return StepResult(verdict, ok=ok,
                      details={"subscribed_af06": subscribed_af06,
                               "attempts": attempts})


# ------------------------------------------------ 步骤 11：自由探测（交互）

async def step_freeform(app, ctx: StepContext) -> StepResult:
    ctx.log("自由探测面板已启用：在下方选择任意特征，可以读取 / 订阅 / 写入任意 hex。")
    ctx.log("所有操作都会记进日志与报告。做完后点“结束观察”按钮。")
    app.enable_freeform(True)
    try:
        await ctx.wait_user("自由探测中……做完后点这个按钮结束本步")
    finally:
        app.enable_freeform(False)
    events = [e.to_dict() for e in ctx.link.events] if ctx.link else []
    return StepResult(
        "自由探测完成，本次会话所有链路事件已记入报告。",
        ok=None, details={"freeform_actions": app.freeform_log,
                          "event_count": len(events)})


# ------------------------------------------------------ 步骤 12：导出报告

async def step_export(app, ctx: StepContext) -> StepResult:
    paths = app.export_report()
    if not paths:
        return StepResult("导出失败，详见日志。", ok=False)
    json_path, text_path = paths
    return StepResult(
        f"报告已导出：\n{json_path}\n{text_path}\n把这两个文件回传即可继续分析。",
        ok=True, details={"json": str(json_path), "text": str(text_path)})


# ------------------------------------------------------ 步骤 13：清理断开

async def step_cleanup(app, ctx: StepContext) -> StepResult:
    link = ctx.link
    if link is None:
        return StepResult("当前没有连接，无需清理。", ok=True)
    if ctx.snapshot()["mic_open"]:
        ctx.log("麦克风仍处于开启状态，补发一次 MIC_CLOSE。")
        try:
            await link.mic_close()
            await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"补发 MIC_CLOSE 失败：{type(exc).__name__}: {exc}")
    await link.close()
    app.link = None
    ctx.meter.reset()
    return StepResult("已取消全部订阅、关闭 CCCD 并断开设备。可以安全退出程序。",
                      ok=True)


STEPS: List[StepDef] = [
    StepDef("connect", "① 连接并枚举整张 GATT 表",
            "选择设备后执行。会列出全部服务/特征/属性，并判定 ATVV 与小米私有 AF00 是否存在。"
            "这是后面所有步骤的前提。",
            "auto", step_connect),
    StepDef("caps_ptt", "② 能力协商 A：PTT + HTT（0x03）",
            "发 GET_CAPABILITIES_V10。收到 CAPS 说明遥控器确实在讲 ATVV，"
            "并给出它的版本号、编码、帧长和交互模式。",
            "auto", step_caps_ptt),
    StepDef("caps_on_request", "③ 能力协商 B：On-request（0x00）",
            "发 On-request 版本。如果 interaction 变成 0x00，只说明遥控器接受了这个模式状态，"
            "不代表麦克风数据会出来，还要看第 ⑥、⑦ 步的结果。",
            "auto", step_caps_on_request),
    StepDef("invalid_opcode", "④ 负对照：无效 opcode（0x77 / 0xFF）",
            "故意发垃圾命令。如果没有回应，证明遥控器的回应是语义相关的，"
            "“发什么都回”这个怀疑就被排除。",
            "auto", step_invalid_opcode),
    StepDef("close_idle", "⑤ 负对照：未开麦时发 MIC_CLOSE",
            "麦克风本来就没开，还发关闭命令。有没有 AUDIO_STOP 决定了"
            "“收到 AUDIO_STOP”能不能当作证据。",
            "auto", step_close_when_idle),
    StepDef("host_open_2s", "⑥ 主机主动开麦 2 秒（自动关）",
            "电脑扮演电视，主动发 MIC_OPEN，2 秒后发 MIC_CLOSE。"
            "对着遥控器说话，看电平表动不动。",
            "auto", step_host_open_2s),
    StepDef("host_open_hold", "⑦ 主机主动开麦持续 10 秒（不关）",
            "同上但不自动关闭，逐秒打印计数。用来区分“只回状态不给数据”"
            "和“给了数据但主机解不出”。",
            "auto", step_host_open_hold),
    StepDef("host_open_ptt", "⑦b 切回 PTT（0x03）后主机开麦 2 秒",
            "前面的主机开麦是在 On-request 模式下做的。这一步先切回 PTT 模式，"
            "再发 MIC_OPEN，看“按住说话”模式下主机开麦是否能拿到音频。",
            "auto", step_host_open_ptt),
    StepDef("physical", "⑧ 物理按键基线（证明电平表能工作）",
            "用真实的语音键录一段。这一步不通过，后面所有“没有音频”的结论都不可信。",
            "manual", step_physical_baseline),
    StepDef("hold_and_close", "⑨ 按住物理键 + 主机 MIC_CLOSE",
            "关键实验：按住语音键的同时让电脑发关闭命令。能关掉 = 固件状态门"
            "（主机有真实控制权）；关不掉 = 硬件/按键优先门控（不拆机基本没戏）。",
            "manual", step_hold_and_close),
    StepDef("mma", "⑩ 小米私有协议 AF00 / MMA 0xD0",
            "如果第 ① 步发现了 AF00/AF05，就往私有通道发语音开始帧，"
            "看 AF06 有没有通知。",
            "auto", step_private_mma),
    StepDef("freeform", "⑪ 自由探测（任意特征读 / 写 / 订阅）",
            "手动模式：自己挑特征、自己填 hex，验证任何你想到的假设。"
            "所有操作都会被记录。",
            "freeform", step_freeform),
    StepDef("export", "⑫ 导出测试报告",
            "把 GATT 表、每一步的判定、全部链路事件写成 JSON + 文本，方便回传。",
            "auto", step_export),
    StepDef("cleanup", "⑬ 清理并断开",
            "补发 MIC_CLOSE、取消订阅、写回 CCCD、关闭设备句柄。",
            "auto", step_cleanup),
]


# ------------------------------------------------------------------ 主界面

STATUS_TEXT = {True: "通过", False: "不通过", None: "待定"}
STATUS_MARK = {True: "✓", False: "✗", None: "◐"}


class UltimateProbeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.loop_thread = LoopThread()
        self.meter = MeterState()
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        # 后台 loop 线程不能直接碰 Tk；所有 UI 变更统一入队，由 _tick 在主线程执行。
        self.ui_queue: "queue.Queue" = queue.Queue()

        self.winrt: Optional[WinRT] = None
        self.link: Optional[BleLink] = None
        self.candidates: List[identity.RC003Candidate] = []
        self.results: Dict[str, Dict[str, Any]] = {}
        self.freeform_log: List[Dict[str, Any]] = []
        self.session_log: List[str] = []

        self._index = 0
        self._running = False
        self._closing = False
        self._connecting = False
        self._ctx: Optional[StepContext] = None
        self._history = deque([-60.0] * BLOCKS, maxlen=BLOCKS)
        self._peak_hold_db = -60.0
        self._blocks: List[int] = []
        self._started_at = datetime.now()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("提示：先退出正式桥接程序（托盘退出），否则同一台遥控器会被占用。")
        self._select_step(0)
        self.root.after(150, self._refresh_devices)
        self.root.after(UI_TICK_MS, self._tick)
        self.root.after(LOG_TICK_MS, self._drain_log)

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.root.title("RC003 终极开麦测试向导")
        self.root.geometry("1180x860")
        self.root.minsize(1040, 720)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        device_row = ttk.Frame(outer)
        device_row.pack(fill="x")
        ttk.Label(device_row, text="设备").pack(side="left")
        self.device_combo = ttk.Combobox(device_row, state="readonly", width=34)
        self.device_combo.pack(side="left", padx=(8, 8))
        self.refresh_btn = ttk.Button(device_row, text="刷新设备",
                                      command=self._refresh_devices)
        self.refresh_btn.pack(side="left")
        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(device_row, textvariable=self.status_var,
                  foreground="#555").pack(side="left", padx=(14, 0))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True, pady=(10, 0))

        # 左侧：步骤列表
        left = ttk.LabelFrame(body, text="测试步骤", padding=6)
        left.pack(side="left", fill="y")
        self.tree = ttk.Treeview(left, columns=("status",), show="tree headings",
                                 height=15, selectmode="browse")
        self.tree.heading("#0", text="步骤")
        self.tree.heading("status", text="结果")
        self.tree.column("#0", width=290, stretch=False)
        self.tree.column("status", width=64, stretch=False, anchor="center")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self._tree_items: List[str] = []
        for position, step in enumerate(STEPS):
            item = self.tree.insert("", "end", text=step.title, values=("待测",))
            self._tree_items.append(item)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # 右侧：当前步骤详情
        right = ttk.Frame(body, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)

        self.step_title_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.step_title_var,
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")

        hint_frame = ttk.Frame(right)
        hint_frame.pack(fill="x", pady=(6, 0))
        self.hint_text = tk.Text(hint_frame, height=4, wrap="word", state="disabled",
                                 bg="#f6f8fa", relief="flat")
        self.hint_text.pack(fill="x")

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(10, 0))
        self.run_btn = ttk.Button(actions, text="▶ 执行本步", command=self._run_current_step)
        self.run_btn.pack(side="left")
        self.user_btn = ttk.Button(actions, text="我已做完", state="disabled",
                                   command=self._on_user_done)
        self.user_btn.pack(side="left", padx=(8, 0))
        self.skip_btn = ttk.Button(actions, text="跳过本步", command=self._skip_step)
        self.skip_btn.pack(side="left", padx=(8, 0))
        self.pass_btn = ttk.Button(actions, text="标记通过",
                                   command=lambda: self._mark(True))
        self.pass_btn.pack(side="left", padx=(8, 0))
        self.fail_btn = ttk.Button(actions, text="标记失败",
                                   command=lambda: self._mark(False))
        self.fail_btn.pack(side="left", padx=(8, 0))
        self.next_btn = ttk.Button(actions, text="下一步 ▶", command=self._next_step)
        self.next_btn.pack(side="left", padx=(8, 0))

        result_frame = ttk.LabelFrame(right, text="本步判定", padding=8)
        result_frame.pack(fill="x", pady=(10, 0))
        self.result_text = tk.Text(result_frame, height=5, wrap="word", state="disabled")
        self.result_text.pack(fill="x")
        self.result_text.tag_configure("ok", foreground="#1a7f37")
        self.result_text.tag_configure("bad", foreground="#c62828")
        self.result_text.tag_configure("mid", foreground="#9a6700")

        free_frame = ttk.LabelFrame(right, text="自由探测（第 ⑪ 步启用）", padding=8)
        free_frame.pack(fill="x", pady=(10, 0))
        row1 = ttk.Frame(free_frame)
        row1.pack(fill="x")
        ttk.Label(row1, text="特征").pack(side="left")
        self.char_combo = ttk.Combobox(row1, state="readonly", width=62)
        self.char_combo.pack(side="left", padx=(6, 0), fill="x", expand=True)
        row2 = ttk.Frame(free_frame)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="HEX").pack(side="left")
        self.hex_entry = ttk.Entry(row2, width=52)
        self.hex_entry.insert(0, "0C 00")
        self.hex_entry.pack(side="left", padx=(6, 8))
        self.read_btn = ttk.Button(row2, text="读取", command=lambda: self._freeform("read"))
        self.read_btn.pack(side="left")
        self.sub_btn = ttk.Button(row2, text="订阅", command=lambda: self._freeform("subscribe"))
        self.sub_btn.pack(side="left", padx=(6, 0))
        self.write_btn = ttk.Button(row2, text="写入", command=lambda: self._freeform("write"))
        self.write_btn.pack(side="left", padx=(6, 0))
        self._freeform_widgets = (self.char_combo, self.hex_entry, self.read_btn,
                                  self.sub_btn, self.write_btn)
        self.enable_freeform(False)

        # 电平表
        meter_frame = ttk.LabelFrame(outer, text="上行音频电平（解码后 PCM）", padding=8)
        meter_frame.pack(fill="x", pady=(10, 0))
        self.canvas = tk.Canvas(meter_frame, width=METER_WIDTH, height=METER_HEIGHT + 24,
                                bg="#11151c", highlightthickness=1,
                                highlightbackground="#2a3140")
        self.canvas.pack(side="left")
        self._build_meter_blocks()
        readout = ttk.Frame(meter_frame)
        readout.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.db_var = tk.StringVar(value="无信号")
        self.frames_var = tk.StringVar(value="PCM 帧：0")
        self.raw_var = tk.StringVar(value="BLE 音频通知：0 / 0 B")
        self.caps_var = tk.StringVar(value="能力：-")
        self.mic_var = tk.StringVar(value="麦克风：关")
        for var in (self.db_var, self.frames_var, self.raw_var, self.caps_var,
                    self.mic_var):
            ttk.Label(readout, textvariable=var).pack(anchor="w")

        log_frame = ttk.LabelFrame(outer, text="事件日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _build_meter_blocks(self) -> None:
        gap = 2
        width = (METER_WIDTH - 16 - (BLOCKS - 1) * gap) / BLOCKS
        for index in range(BLOCKS):
            x0 = 8 + index * (width + gap)
            rect = self.canvas.create_rectangle(x0, 6, x0 + width, 6 + METER_HEIGHT,
                                                fill="#1b2230", outline="")
            self._blocks.append(rect)
        for label, fraction in (("-60", 0.0), ("-30", 0.5), ("-12", 0.8), ("0", 1.0)):
            x = 8 + fraction * (METER_WIDTH - 16)
            self.canvas.create_text(x, METER_HEIGHT + 15, text=label,
                                    fill="#7d8590", anchor="center")
        self._peak_line = self.canvas.create_line(9, 6, 9, 6 + METER_HEIGHT,
                                                  fill="#e6e8eb", width=2, state="hidden")

    # -------------------------------------------------------------- 日志/线程

    def _post(self, callback, *args) -> None:
        self.ui_queue.put((callback, args))

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {message}"
        self.session_log.append(line)
        self.log_queue.put(line)

    def _drain_log(self) -> None:
        if self._closing:
            return
        lines: List[str] = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > MAX_LOG_LINES:
                self.log_text.delete("1.0", f"{total - MAX_LOG_LINES}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(LOG_TICK_MS, self._drain_log)

    # -------------------------------------------------------------- 设备发现

    def _ensure_winrt(self) -> bool:
        if self.winrt is not None:
            return True
        try:
            self.winrt = WinRT()
        except Exception as exc:  # noqa: BLE001
            self.status_var.set("WinRT 不可用")
            self.log(f"WinRT 初始化失败：{type(exc).__name__}: {exc}")
            self.log("请在 rc003 的 .venv 里安装 requirements.txt（winrt-Windows.* 3.2.1）。")
            return False
        return True

    def _refresh_devices(self) -> None:
        if self._connecting or self.link is not None:
            return
        if not self._ensure_winrt():
            return
        self.status_var.set("正在扫描已配对的 BLE 设备…")
        future = self.loop_thread.submit(ble_transport_winrt.discover_candidates())
        future.add_done_callback(lambda done: self._post(self._on_discovered, done))

    def _on_discovered(self, future) -> None:
        if self._closing:
            return
        try:
            candidates = future.result()
        except Exception as exc:  # noqa: BLE001 - 诊断工具直接显示失败原因
            self.status_var.set(f"扫描失败：{type(exc).__name__}")
            self.log(f"扫描失败：{type(exc).__name__}: {exc}")
            return
        self.candidates = identity.qualifying_candidates(candidates)
        values = [f"{item.name}  #{index + 1}"
                  for index, item in enumerate(self.candidates)]
        self.device_combo["values"] = values
        if values:
            self.device_combo.current(0)
            self.status_var.set(f"找到 {len(values)} 个 RC003，选择后执行第 ① 步。")
            self.log(f"发现 {len(values)} 个 RC003 候选。")
        else:
            self.device_combo.set("")
            self.status_var.set("没有找到已配对的 RC003")
            self.log("没有名称匹配的 RC003；请先在 Windows 蓝牙设置里配对。")

    def selected_candidate(self) -> Optional[identity.RC003Candidate]:
        choice = self.device_combo.current()
        if choice < 0 or choice >= len(self.candidates):
            return None
        return self.candidates[choice]

    # -------------------------------------------------------------- 步骤导航

    def _on_tree_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection or self._running:
            return
        index = self._tree_items.index(selection[0])
        if index != self._index:
            self._select_step(index)

    def _select_step(self, index: int) -> None:
        self._index = max(0, min(index, len(STEPS) - 1))
        step = STEPS[self._index]
        self.tree.selection_set(self._tree_items[self._index])
        self.tree.see(self._tree_items[self._index])
        self.step_title_var.set(f"{self._index + 1}/{len(STEPS)}  {step.title}")
        self.hint_text.configure(state="normal")
        self.hint_text.delete("1.0", "end")
        self.hint_text.insert("1.0", step.hint)
        self.hint_text.configure(state="disabled")
        self._show_result(step.key)

    def _show_result(self, key: str) -> None:
        record = self.results.get(key)
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        if record is None:
            self.result_text.insert("1.0", "（尚未执行）")
        else:
            tag = {True: "ok", False: "bad", None: "mid"}[record["ok"]]
            self.result_text.insert("1.0",
                                    f"[{record['status']}] {record['verdict']}", tag)
            if record.get("overridden"):
                self.result_text.insert("end", "\n（该结果由你手动标记）", tag)
        self.result_text.configure(state="disabled")

    def _record_result(self, step: StepDef, result: StepResult,
                       overridden: bool = False) -> None:
        self.results[step.key] = {
            "title": step.title,
            "verdict": result.verdict,
            "ok": result.ok,
            "status": STATUS_TEXT[result.ok],
            "overridden": overridden,
            "details": result.details,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        mark = STATUS_MARK[result.ok]
        self.tree.item(self._tree_items[STEPS.index(step)],
                       values=(f"{mark} {STATUS_TEXT[result.ok]}",))
        self._show_result(step.key)

    def _mark(self, ok: bool) -> None:
        step = STEPS[self._index]
        existing = self.results.get(step.key)
        verdict = (existing["verdict"] if existing else "") + \
            f"\n【人工标记：{'通过' if ok else '不通过'}】"
        self._record_result(step, StepResult(verdict, ok=ok,
                                             details=(existing or {}).get("details", {})),
                            overridden=True)
        self.log(f"第 {self._index + 1} 步被人工标记为 {'通过' if ok else '不通过'}。")

    def _skip_step(self) -> None:
        if self._running:
            return
        step = STEPS[self._index]
        self._record_result(step, StepResult("已跳过（未执行）。", ok=None,
                                            details={"skipped": True}),
                            overridden=True)
        self.log(f"跳过第 {self._index + 1} 步：{step.title}")
        self._next_step()

    def _next_step(self) -> None:
        if self._running:
            return
        if self._index + 1 < len(STEPS):
            self._select_step(self._index + 1)

    # -------------------------------------------------------------- 执行步骤

    def _run_current_step(self) -> None:
        if self._running:
            self.log("已有步骤在执行，请等它结束。")
            return
        if not self._ensure_winrt():
            return
        step = STEPS[self._index]
        if step.key != "connect" and self.link is None:
            if not messagebox.askyesno("RC003 终极测试",
                                       "还没有连接设备。要先执行第 ① 步连接吗？"):
                return
            self._select_step(0)
            step = STEPS[0]
        self._running = True
        self._set_running_state(True)
        ctx = StepContext(self)
        self._ctx = ctx
        self.log(f"===== 开始第 {self._index + 1} 步：{step.title} =====")
        future = self.loop_thread.submit(self._run_step_async(step, ctx))
        future.add_done_callback(
            lambda done, _step=step: self._post(self._on_step_done, done, _step))

    async def _run_step_async(self, step: StepDef, ctx: StepContext) -> StepResult:
        started = time.time()
        try:
            result = await step.runner(self, ctx)
            if not isinstance(result, StepResult):
                result = StepResult(str(result), ok=None)
        except Exception as exc:  # noqa: BLE001 - 诊断工具要把异常也变成结论
            self.log(f"步骤异常：{type(exc).__name__}: {exc}")
            result = StepResult(f"步骤异常：{type(exc).__name__}: {exc}", ok=False,
                                details={"error": repr(exc)})
        result.details.setdefault("duration_s", round(time.time() - started, 2))
        return result

    def _on_step_done(self, future, step: StepDef) -> None:
        self._running = False
        self._ctx = None
        self._set_running_state(False)
        if self._closing:
            return
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001
            result = StepResult(f"步骤执行失败：{type(exc).__name__}: {exc}", ok=False)
        self._record_result(step, result)
        self.log(f"===== 第 {STEPS.index(step) + 1} 步结束："
                 f"{STATUS_TEXT[result.ok]} =====")
        if step.key == "connect" and self.link is not None:
            self.char_combo["values"] = self.link.list_char_labels()
            if self.char_combo["values"]:
                self.char_combo.current(0)
        if result.ok is True and STEPS.index(step) + 1 < len(STEPS):
            self.log("可以继续下一步了。")

    def _set_running_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.run_btn.configure(text="执行中…" if running else "▶ 执行本步", state=state)
        self.skip_btn.configure(state=state)
        self.next_btn.configure(state=state)
        self.refresh_btn.configure(
            state="disabled" if (running or self.link is not None) else "normal")
        if not running:
            self.user_btn.configure(state="disabled", text="我已做完")
        self.status_var.set("执行中…" if running else
                            ("已连接" if self.link is not None else "未连接"))

    # ------------------------------------------------------ 用户交互按钮

    def arm_user_button(self, prompt: str) -> None:
        self._post(self._arm_user_button_ui, prompt)

    def _arm_user_button_ui(self, prompt: str) -> None:
        if self._closing:
            return
        self.user_btn.configure(text=prompt, state="normal")
        self.status_var.set(f"等你操作：{prompt}")

    def disarm_user_button(self) -> None:
        self._post(self._disarm_user_button_ui)

    def _disarm_user_button_ui(self) -> None:
        if self._closing:
            return
        self.user_btn.configure(text="我已做完", state="disabled")

    def _on_user_done(self) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        ctx.user_done()

    # ------------------------------------------------------------ 自由探测

    def enable_freeform(self, enabled: bool) -> None:
        self._post(self._enable_freeform_ui, enabled)

    def _enable_freeform_ui(self, enabled: bool) -> None:
        if self._closing:
            return
        state = "normal" if enabled else "disabled"
        for widget in self._freeform_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        if enabled:
            self.char_combo.configure(state="readonly")
            if self.link is not None:
                self.char_combo["values"] = self.link.list_char_labels()

    def _selected_char_uuid(self) -> Optional[str]:
        text = self.char_combo.get()
        if not text:
            return None
        return text.split()[0].casefold()

    def _freeform(self, kind: str) -> None:
        if self.link is None:
            self.log("自由探测需要先完成第 ① 步连接。")
            return
        target = self._selected_char_uuid()
        if target is None:
            self.log("请先选择一个特征。")
            return
        data = b""
        if kind == "write":
            raw = self.hex_entry.get().replace(",", " ").replace("0x", "")
            try:
                data = bytes.fromhex(raw.replace(" ", ""))
            except ValueError:
                self.log(f"HEX 解析失败：{raw!r}")
                return
            if not data:
                self.log("HEX 内容为空。")
                return
        self.freeform_log.append({"kind": kind, "char": target,
                                  "hex": data.hex().upper(),
                                  "at": datetime.now().isoformat(timespec="seconds")})
        self.log(f"自由探测：{kind} {target}")
        future = self.loop_thread.submit(self._freeform_async(kind, target, data))
        future.add_done_callback(lambda done: self._post(self._freeform_done, done))

    async def _freeform_async(self, kind: str, target: str, data: bytes) -> str:
        link = self.link
        if link is None:
            return "连接已断开"
        try:
            if kind == "read":
                value = await link.read(target)
                return f"读取成功：{hex_text(value, 64)}"
            if kind == "subscribe":
                ok = await link.subscribe(target, "notify")
                return "订阅成功" if ok else "订阅失败（详见日志）"
            await link.write(target, data)
            return f"写入成功：{hex_text(data, 64)}"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    def _freeform_done(self, future) -> None:
        try:
            self.log(f"自由探测结果：{future.result()}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"自由探测异常：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 报告

    def assessment(self) -> List[str]:
        """根据各步结果给出模型 A/B/C 的倾向性判断。"""
        lines: List[str] = []
        connect = self.results.get("connect")
        if connect is None:
            return ["还没有执行第 ① 步，无法给出判断。"]
        details = connect.get("details", {})
        has_atvv = details.get("has_atvv")
        has_af00 = details.get("has_af00")

        if not has_atvv:
            lines.append("· 这台设备没有暴露 ATVV 服务 —— 之前所有基于 ATVV 的推断都不适用，"
                         "需要重新识别它到底用什么协议传语音。")
        else:
            lines.append("· ATVV 服务存在，语音链路确实是 Android TV 的 ATVV。")

        caps = self.results.get("caps_on_request")
        if caps and caps.get("ok"):
            interaction = caps.get("details", {}).get("interaction_after")
            if interaction == "0x00":
                lines.append("· 遥控器接受了 On-request（按一下再说话）模式的切换请求 —— "
                             "这只说明状态被接受，不代表麦克风音频会出来，需结合第 ⑥、⑦ 步判断。")
            else:
                lines.append(f"· 遥控器拒绝切换到 On-request（interaction={interaction}），"
                             "交互模式由固件固定。")

        invalid = self.results.get("invalid_opcode")
        if invalid and invalid.get("ok") is True:
            lines.append("· 负对照通过：无效命令没有回应，说明 ATVV 的回应是真实的语义应答，"
                         "不是“发什么都回”。")
        elif invalid and invalid.get("ok") is False:
            lines.append("· ⚠ 负对照失败：无效命令也能触发回应，之前“主机命令有效”的证据需要重估。")

        host_steps = {
            "On-request 2 秒": self.results.get("host_open_2s"),
            "On-request 10 秒": self.results.get("host_open_hold"),
            "PTT 2 秒": self.results.get("host_open_ptt"),
        }
        host_ok_names = [k for k, r in host_steps.items() if (r or {}).get("ok") is True]
        if host_ok_names:
            lines.append("· 主机主动 MIC_OPEN 拿到了真实音频（" + "、".join(host_ok_names) + "）—— "
                         "这支持模型 A，但需要确认该模式下音频确实是语音而非噪声。")
        else:
            done = {k: r for k, r in host_steps.items() if r}
            if done:
                any_start = any((r.get("details", {}).get("audio_start")) for r in done.values())
                any_raw = any((r.get("details", {}).get("raw_bytes", 0)) for r in done.values())
                tested = "、".join(done.keys())
                if any_start and not any_raw:
                    lines.append(f"· 已测试（{tested}）：主机 MIC_OPEN 能换来 AUDIO_START，但零音频字节。"
                                 "这支持模型 B（固件把麦克风数据通路锁在物理按键上），"
                                 "但只覆盖了所测模式，不能排除其他模式或私有协议的情况。")
                elif any_start and any_raw:
                    lines.append("· 音频字节有上来但解不出 PCM —— 问题可能在主机侧解码/帧对齐，"
                                 "先修这个再下结论。")
                else:
                    lines.append(f"· 已测试（{tested}）：主机 MIC_OPEN 没有被承认，"
                                 "遥控器没有对主机命令给出开麦回应。")
            if not self.results.get("host_open_ptt"):
                lines.append("· 尚未执行第 ⑦b 步（PTT 模式下的主机开麦），该结论还不完整。")

        gate = self.results.get("hold_and_close")
        if gate:
            if gate.get("ok") is True:
                lines.append("· 第 ⑨ 步显示是**固件状态门**：主机命令能真正切断麦克风，"
                             "说明主机对麦克风有控制权。")
            elif gate.get("ok") is False:
                lines.append("· 第 ⑨ 步显示是**硬件/按键优先门控**：物理按住时主机关不掉，"
                             "麦克风由按键直接驱动。")

        physical = self.results.get("physical")
        if physical and physical.get("ok") is not True:
            lines.append("· ⚠ 物理按键基线未通过 —— 上面所有关于“没有音频”的结论都不可信，"
                         "请先解决订阅/连接问题再重测。")

        if has_af00:
            mma = self.results.get("mma")
            if mma and mma.get("ok") is True:
                lines.append("· 小米私有 AF00 通道有回应 —— 存在第二条可能的开麦路径，值得继续逆向。")
            else:
                lines.append("· 存在 AF00 私有服务但裸发 MMA 帧无回应 —— 可能需要先完成小米鉴权握手。")
        else:
            lines.append("· 没有发现小米私有 AF00 服务（或被 Windows HID 驱动隐藏）—— "
                         "私有协议这条路在本机暂时走不通。")
        return lines

    def build_payload(self) -> Dict[str, Any]:
        steps_out: List[Dict[str, Any]] = []
        for position, step in enumerate(STEPS):
            record = self.results.get(step.key)
            steps_out.append({
                "index": position + 1,
                "key": step.key,
                "title": step.title,
                "kind": step.kind,
                "status": record["status"] if record else "未执行",
                "verdict": record["verdict"] if record else None,
                "details": record["details"] if record else None,
            })
        return {
            "tool": "rc003_ultimate_probe",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "device": self.link.device_name if self.link else
                      (self.selected_candidate().name if self.selected_candidate() else None),
            "capabilities": self.meter.caps_text,
            "steps": steps_out,
            "assessment": self.assessment(),
            "freeform_actions": self.freeform_log,
            "events": [e.to_dict() for e in (self.link.events if self.link else [])],
            "log": self.session_log[-4000:],
        }

    def _render_text_report(self, payload: Dict[str, Any]) -> str:
        out: List[str] = []
        out.append("RC003 终极开麦测试报告")
        out.append("=" * 60)
        out.append(f"生成时间：{payload['generated_at']}")
        out.append(f"设备：{payload['device']}")
        out.append(f"能力：{payload['capabilities']}")
        out.append("")
        out.append("【总体判断】")
        out.extend(payload["assessment"])
        out.append("")
        out.append("【逐步结果】")
        for step in payload["steps"]:
            out.append(f"{step['index']:>2}. {step['title']}  →  {step['status']}")
            if step["verdict"]:
                for line in str(step["verdict"]).splitlines():
                    out.append(f"      {line}")
            details = step.get("details") or {}
            for key, value in details.items():
                if key in ("gatt", "services", "events", "events_after_close",
                           "events_after_release", "attempts", "freeform_actions"):
                    continue
                out.append(f"      {key} = {value}")
            out.append("")
        out.append("【GATT 表】")
        connect = self.results.get("connect", {}).get("details", {})
        out.append(connect.get("gatt", "（未枚举）"))
        out.append("")
        out.append(f"【链路事件】共 {len(payload['events'])} 条")
        for event in payload["events"][-200:]:
            out.append(f"  #{event['seq']:>4} [{event['channel']}] {event['name']} "
                       f"{event['payload_hex']}  {event['detail']}")
        return "\n".join(out)

    def export_report(self) -> Optional[tuple]:
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            json_path = REPORT_DIR / f"rc003-ultimate-report-{stamp}.json"
            text_path = REPORT_DIR / f"rc003-ultimate-report-{stamp}.txt"
            payload = self.build_payload()
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                            default=str), encoding="utf-8")
            text_path.write_text(self._render_text_report(payload), encoding="utf-8")
            self.log(f"报告已写入：{json_path}")
            self.log(f"报告已写入：{text_path}")
            return json_path, text_path
        except Exception as exc:  # noqa: BLE001
            self.log(f"导出报告失败：{type(exc).__name__}: {exc}")
            return None

    # -------------------------------------------------------------- UI tick

    def _tick(self) -> None:
        if self._closing:
            return
        while True:
            try:
                callback, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception as exc:  # noqa: BLE001 - 单个回调异常不能中断界面
                self.log(f"回调异常：{type(exc).__name__}: {exc}")
        if self._closing:
            return

        snap = self.meter.snapshot()
        now = time.monotonic()
        db = 20.0 * math.log10(snap["rms"] / 32768.0) if snap["rms"] > 0.0 else -60.0
        db = max(-60.0, min(0.0, db))
        self._history.append(db)
        self._peak_hold_db = max(db, self._peak_hold_db - 0.8)
        self._paint_meter(db)

        last = snap["last_audio_at"]
        if last > 0.0 and (now - last) < 0.3:
            self.db_var.set(f"{db:.1f} dBFS")
        elif last > 0.0:
            self.db_var.set(f"无信号（最近一次 {now - last:.1f}s 前）")
        else:
            self.db_var.set("无信号")
        self.frames_var.set(f"PCM 帧：{snap['frames']} · 样本：{snap['samples']} · "
                            f"峰值：{snap['peak']}")
        self.raw_var.set(f"BLE 音频通知：{snap['notifications']} / "
                         f"{snap['notification_bytes']} B")
        self.caps_var.set(f"能力：{self.meter.caps_text}")
        self.mic_var.set(f"麦克风：{'开' if snap['mic_open'] else '关'}")

        if not self._running:
            connected = self.link is not None
            self.status_var.set("已连接" if connected else "未连接")
            self.refresh_btn.configure(state="disabled" if connected else "normal")
        self.root.after(UI_TICK_MS, self._tick)

    def _paint_meter(self, db: float) -> None:
        for index, rect in enumerate(self._blocks):
            block_db = -60.0 + (index + 1) * 60.0 / BLOCKS
            if block_db <= db:
                if block_db >= -6.0:
                    color = "#e5484d"
                elif block_db >= -18.0:
                    color = "#f5a524"
                elif block_db >= -36.0:
                    color = "#46a758"
                else:
                    color = "#2f6f45"
            else:
                color = "#1b2230"
            self.canvas.itemconfigure(rect, fill=color)
        if self._peak_hold_db <= -59.5:
            self.canvas.itemconfigure(self._peak_line, state="hidden")
        else:
            fraction = (self._peak_hold_db + 60.0) / 60.0
            x = 8 + fraction * (METER_WIDTH - 16)
            self.canvas.coords(self._peak_line, x, 6, x, 6 + METER_HEIGHT)
            self.canvas.itemconfigure(self._peak_line, state="normal")

    # ---------------------------------------------------------------- 关闭

    def _on_close(self) -> None:
        if self._running and not messagebox.askyesno(
                "RC003 终极测试", "还有步骤在执行，确定要退出吗？"):
            return
        self._closing = True
        if self._ctx is not None:
            self._ctx.user_done()
        link = self.link
        self.link = None
        if link is not None:
            try:
                self.loop_thread.submit(link.close()).result(timeout=5.0)
            except Exception:  # noqa: BLE001 - 关闭窗口时不再打扰用户
                pass
        self.loop_thread.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    UltimateProbeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
