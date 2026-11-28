"""Chromecast service lifecycle; receiver ownership stays in Client.

Application services provide shared input and status operations. This owner keeps
its receiver and voice host until both confirm cleanup, preserving switch safety.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable, Any
from . import bridge_runtime_status


@dataclass(frozen=True)
class RuntimeServices:
    selected_key: Callable[[], str]
    logger: Any
    create_voice_host: Callable
    set_input_state: Callable
    publish_status: Callable
    start_input: Callable
    stop_input: Callable
    disable_input: Callable
    cancel_mappings: Callable
    release_inputs: Callable
    route_edge: Callable
    start_battery_monitor: Callable | None = None
    trace_enabled: Callable[[], bool] | None = None


class ChromecastRuntime:
    def __init__(self, services: RuntimeServices):
        self.services = services
        self.client = None
        self.voice_host = None
        self.stopping = threading.Event()
        self._battery_monitor = None

    async def stop(self):
        self.stopping.set()
        if self.voice_host is not None:
            await asyncio.to_thread(self.voice_host.stop)
        if self.client is not None:
            await asyncio.to_thread(self.client.stop)

    def cancel_input(self):
        if self.voice_host is not None:
            self.voice_host.request_stop()
        self.services.cancel_mappings()

    def on_edge(self, edge):
        if (self.client is None or edge.entity != self.services.selected_key()
                or edge.generation != self.client.identity.generation):
            return
        self.services.route_edge(edge)

    def on_reopen(self, ready):
        # 接收进程在同一个提权上下文里自愈：断开时显示等待设备，
        # 重新附着成功后恢复“已连接”，不需要用户手动重启服务。
        if self.stopping.is_set():
            return
        if ready:
            self.services.set_input_state(raw_input_state="chromecast_ready", hid_tap_state="ready")
            self.services.publish_status(bridge_runtime_status.BridgeConnectionState.CONNECTED)
        else:
            self.services.set_input_state(raw_input_state="chromecast_reconnecting", hid_tap_state="starting")
            self.services.publish_status(bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE)

    async def run(self) -> None:
        from .chromecast_client import Client, message
        self.services.set_input_state(raw_input_state="starting", hid_tap_state="starting",
                                      voice_key_physicalizer_state="not_used")
        self.services.publish_status(bridge_runtime_status.BridgeConnectionState.CONNECTING)
        self.voice_host = self.services.create_voice_host()
        self.client = Client(self.services.selected_key(), mode="run", on_edge=self.on_edge,
                                  on_lost=self.cancel_input,
                                  on_voice=self.voice_host.enqueue,
                                  on_reopen=self.on_reopen,
                                  trace_enabled=self.services.trace_enabled)
        self.voice_host.start(self.client)
        try:
            # Keyboard ownership is shared safety infrastructure, not the
            # Xiaomi remote decoder. Start it before accepting host shortcuts.
            self.services.start_input()
            await asyncio.to_thread(self.client.start, cancel_event=self.stopping)
            self.services.set_input_state(raw_input_state="chromecast_ready", hid_tap_state="ready")
            self.services.publish_status(bridge_runtime_status.BridgeConnectionState.CONNECTED)
            # 电量是可选遥测：读不到不影响遥控器本身的连接。
            if self.services.start_battery_monitor is not None:
                try:
                    self._battery_monitor = await self.services.start_battery_monitor()
                except Exception as exc:
                    self.services.logger.warning(
                        "Chromecast battery monitor unavailable: error_type=%s",
                        type(exc).__name__,
                    )
            while not self.stopping.is_set() and not self.client.finished.is_set():
                await asyncio.sleep(.05)
            if self.client.finished.is_set() and not self.stopping.is_set():
                raise RuntimeError(message(self.client.reason))
        except Exception:
            self.services.disable_input()
            self.services.set_input_state(raw_input_state="chromecast_" + self.client.reason,
                                          hid_tap_state="failed")
            self.services.publish_status(bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE)
            self.services.logger.warning("Chromecast receiver: stage=%s code=%s; %s",
                                 self.client.failure_stage, self.client.failure_code,
                                 message(self.client.reason))
            # Preserve a visible reason until the user stops/restarts; do not
            # automatically repeat permission prompts after a failed start.
            while not self.stopping.is_set():
                await asyncio.sleep(.1)
        finally:
            # Keep ownership on teardown failure; the desktop must not switch
            # entities over a receiver whose exit has not been confirmed.
            self.services.logger.info("Chromecast shutdown: stage=cancel_input state=begin")
            self.cancel_input()
            self.services.logger.info("Chromecast shutdown: stage=cancel_input state=done")
            if self._battery_monitor is not None:
                self._battery_monitor.stop()
                self._battery_monitor = None
            cleanup_last_log = {}
            while True:
                try:
                    for stage, cleanup in (("voice_host", self.voice_host.stop),
                                           ("receiver", self.client.stop),
                                           ("input_channels", self.services.stop_input)):
                        started = time.monotonic()
                        report = started - cleanup_last_log.get(stage, float("-inf")) >= 5
                        if report:
                            cleanup_last_log[stage] = started
                            self.services.logger.info("Chromecast shutdown: stage=%s state=begin", stage)
                        await asyncio.to_thread(cleanup)
                        if report:
                            self.services.logger.info("Chromecast shutdown: stage=%s state=done elapsed_ms=%.0f",
                                              stage, (time.monotonic() - started) * 1000)
                    break
                except RuntimeError as exc:
                    if report:
                        self.services.logger.warning("Chromecast shutdown: stage=%s state=retry error_type=%s elapsed_ms=%.0f",
                                             stage, type(exc).__name__, (time.monotonic() - started) * 1000)
                    # Remain the live bridge owner, so stop/switch/exit cannot
                    # report success over an unconfirmed elevated receiver.
                    self.services.set_input_state(raw_input_state="failed_stopping")
                    await asyncio.sleep(.25)
                except Exception as exc:
                    self.services.logger.error("Chromecast shutdown: stage=%s state=error error_type=%s",
                                       stage, type(exc).__name__)
                    raise
            self.client = None
            self.voice_host = None
            self.services.logger.info("Chromecast shutdown: stage=release_inputs state=begin")
            while True:
                released = self.services.release_inputs()
                if released:
                    break
                self.services.set_input_state(raw_input_state="failed_stopping")
                await asyncio.sleep(.25)
            self.services.set_input_state(raw_input_state="stopped")
            self.services.logger.info("Chromecast shutdown: stage=release_inputs state=done")
