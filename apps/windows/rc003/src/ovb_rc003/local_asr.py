"""本地离线语音识别：sherpa-onnx + SenseVoice（不联网、不需要密钥）。

遥控器录到的 PCM 直接交给本地模型转成文字，结果是一个字符串，交给 OpenMexia
去执行。模型加载和识别都跑在后台线程，不占用 Raw Input / BLE 回调。
"""

from __future__ import annotations

import array
import logging
import queue
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

MODEL_DIR_NAME = "sense-voice"
MODEL_FILE = "model.int8.onnx"
TOKENS_FILE = "tokens.txt"
MODEL_FILES = (MODEL_FILE, TOKENS_FILE)
SAMPLE_RATE = 16000
MODEL_BASE_URLS = (
    # 国内镜像，默认走这个。
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main",
    # 镜像不通时回退到 Hugging Face 官方地址。
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main",
)
DOWNLOAD_TIMEOUT_SECONDS = 30.0


class LocalAsrError(RuntimeError):
    """识别没做成：模型没装、没录到声音、或者没听清内容。"""


def model_dir(config_root: Path) -> Path:
    """模型放在配置目录下，和日志、更新共用一个根路径。"""

    return Path(config_root) / "models" / MODEL_DIR_NAME


def model_available(config_root: Path) -> bool:
    base = model_dir(config_root)
    return (base / MODEL_FILE).is_file() and (base / TOKENS_FILE).is_file()


def _download(url: str, destination: Path) -> None:
    """先写 .part，下完再改名，避免半截文件被当成已安装。"""

    part = destination.with_name(destination.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            with part.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
        part.replace(destination)
    except (urllib.error.URLError, OSError) as exc:
        try:
            part.unlink()
        except OSError:
            pass
        raise LocalAsrError("下载语音识别模型失败，请检查网络后重试") from exc


def install_model(config_root: Path) -> None:
    """按用户点击下载 SenseVoice 模型；已存在的文件直接跳过。"""

    base = model_dir(config_root)
    base.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        target = base / name
        if target.is_file():
            continue
        error: Optional[LocalAsrError] = None
        for model_base_url in MODEL_BASE_URLS:
            try:
                _download(f"{model_base_url}/{name}", target)
                error = None
                break
            except LocalAsrError as exc:
                error = exc
        if error is not None:
            raise error
    if not model_available(config_root):
        raise LocalAsrError("语音识别模型下载不完整，请重试")
    _LOGGER.info("local asr model installed: %s", base)


def remove_model(config_root: Path) -> None:
    """卸载模型文件；目录空了顺手删掉。"""

    base = model_dir(config_root)
    for name in MODEL_FILES:
        try:
            (base / name).unlink(missing_ok=True)
        except OSError as exc:
            raise LocalAsrError("模型文件正在被占用，请重启软件后再卸载") from exc
    try:
        base.rmdir()
    except OSError:
        pass
    _LOGGER.info("local asr model removed: %s", base)


def pcm_to_samples(pcm: bytes) -> list:
    """16bit 小端 PCM → [-1, 1] 的浮点采样。"""

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return [value / 32768.0 for value in samples]


def write_wav(path: Path, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> None:
    """调试用：把一段 PCM 存成 wav，方便复现识别问题。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


class LocalSpeechClient:
    """懒加载 SenseVoice 模型，之后复用同一个识别器。"""

    def __init__(self, config_root: Path) -> None:
        self._config_root = Path(config_root)
        self._recognizer = None
        self._lock = threading.Lock()

    def transcribe(self, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> str:
        if not pcm:
            raise LocalAsrError("没有录到声音")
        recognizer = self._ensure_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, pcm_to_samples(pcm))
        recognizer.decode_stream(stream)
        text = str(stream.result.text or "").strip()
        if not text:
            raise LocalAsrError("没有听清内容")
        return text

    def _ensure_recognizer(self):
        with self._lock:
            if self._recognizer is not None:
                return self._recognizer
            base = model_dir(self._config_root)
            model = base / MODEL_FILE
            tokens = base / TOKENS_FILE
            if not (model.is_file() and tokens.is_file()):
                raise LocalAsrError("本地语音模型还没准备好")
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise LocalAsrError("缺少 sherpa-onnx，本地语音识别不可用") from exc
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model),
                tokens=str(tokens),
                num_threads=2,
                use_itn=True,
                language="zh",
                debug=False,
            )
            _LOGGER.info("local asr model loaded: %s", model)
            return self._recognizer


class LocalAsrRunner:
    """后台线程做识别，完成后把结果交给回调（回调在 worker 线程里执行）。"""

    def __init__(
        self, config_root: Path, client: Optional[LocalSpeechClient] = None
    ) -> None:
        self._client = client or LocalSpeechClient(config_root)
        self._queue: "queue.Queue[Optional[Tuple[bytes, Callable[[str, Optional[str]], None]]]]" = (
            queue.Queue()
        )
        self._thread = threading.Thread(target=self._run, name="local-asr", daemon=True)
        self._thread.start()

    def submit(
        self, pcm: bytes, *, on_result: Callable[[str, Optional[str]], None]
    ) -> None:
        """排队识别一段录音；调用方立即返回。"""

        self._queue.put((bytes(pcm), on_result))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            pcm, on_result = item
            text = ""
            error: Optional[str] = None
            try:
                text = self._client.transcribe(pcm)
            except LocalAsrError as exc:
                error = str(exc)
            except BaseException as exc:  # noqa: BLE001 - 不能拖垮后台线程
                error = "语音识别出错"
                _LOGGER.warning("local asr error: error_type=%s", type(exc).__name__)
            try:
                on_result(text, error)
            except BaseException as exc:  # noqa: BLE001 - 回调异常也不能退出线程
                _LOGGER.warning(
                    "local asr callback error: error_type=%s", type(exc).__name__
                )

    def close(self) -> None:
        self._queue.put(None)
