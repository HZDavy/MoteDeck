"""模式2：把本机麦克风当成语音输入源。

模式1 用遥控器自带的麦克风，而那只遥控器只在话筒键被按住时才送音频，
所以"按一下开始、再按一下结束"在那条路上永远拿不到声音。模式2 把输入源
换成插在这台电脑上的录音设备：本机麦克风本来就是一直开着的，松手之后
音频不会断，短按锁存才真正成立。
"""

from __future__ import annotations

import math
import threading
from typing import Callable, List, Optional, Sequence

# 遥控器的音频是 16 kHz 单声道。本机麦克风也按同一规格采，后面那条
# 重采样 / 写出的链路就完全不用改，两种模式共用同一条播放路径。
CAPTURE_SAMPLE_RATE_HZ = 16000
# 30 ms 一块：小到延迟可忽略，大到大多数声卡都能接受。
_FRAME_SAMPLES = 480


class LocalMicrophoneUnavailableError(RuntimeError):
    """选定的录音设备打不开（被占用、已拔出、名字对不上）。"""


def _sounddevice():
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 缺依赖时降级为"没有可选设备"
        raise LocalMicrophoneUnavailableError(
            f"录音设备支持不可用: {exc}"
        ) from exc
    return sd


def list_input_devices() -> List[str]:
    """所有能录音的设备名，去重并保持驱动枚举顺序。"""

    try:
        sd = _sounddevice()
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 - 枚举失败只意味着列表为空
        return []
    names: List[str] = []
    for device in devices:
        if device.get("max_input_channels", 0) <= 0:
            continue
        name = str(device.get("name", "")).strip()
        if name and name not in names:
            names.append(name)
    return names


def resolve_input_device_index(device_name: str) -> int:
    """把用户选中的设备名解析成 sounddevice 的索引。"""

    sd = _sounddevice()
    wanted = device_name.strip()
    if not wanted:
        raise LocalMicrophoneUnavailableError("尚未选择本机麦克风")
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_input_channels", 0) <= 0:
            continue
        if str(device.get("name", "")).strip() == wanted:
            return index
    raise LocalMicrophoneUnavailableError(f"选定的录音设备当前不可用: {wanted!r}")


def default_input_device_name() -> str:
    """系统默认录音设备的名字；拿不到时返回空串。"""

    try:
        sd = _sounddevice()
        default = sd.default.device
    except Exception:  # noqa: BLE001 - 没有默认设备就等于没有电平
        return ""
    try:
        index = (
            int(default[0])
            if isinstance(default, (tuple, list))
            else int(default)
        )
    except (TypeError, ValueError, IndexError):
        return ""
    if index < 0:
        return ""
    try:
        info = sd.query_devices(index)
    except Exception:  # noqa: BLE001 - 默认设备失效时不报错
        return ""
    if int(info.get("max_input_channels", 0)) <= 0:
        return ""
    return str(info.get("name", "")).strip()


