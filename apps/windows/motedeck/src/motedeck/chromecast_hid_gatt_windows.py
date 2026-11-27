"""Fallback HID source: read the remote's HID-over-GATT Report notifications.

Only used when the Windows driver callback cannot be located (for example a
driver build whose PDB Microsoft never published). The notifications carry the
same report bytes the driver sees, so no private symbols are involved.

Never writes reports and never clears the CCCD: the Windows HID driver shares
the same subscription, so clearing it would break ordinary system input.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid

from . import remote_layout

HID_SERVICE = uuid.UUID("00001812-0000-1000-8000-00805f9b34fb")
REPORT_CHARACTERISTIC = uuid.UUID("00002a4d-0000-1000-8000-00805f9b34fb")
_CONSUMER_CODES = {usage: remote_layout.CHROMECAST_REPORT_CODES[key]
                   for key, usage in remote_layout.CHROMECAST_CONSUMER_USAGES.items()}
_BUTTON_CODES = frozenset(remote_layout.CHROMECAST_REPORT_CODES.values())


def decode_report(value: bytes):
    """GATT 报文 -> 与驱动回调一致的八字节报告；无法识别时返回 None。"""
    if len(value) == 3 and value[0] == 0x02:
        # 少数系统会在消费类报文前补上报告 ID，去掉后再解析。
        value = value[1:]
    if len(value) == 2:
        usage = value[0] | (value[1] << 8)
        if usage == 0:
            return bytes(8)
        code = _CONSUMER_CODES.get(usage)
        if code is None:
            return None
        return bytes([code]) + bytes(7)
    if len(value) != 8:
        return None
    if any(value[1:]) or (value[0] and value[0] not in _BUTTON_CODES):
        return None
    return value


class GattHidTap:
    def __init__(self, device):
        self.device = device
        self.events = queue.Queue(maxsize=256)
        self.overflow = threading.Event()
        self.service = None
        self.tokens = []

    def _put(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.overflow.set()

    async def start(self):
        import asyncio
        from winrt.windows.devices.bluetooth import BluetoothCacheMode
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCharacteristicProperties,
            GattClientCharacteristicConfigurationDescriptorValue as CccdValue,
        )
        from .chromecast_pipe_windows import PipeError
        device = getattr(self.device, "device", None)
        if device is None:
            raise PipeError("hid_gatt_unavailable")
        try:
            result = await asyncio.wait_for(
                device.get_gatt_services_for_uuid_with_cache_mode_async(HID_SERVICE, BluetoothCacheMode.UNCACHED), 5)
        except Exception:
            raise PipeError("hid_gatt_unavailable") from None
        services = list(result.services)
        if int(result.status) != 0 or len(services) != 1:
            for service in services:
                try:
                    service.close()
                except Exception:
                    pass
            raise PipeError("hid_gatt_unavailable")
        self.service = services[0]
        try:
            result = await asyncio.wait_for(
                self.service.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED), 5)
        except Exception:
            self.close()
            raise PipeError("hid_gatt_unavailable") from None
        subscribed = 0
        for characteristic in result.characteristics:
            if str(characteristic.uuid).lower() != str(REPORT_CHARACTERISTIC):
                continue
            properties = characteristic.characteristic_properties
            if properties & GattCharacteristicProperties.NOTIFY:
                mode = CccdValue.NOTIFY
            elif properties & GattCharacteristicProperties.INDICATE:
                mode = CccdValue.INDICATE
            else:
                continue
            try:
                status = await asyncio.wait_for(
                    characteristic.write_client_characteristic_configuration_descriptor_async(mode), 3)
            except Exception:
                continue
            if int(status) != 0:
                continue
            token = characteristic.add_value_changed(
                lambda _sender, args: self._on_value(args))
            self.tokens.append((characteristic, token))
            subscribed += 1
        if not subscribed:
            self.close()
            raise PipeError("hid_gatt_unavailable")
        self._put(("hook_ready", "", time.monotonic()))

    def _on_value(self, args):
        try:
            value = bytes(args.characteristic_value)
        except Exception:
            return
        decoded = decode_report(value)
        if decoded is not None:
            self._put(("report", decoded.hex(), time.monotonic()))

    def poll(self):
        if self.overflow.is_set():
            return [("error", "hid_queue_overflow", 0.0)]
        result = []
        for _ in range(128):
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        return result

    def source_alive(self):
        try:
            from winrt.windows.devices.bluetooth import BluetoothConnectionStatus
            return (getattr(self.device, "device", None) is not None
                    and self.device.device.connection_status == BluetoothConnectionStatus.CONNECTED)
        except Exception:
            return False

    def close(self):
        # 只摘掉自己的回调，不写 CCCD：驱动仍在用同一个订阅。
        tokens, self.tokens = self.tokens, []
        for characteristic, token in tokens:
            try:
                characteristic.remove_value_changed(token)
            except Exception:
                pass
        if self.service is not None:
            try:
                self.service.close()
            except Exception:
                pass
            self.service = None
