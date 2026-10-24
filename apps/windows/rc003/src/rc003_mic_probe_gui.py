"""RC003 开麦手动测试面板（独立诊断工具，不进入正式产品路径）。

用途：
1. 连接已配对的小米遥控器 2 Pro（RC003）；
2. 「① 点击开麦」：向遥控器写一次主机侧 MIC_OPEN（0x0C），2 秒后自动
   MIC_CLOSE，模拟“主机收到话筒键后开一次麦”；
3. 「② 短按持续开麦」：写 MIC_OPEN 后不自动关闭，观察一次短按能否让
   遥控器持续送音频；
4. 右侧电平表直接把解码后的 PCM 画成电平柱，判断音频到底有没有流动。

运行（先退出正式桥接，避免同一台遥控器的 GATT/单实例争用）：
    cd d:\\无线麦\\windows-remote-mic-app\\apps\\windows\\rc003
    .\\.venv\\Scripts\\python.exe src\\rc003_mic_probe_gui.py
"""

from __future__ import annotations

import asyncio
import math
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ovb_rc003 import atvv_protocol as proto  # noqa: E402
from ovb_rc003 import atvv_session, ble_transport_winrt, identity  # noqa: E402

BLOCKS = 48
METER_WIDTH = 520
METER_HEIGHT = 64
UI_TICK_MS = 50
AUTO_CLOSE_MS = 2000


class LoopThread:
    """在后台线程里跑一个独立 asyncio 事件循环，供 WinRT 异步调用使用。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="rc003-probe-loop", daemon=True)
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


class MeterState:
    """BLE 工作线程写入、Tk 线程读取的电平/计数快照。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rms = 0.0
        self.peak = 0
        self.frames = 0
        self.samples = 0
        self.last_audio_at = 0.0
        self.caps_text = "-"
        self.mic_open = False

    def add_samples(self, samples) -> None:
        if not samples:
            return
        peak = max(abs(sample) for sample in samples)
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        with self.lock:
            self.rms = rms
            self.peak = max(self.peak, peak)
            self.frames += 1
            self.samples += len(samples)
            self.last_audio_at = time.monotonic()

    def begin_session(self) -> None:
        with self.lock:
            self.rms = 0.0
            self.peak = 0
            self.frames = 0
            self.samples = 0
            self.last_audio_at = 0.0

    def snapshot(self):
        with self.lock:
            return (
                self.rms,
                self.peak,
                self.frames,
                self.samples,
                self.last_audio_at,
            )


class ProbeWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.loop_thread = LoopThread()
        self.session = None
        self.candidates = []
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        # 后台 loop 线程不能直接碰 Tk；回调统一入队，由 _tick 在主线程执行。
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.meter = MeterState()
        self._caps_ready = False
        self._connected_at = 0.0
        self._closing = False
        self._connecting = False
        self._holding = False
        self._history = deque([-60.0] * BLOCKS, maxlen=BLOCKS)
        self._peak_hold_db = -60.0
        self._blocks = []

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("提示：先退出正式桥接程序（托盘退出），否则同一台遥控器可能被占用。")
        self.root.after(200, self._refresh_devices)
        self.root.after(UI_TICK_MS, self._tick)
        self.root.after(80, self._drain_log)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.root.title("RC003 开麦测试面板")
        self.root.geometry("860x600")
        self.root.minsize(820, 540)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        default_font = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", default_font)

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        device_row = ttk.Frame(outer)
        device_row.pack(fill="x")
        ttk.Label(device_row, text="设备").pack(side="left")
        self.device_combo = ttk.Combobox(device_row, state="readonly", width=38)
        self.device_combo.pack(side="left", padx=(8, 8))
        self.refresh_btn = ttk.Button(device_row, text="刷新设备", command=self._refresh_devices)
        self.refresh_btn.pack(side="left")
        self.connect_btn = ttk.Button(device_row, text="连接", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=(8, 0))

        mode_row = ttk.Frame(outer)
        mode_row.pack(fill="x", pady=(8, 0))
        ttk.Label(mode_row, text="连接协商").pack(side="left")
        self.mode_var = tk.StringVar(value="ptt")
        ttk.Radiobutton(
            mode_row, text="标准 PTT+HTT（0x03）", value="ptt", variable=self.mode_var
        ).pack(side="left", padx=(10, 6))
        ttk.Radiobutton(
            mode_row, text="On-request（0x00，旧探针用）", value="on_request", variable=self.mode_var
        ).pack(side="left")

        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(outer, textvariable=self.status_var, foreground="#555").pack(
            fill="x", pady=(8, 0)
        )

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.open_btn = ttk.Button(
            actions, text="① 点击开麦（开 2 秒后自动关）", command=self._send_open_once
        )
        self.open_btn.pack(side="left")
        self.hold_btn = ttk.Button(
            actions, text="② 短按持续开麦（不自动关）", command=self._send_hold
        )
        self.hold_btn.pack(side="left", padx=(8, 0))
        self.close_btn = ttk.Button(
            actions, text="③ 停止 / 发 MIC_CLOSE", command=self._send_close
        )
        self.close_btn.pack(side="left", padx=(8, 0))

        meter_frame = ttk.LabelFrame(outer, text="遥控器上行音频电平（解码后 PCM）", padding=10)
        meter_frame.pack(fill="x", pady=(12, 0))

        self.canvas = tk.Canvas(
            meter_frame,
            width=METER_WIDTH,
            height=METER_HEIGHT + 26,
            bg="#11151c",
            highlightthickness=1,
            highlightbackground="#2a3140",
        )
        self.canvas.pack(side="left")
        self._build_meter_blocks()

        readout = ttk.Frame(meter_frame)
        readout.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.db_var = tk.StringVar(value="无信号")
        self.frames_var = tk.StringVar(value="PCM 帧：0")
        self.raw_var = tk.StringVar(value="BLE 音频通知：0 / 0 B")
        self.last_var = tk.StringVar(value="最近音频：-")
        self.caps_var = tk.StringVar(value="能力：-")
        for var in (self.db_var, self.frames_var, self.raw_var, self.last_var, self.caps_var):
            ttk.Label(readout, textvariable=var).pack(anchor="w")

        log_frame = ttk.LabelFrame(outer, text="事件日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _build_meter_blocks(self) -> None:
        gap = 2
        width = (METER_WIDTH - 16 - (BLOCKS - 1) * gap) / BLOCKS
        for index in range(BLOCKS):
            x0 = 8 + index * (width + gap)
            rect = self.canvas.create_rectangle(
                x0, 8, x0 + width, 8 + METER_HEIGHT, fill="#1b2230", outline=""
            )
            self._blocks.append(rect)
        for label, fraction in (("-60", 0.0), ("-30", 0.5), ("-12", 0.8), ("0", 1.0)):
            x = 8 + fraction * (METER_WIDTH - 16)
            self.canvas.create_text(
                x, METER_HEIGHT + 17, text=label, fill="#7d8590", anchor="center"
            )
        self._peak_line = self.canvas.create_line(
            9, 8, 9, 8 + METER_HEIGHT, fill="#e6e8eb", width=2, state="hidden"
        )

    # -------------------------------------------------------------- logging

    def _post(self, callback, *args) -> None:
        self.ui_queue.put((callback, args))

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_queue.put(f"[{stamp}] {message}")

    def _drain_log(self) -> None:
        if self._closing:
            return
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            # 只保留最后 800 行，避免长时间测试后卡顿。
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > 800:
                self.log_text.delete("1.0", f"{total - 800}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(80, self._drain_log)

    # ------------------------------------------------------------ discovery

    def _refresh_devices(self) -> None:
        if self._connecting or self.session is not None:
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
            self._log(f"扫描失败：{type(exc).__name__}: {exc}")
            return

        self.candidates = identity.qualifying_candidates(candidates)
        values = [f"{item.name}  #{index + 1}" for index, item in enumerate(self.candidates)]
        self.device_combo["values"] = values
        if values:
            self.device_combo.current(0)
            self.status_var.set(f"找到 {len(values)} 个 RC003 记录，选择后点“连接”。")
            self._log(f"发现 {len(values)} 个 RC003 候选。")
        else:
            self.device_combo.set("")
            self.status_var.set("没有找到已配对的 RC003。")
            self._log("没有找到名称匹配的 RC003；请先在 Windows 蓝牙设置里配对。")

    # ------------------------------------------------------------ connection

    def _toggle_connect(self) -> None:
        if self._connecting:
            return
        if self.session is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        if not self.candidates:
            messagebox.showwarning("RC003 测试", "请先刷新并选择一个 RC003 设备。")
            return
        choice = self.device_combo.current()
        if choice < 0 or choice >= len(self.candidates):
            messagebox.showwarning("RC003 测试", "请先选择一个设备。")
            return

        candidate = self.candidates[choice]
        on_request = self.mode_var.get() == "on_request"
        self._connecting = True
        self._caps_ready = False
        self.connect_btn.configure(state="disabled")
        self.status_var.set("正在连接…")
        self._log(
            "连接 "
            f"{candidate.name}，协商={'On-request 0x00' if on_request else 'PTT+HTT 0x03'}"
        )
        future = self.loop_thread.submit(self._connect_async(candidate, on_request))
        future.add_done_callback(lambda done: self._post(self._on_connected, done))

    async def _connect_async(self, candidate, on_request: bool):
        session = ble_transport_winrt.RC003BleSession(
            on_pcm_frame=self._on_pcm,
            on_control_event=self._on_control,
            on_error=self._on_error,
            on_disconnected=self._on_disconnected,
            get_capabilities_command=(
                proto.GET_CAPABILITIES_ON_REQUEST_V10
                if on_request
                else proto.GET_CAPABILITIES_V10
            ),
            loop=self.loop_thread.loop,
        )
        await session.connect(candidate)
        self.session = session
        return session

    def _on_connected(self, future) -> None:
        self._connecting = False
        self.connect_btn.configure(state="normal")
        if self._closing:
            return
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            self.session = None
            self.status_var.set(f"连接失败：{type(exc).__name__}")
            self._log(f"连接失败：{type(exc).__name__}: {exc}")
            return
        self._connected_at = time.monotonic()
        self.connect_btn.configure(text="断开")
        self.status_var.set("已连接，等待 CAPS…")
        self._log("GATT 已连接，已订阅 ATVV control/audio，已发送 GET_CAPABILITIES。")

    def _disconnect(self) -> None:
        session = self.session
        self.session = None
        self._holding = False
        self._caps_ready = False
        self.meter.begin_session()
        self.meter.caps_text = "-"
        self.connect_btn.configure(text="连接", state="disabled")
        self.status_var.set("正在断开…")
        if session is None:
            self.connect_btn.configure(state="normal")
            return
        self._log("断开连接（若麦克风仍开，会先发 MIC_CLOSE）。")
        future = self.loop_thread.submit(session.close())
        future.add_done_callback(
            lambda done: self._post(self._on_disconnected_done, done)
        )

    def _on_disconnected_done(self, future) -> None:
        self.connect_btn.configure(state="normal")
        self.status_var.set("未连接")
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            self._log(f"断开清理未完成：{type(exc).__name__}: {exc}")

    def _on_disconnected(self) -> None:
        self._log("BLE 连接已断开。")

    # --------------------------------------------------------- ATVV callbacks

    def _on_pcm(self, samples) -> None:
        self.meter.add_samples(samples)

    def _on_control(self, event) -> None:
        if isinstance(event, atvv_session.CapsReceived):
            caps = event.capabilities
            self.meter.caps_text = (
                f"能力：v0x{caps.version:04X} codec=0x{caps.codecs:02X} "
                f"interaction=0x{caps.interaction:02X} frame={caps.frame_size} "
                f"rate={caps.sample_rate:.0f}Hz"
            )
            self._caps_ready = True
            self._log(self.meter.caps_text)
        elif isinstance(event, atvv_session.AudioStarted):
            self.meter.begin_session()
            self.meter.mic_open = True
            self._log(
                "← AUDIO_START "
                f"(session={event.session_id} reason={event.reason} codec={event.codec})"
            )
        elif isinstance(event, atvv_session.AudioStopped):
            self.meter.mic_open = False
            self._log(f"← AUDIO_STOP (reason={event.reason})")
        elif isinstance(event, atvv_session.MicButtonPressed):
            self._log("← MIC_BUTTON (0x08)：遥控器报告话筒键被按下")
        elif isinstance(event, atvv_session.AudioSynced):
            self._log("← AUDIO_SYNC")
        elif isinstance(event, atvv_session.UnknownControl):
            self._log(f"← 未知控制事件 opcode=0x{event.opcode:02X}")
        else:
            self._log(f"← 控制事件 {event!r}")

    def _on_error(self, exc: BaseException) -> None:
        self._log(f"错误：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- commands

    def _ready_for_command(self) -> bool:
        if self.session is None:
            return False
        # CAPS 一般立即到达；等 3 秒仍没来也允许试，避免面板被卡死。
        return self._caps_ready or (time.monotonic() - self._connected_at) > 3.0

    def _send_open_once(self) -> None:
        if not self._ready_for_command():
            self._log("尚未连接或仍在等待 CAPS。")
            return
        self.session.send_mic_open_threadsafe()
        self._log("→ MIC_OPEN（点击模式，2 秒后自动 MIC_CLOSE）")
        self.root.after(AUTO_CLOSE_MS, self._auto_close)

    def _auto_close(self) -> None:
        if self._closing or self.session is None or self._holding:
            return
        self.session.send_mic_close_threadsafe()
        self._log("→ MIC_CLOSE（点击模式自动结束）")

    def _send_hold(self) -> None:
        if not self._ready_for_command():
            self._log("尚未连接或仍在等待 CAPS。")
            return
        self._holding = True
        self.session.send_mic_open_threadsafe()
        self._log("→ MIC_OPEN（持续模式：不会自动关闭，观察电平是否持续有值）")

    def _send_close(self) -> None:
        if self.session is None:
            self._log("尚未连接。")
            return
        self._holding = False
        self.session.send_mic_close_threadsafe()
        self._log("→ MIC_CLOSE（手动停止）")

    # ------------------------------------------------------------- UI tick

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
            except Exception as exc:  # noqa: BLE001 - 诊断面板不因单个回调中断
                self._log(f"回调异常：{type(exc).__name__}: {exc}")
        if self._closing:
            return
        rms, peak, frames, samples, last_audio_at = self.meter.snapshot()
        session = self.session
        connected = session is not None
        now = time.monotonic()

        if connected:
            mic_open = bool(getattr(session, "_session").mic_open)
            status = "已连接 · 麦克风开" if mic_open else "已连接 · 麦克风关"
            if self._caps_ready:
                status += " · CAPS 已收到"
            self.status_var.set(status)
        elif not self._connecting:
            self.status_var.set("未连接")

        ready = self._ready_for_command()
        state = "normal" if ready else "disabled"
        self.open_btn.configure(state=state)
        self.hold_btn.configure(state=state)
        self.close_btn.configure(state="normal" if connected else "disabled")
        self.refresh_btn.configure(
            state="disabled" if (connected or self._connecting) else "normal"
        )

        db = 20.0 * math.log10(rms / 32768.0) if rms > 0.0 else -60.0
        db = max(-60.0, min(0.0, db))
        self._history.append(db)
        self._peak_hold_db = max(db, self._peak_hold_db - 0.8)
        self._paint_meter(db)

        fresh = last_audio_at > 0.0 and (now - last_audio_at) < 0.3
        if fresh:
            self.db_var.set(f"{db:.1f} dBFS")
        elif last_audio_at > 0.0:
            self.db_var.set(f"无信号（最近一次 {now - last_audio_at:.1f}s 前）")
        else:
            self.db_var.set("无信号（本次会话还没收到 PCM）")

        self.frames_var.set(
            f"PCM 帧：{frames} · 样本：{samples} · 峰值：{peak}"
        )
        if session is not None:
            raw_notifications = getattr(session, "_raw_audio_notifications", 0)
            raw_bytes = getattr(session, "_raw_audio_bytes", 0)
            self.raw_var.set(f"BLE 音频通知：{raw_notifications} / {raw_bytes} B")
        else:
            self.raw_var.set("BLE 音频通知：0 / 0 B")
        self.last_var.set(
            "最近音频：-" if last_audio_at <= 0.0 else f"最近音频：{now - last_audio_at:.2f}s 前"
        )
        self.caps_var.set(self.meter.caps_text)

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
            self.canvas.coords(self._peak_line, x, 8, x, 8 + METER_HEIGHT)
            self.canvas.itemconfigure(self._peak_line, state="normal")

    # ---------------------------------------------------------------- close

    def _on_close(self) -> None:
        self._closing = True
        session = self.session
        self.session = None
        if session is not None:
            try:
                self.loop_thread.submit(session.close()).result(timeout=4.0)
            except Exception:  # noqa: BLE001 - 关闭窗口时不再打扰用户
                pass
        self.loop_thread.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ProbeWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