class LocalMicrophoneCapture:
    """把一个录音设备按 16 kHz 单声道读出来，逐块交给回调。

    回调在声卡线程上执行，因此它必须像 BLE 那条路一样只做入队，不做
    任何可能阻塞的事情。
    """

    def __init__(
        self,
        *,
        device_name: str,
        on_frame: Callable[[Sequence[int]], None],
        logger,
        sample_rate: int = CAPTURE_SAMPLE_RATE_HZ,
    ) -> None:
        self._device_name = device_name
        self._on_frame = on_frame
        self._logger = logger
        self._sample_rate = sample_rate
        self._stream: Optional[object] = None
        self._lock = threading.Lock()
        self._frames = 0

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stream is not None

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    def start(self) -> bool:
        with self._lock:
            if self._stream is not None:
                return True
        try:
            sd = _sounddevice()
            index = resolve_input_device_index(self._device_name)
            stream = sd.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=_FRAME_SAMPLES,
                device=index,
                channels=1,
                dtype="int16",
                callback=self._handle_indata,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 - 打不开就是"模式2 静默"
            self._logger.warning(
                "local voice microphone open failed: device=%s error=%s",
                self._device_name,
                exc,
            )
            return False
        with self._lock:
            self._stream = stream
            self._frames = 0
        self._logger.info(
            "local voice microphone opened: device=%s rate=%s",
            self._device_name,
            self._sample_rate,
        )
        return True

    def _handle_indata(self, indata, _frames, _time_info, status) -> None:
        if status:
            self._logger.debug("local voice microphone status: %s", status)
        with self._lock:
            if self._stream is None:
                return
            self._frames += 1
        try:
            samples = memoryview(indata).cast("h").tolist()
        except Exception as exc:  # noqa: BLE001 - 认不出的缓冲直接丢掉
            self._logger.debug(
                "local voice microphone buffer rejected: error_type=%s",
                type(exc).__name__,
            )
            return
        try:
            self._on_frame(samples)
        except BaseException as exc:  # noqa: BLE001 - 声卡线程绝不能死
            self._logger.warning(
                "local voice frame delivery failed: error_type=%s",
                type(exc).__name__,
            )

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except BaseException as exc:  # noqa: BLE001 - 关闭失败不再重试
            self._logger.warning(
                "local voice microphone close failed: error_type=%s",
                type(exc).__name__,
            )
        self._logger.info("local voice microphone closed")


class MicrophoneLevelMonitor:
    """持续开一路录音设备，只保留最新电平（0..1），给设置页画电平条。

    设置窗口和 resident 进程是两个进程，这里拿不到 resident 那条音频流，
    所以语音页的电平自己打开"当前选择的麦克风"来采。回调在声卡线程上，
    只做一次 RMS 计算就写回字段；UI 侧按帧率去读。
    """

    # 低于 -60 dBFS 当作静音，接近 -12 dBFS 就是满格。
    _FLOOR_DB = -60.0
    _CEIL_DB = -12.0

    def __init__(
        self,
        *,
        logger,
        sample_rate: int = CAPTURE_SAMPLE_RATE_HZ,
    ) -> None:
        self._logger = logger
        self._sample_rate = sample_rate
        self._stream: Optional[object] = None
        self._device_name = ""
        self._level = 0.0
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stream is not None

    @property
    def device_name(self) -> str:
        with self._lock:
            return self._device_name

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    def start(self, device_name: str) -> bool:
        self.stop()
        wanted = (device_name or "").strip()
        if not wanted:
            return False
        try:
            sd = _sounddevice()
            index = resolve_input_device_index(wanted)
            stream = sd.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=_FRAME_SAMPLES,
                device=index,
                channels=1,
                dtype="int16",
                callback=self._handle_indata,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 - 打不开就当作没有电平
            self._logger.debug(
                "microphone level monitor open failed: device=%s error=%s",
                wanted,
                exc,
            )
            return False
        with self._lock:
            self._stream = stream
            self._device_name = wanted
            self._level = 0.0
        return True

    def _handle_indata(self, indata, _frames, _time_info, status) -> None:
        if status:
            self._logger.debug("microphone level monitor status: %s", status)
        try:
            samples = memoryview(indata).cast("h")
            total = 0
            for value in samples:
                total += value * value
            count = len(samples)
            if count == 0:
                return
            rms = math.sqrt(total / count) / 32768.0
        except Exception:  # noqa: BLE001 - 认不出的缓冲直接跳过
            return
        level = self._normalize(rms)
        with self._lock:
            if self._stream is None:
                return
            self._level = level

    def _normalize(self, rms: float) -> float:
        if rms <= 0.0:
            return 0.0
        span = self._CEIL_DB - self._FLOOR_DB
        if span <= 0:
            return 0.0
        db = 20.0 * math.log10(rms)
        return min(1.0, max(0.0, (db - self._FLOOR_DB) / span))

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._device_name = ""
            self._level = 0.0
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except BaseException as exc:  # noqa: BLE001 - 关闭失败不再重试
            self._logger.debug(
                "microphone level monitor close failed: error_type=%s",
                type(exc).__name__,
            )

