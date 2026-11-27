"""Select the HID report source for a Google receiver.

Primary path hooks the Windows driver callback (needs a known build hash or a
public PDB). When Microsoft never published symbols for that driver build, the
receiver falls back to subscribing to the HID-over-GATT Report characteristic,
which delivers the same report bytes without any private symbol.
"""
from __future__ import annotations

import asyncio

from .chromecast_hid_gatt_windows import GattHidTap
from .chromecast_hid_tap_windows import SYMBOL_REASONS, HidTap
from .chromecast_pipe_windows import PipeError


class HidSource:
    def __init__(self, entity, device, observation=None):
        self.entity = entity
        self.device = device
        self.observation = observation
        self.source = None
        self.fallback = False

    def _record_symbol(self, tap):
        if self.observation is not None and tap.symbol is not None:
            self.observation.symbol_probe(**tap.symbol)

    async def start(self):
        tap = HidTap(self.entity)
        try:
            await asyncio.to_thread(tap.start)
        except Exception as error:
            self._record_symbol(tap)
            if str(error) not in SYMBOL_REASONS:
                raise
        else:
            self._record_symbol(tap)
            self.source = tap
            return
        # 驱动符号不可用：改用 HID over GATT 直连，报文与驱动内部同源。
        if self.observation is not None:
            self.observation.stage("hid_fallback_start")
        gatt = GattHidTap(self.device)
        try:
            await gatt.start()
        except Exception:
            raise PipeError("hid_gatt_unavailable") from None
        self.fallback = True
        self.source = gatt
        if self.observation is not None:
            self.observation.stage("hid_fallback_ready")

    def poll(self):
        return [] if self.source is None else self.source.poll()

    def source_alive(self):
        return self.source is not None and self.source.source_alive()

    def close(self):
        source, self.source = self.source, None
        if source is not None:
            source.close()
