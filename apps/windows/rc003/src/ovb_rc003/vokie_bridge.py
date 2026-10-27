"""Loopback bridge that lets the Vokie plugin hand its voice modes to MoteDeck.

MoteDeck is the server here: the settings window asks for a connection, this
module listens on 127.0.0.1 with an ephemeral port and drops a tiny discovery
file into the configuration root.  The Vokie plugin worker reads that file,
opens a plain TCP socket and speaks newline-delimited JSON.  Keeping the
transport that small means the plugin needs no native code, and MoteDeck keeps
owning the port and the shared token.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal, Slot

from . import __version__, product_identity

_LOGGER = logging.getLogger(__name__)

PROTOCOL_NAME = "motedeck-vokie-bridge"
BRIDGE_VERSION = 1
DISCOVERY_FILE_NAME = "vokie-bridge.json"
MAX_LINE_BYTES = 64 * 1024
MAX_MODES = 32
_POLL_SECONDS = 0.25
_JOIN_TIMEOUT_SECONDS = 2.0

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_CONNECTED = "connected"

_ACTIVE_BRIDGE: Optional["VokieBridge"] = None


def active_bridge() -> Optional["VokieBridge"]:
    """Return the bridge owned by the settings window, if any.

    The BLE audio callback runs on the bridge thread, which cannot touch Qt
    objects, so it reaches the bridge through this module-level handle.
    """

    return _ACTIVE_BRIDGE


def discovery_path(config_root: Path) -> Path:
    """Return the file the plugin worker polls for connection details."""

    return Path(config_root) / DISCOVERY_FILE_NAME


def normalize_modes(raw: object) -> List[Dict[str, str]]:
    """Return the plugin's voice modes in a shape the config layer accepts.

    A mode without a hotkey is dropped: the remote only replays shortcuts, so a
    row with nothing to replay would be a button that silently does nothing.
    """

    if not isinstance(raw, list):
        return []
    modes: List[Dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_hotkeys: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        mode_id = str(item.get("id", "")).strip().lower()[:64]
        name = str(item.get("name", "")).strip()[:64]
        hotkey = str(item.get("hotkey", "")).strip()[:120]
        if not mode_id or not name or not hotkey:
            continue
        if mode_id in seen_ids or hotkey.casefold() in seen_hotkeys:
            continue
        seen_ids.add(mode_id)
        seen_hotkeys.add(hotkey.casefold())
        modes.append({"id": mode_id, "name": name, "hotkey": hotkey})
        if len(modes) >= MAX_MODES:
            break
    return modes


class VokieBridge(QObject):
    """Own the listener, the token and the discovery file for one session."""

    stateChanged = Signal()
    modesReceived = Signal(list)
    pluginDisconnected = Signal()
    # Vokie 接下了一次语音会话：它的输入框就是这时才弹出来的。
    sessionAccepted = Signal()

    def __init__(self, config_root: Path, parent=None) -> None:
        super().__init__(parent)
        self._config_root = Path(config_root)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._server: Optional[socket.socket] = None
        self._connection: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._state = STATE_IDLE
        self._port = 0
        self._token = ""
        self._plugin_name = ""
        self._plugin_version = ""
        self._modes: List[Dict[str, str]] = []

    # -- QML-facing state -------------------------------------------------

    @Slot(result=str)
    def state(self) -> str:
        with self._lock:
            return self._state

    @Slot(result=bool)
    def connected(self) -> bool:
        with self._lock:
            return self._state == STATE_CONNECTED

    @Slot(result=str)
    def pluginName(self) -> str:
        with self._lock:
            return self._plugin_name

    @Slot(result=str)
    def pluginVersion(self) -> str:
        with self._lock:
            return self._plugin_version

    @Slot(result=int)
    def port(self) -> int:
        with self._lock:
            return self._port

    @Slot(result=list)
    def modes(self) -> list:
        with self._lock:
            return [dict(mode) for mode in self._modes]

    # -- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        """Open the listener and publish the discovery file."""

        with self._lock:
            if self._state != STATE_IDLE:
                return True
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                server.settimeout(_POLL_SECONDS)
            except OSError as exc:
                _LOGGER.warning("vokie bridge could not listen: %s", exc)
                return False
            self._stop_event.clear()
            self._server = server
            self._token = secrets.token_urlsafe(32)
            self._port = int(server.getsockname()[1])
            self._state = STATE_LISTENING
            try:
                self._write_discovery()
            except OSError as exc:
                _LOGGER.warning("vokie bridge discovery file failed: %s", exc)
                try:
                    server.close()
                except OSError:
                    pass
                self._server = None
                self._state = STATE_IDLE
                self._port = 0
                self._token = ""
                return False
            thread = threading.Thread(
                target=self._accept_loop,
                args=(server,),
                name="vokie-bridge",
                daemon=True,
            )
            self._accept_thread = thread
            thread.start()
        global _ACTIVE_BRIDGE
        _ACTIVE_BRIDGE = self
        self.stateChanged.emit()
        return True

    def stop(self) -> None:
        """Close the listener, drop the discovery file and reset the state."""

        global _ACTIVE_BRIDGE
        if _ACTIVE_BRIDGE is self:
            _ACTIVE_BRIDGE = None
        self._stop_event.set()
        self._close_connection()
        with self._lock:
            server = self._server
            self._server = None
            was_connected = self._state == STATE_CONNECTED
            self._state = STATE_IDLE
            self._port = 0
            self._token = ""
            self._plugin_name = ""
            self._plugin_version = ""
            self._modes = []
            thread = self._accept_thread
            self._accept_thread = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        self._remove_discovery()
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        if was_connected:
            self.stateChanged.emit()

    def disconnect_plugin(self) -> None:
        """Ask the plugin to go away but keep listening for a reconnect."""

        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is None:
            return
        self._send(connection, {"type": "goodbye"})
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    @Slot(result="QVariantMap")
    def describe(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "port": self._port,
                "pluginName": self._plugin_name,
                "pluginVersion": self._plugin_version,
                "modeCount": len(self._modes),
            }

    def send_config(self, payload: Dict[str, Any]) -> bool:
        """Push one configuration frame to the connected plugin."""

        with self._lock:
            connection = self._connection
        if connection is None:
            return False
        return self._send(connection, {"type": "config", **payload})

    # -- internals --------------------------------------------------------

    def _write_discovery(self) -> None:
        payload = {
            "protocol": PROTOCOL_NAME,
            "version": BRIDGE_VERSION,
            "app": product_identity.DISPLAY_NAME,
            "appVersion": __version__,
            "host": "127.0.0.1",
            "port": self._port,
            "token": self._token,
            "createdAt": int(time.time()),
        }
        path = discovery_path(self._config_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def _remove_discovery(self) -> None:
        try:
            discovery_path(self._config_root).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _close_connection(self) -> Optional[socket.socket]:
        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is None:
            return None
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
        return connection

    def _accept_loop(self, server: socket.socket) -> None:
        while not self._stop_event.is_set():
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._stop_event.is_set() or self._connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
                    continue
                self._connection = connection
            self._client_loop(connection)

    def _client_loop(self, connection: socket.socket) -> None:
        buffer = bytearray()
        greeted = False
        reason = ""
        try:
            connection.settimeout(_POLL_SECONDS)
            while not self._stop_event.is_set() and not reason:
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    reason = "socket_error"
                    break
                if not chunk:
                    reason = "closed"
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_LINE_BYTES * 4:
                    buffer.clear()
                    reason = "oversized"
                    break
                while b"\n" in buffer:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(raw) > MAX_LINE_BYTES:
                        reason = "oversized"
                        break
                    if greeted:
                        if not self._handle_message(connection, raw):
                            reason = "protocol_error"
                            break
                        continue
                    accepted, failure = self._handle_hello(connection, raw)
                    if not accepted:
                        reason = failure or "rejected"
                        break
                    greeted = True
        finally:
            self._finish_connection(connection, greeted, reason)

    def _handle_hello(
        self, connection: socket.socket, raw: bytes
    ) -> Tuple[bool, str]:
        payload = self._decode(raw)
        if payload is None or payload.get("type") != "hello":
            self._send(connection, {"type": "hello_rejected", "reason": "expected_hello"})
            return False, "expected_hello"
        token = payload.get("token")
        if not isinstance(token, str) or not self._token or not secrets.compare_digest(
            token, self._token
        ):
            self._send(connection, {"type": "hello_rejected", "reason": "bad_token"})
            return False, "bad_token"
        modes = normalize_modes(payload.get("modes"))
        info = payload.get("plugin")
        if not isinstance(info, dict):
            info = {}
        name = str(info.get("name") or info.get("id") or "Vokie").strip()[:64]
        version = str(info.get("version") or "").strip()[:32]
        self._send(
            connection,
            {
                "type": "hello_ok",
                "app": product_identity.DISPLAY_NAME,
                "appVersion": __version__,
                "protocol": PROTOCOL_NAME,
                "version": BRIDGE_VERSION,
                "modes": [{"id": mode["id"]} for mode in modes],
            },
        )
        with self._lock:
            if self._connection is not connection:
                return False, "superseded"
            self._state = STATE_CONNECTED
            self._plugin_name = name
            self._plugin_version = version
            self._modes = modes
        self.stateChanged.emit()
        self.modesReceived.emit([dict(mode) for mode in modes])
        _LOGGER.info(
            "vokie plugin connected: name=%s version=%s modes=%d",
            name,
            version,
            len(modes),
        )
        return True, ""

    def _handle_message(self, connection: socket.socket, raw: bytes) -> bool:
        payload = self._decode(raw)
        if payload is None:
            return False
        message_type = payload.get("type")
        if message_type == "ping":
            self._send(connection, {"type": "pong", "time": int(time.time())})
            return True
        if message_type == "modes":
            modes = normalize_modes(payload.get("modes"))
            if not modes:
                self._send(connection, {"type": "error", "reason": "no_modes"})
                return True
            with self._lock:
                self._modes = modes
            self.modesReceived.emit([dict(mode) for mode in modes])
            return True
        if message_type == "session_accepted":
            self.sessionAccepted.emit()
            return True
        if message_type == "bye":
            return False
        return True

    def _finish_connection(
        self, connection: socket.socket, greeted: bool, reason: str
    ) -> None:
        try:
            connection.close()
        except OSError:
            pass
        with self._lock:
            if self._connection is connection:
                self._connection = None
            was_connected = self._state == STATE_CONNECTED
            if was_connected:
                self._state = (
                    STATE_LISTENING if self._server is not None else STATE_IDLE
                )
                self._plugin_name = ""
                self._plugin_version = ""
                self._modes = []
        if not greeted:
            _LOGGER.info("vokie plugin handshake failed: %s", reason or "unknown")
            return
        _LOGGER.info("vokie plugin disconnected: %s", reason or "closed")
        if was_connected:
            self.stateChanged.emit()
            self.pluginDisconnected.emit()

    @staticmethod
    def _decode(raw: bytes) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _send(self, connection: socket.socket, payload: Dict[str, Any]) -> bool:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                connection.sendall(line.encode("utf-8"))
        except OSError:
            return False
        return True

    def _send_line(self, payload: Dict[str, Any]) -> bool:
        with self._lock:
            connection = self._connection
        if connection is None:
            return False
        return self._send(connection, payload)

    def send_audio_start(self, mode: str) -> bool:
        """Tell the plugin to open a Vokie voice session for ``mode``."""

        return self._send_line(
            {"type": "audio_start", "mode": str(mode), "sampleRate": 16000}
        )

    def send_audio(self, pcm: bytes) -> bool:
        """Forward one chunk of 16 kHz mono s16le PCM to the plugin."""

        if not pcm:
            return False
        return self._send_line(
            {"type": "audio", "pcm": base64.b64encode(pcm).decode("ascii")}
        )

    def send_audio_stop(self) -> bool:
        """Finish the voice session the plugin opened for this press."""

        return self._send_line({"type": "audio_stop"})
