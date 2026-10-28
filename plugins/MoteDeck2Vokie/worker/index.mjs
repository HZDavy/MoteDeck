// MoteDeck —— Vokie 外部设备插件 Worker
//
// 这个 Worker 做两件事：
//   1. 讲完 Vokie Host 的插件协议（握手 + 生命周期 + 配置确认），让 MoteDeck
//      出现在 Vokie 的插件列表里，并提供「连接本软件」所需的配置开关。
//   2. 当用户在插件页点「连接本软件」后，读取 MoteDeck 写在
//      %LOCALAPPDATA%\RemoteMic\RC003\vokie-bridge.json 里的发现信息，
//      用 TCP + 行分隔 JSON 把 Vokie 的语音模式（名称 + 真实快捷键）交给 MoteDeck。
//
// 只使用 Node 内置模块：不依赖 npm install，也不依赖全局 WebSocket。

import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PACKAGE_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const MANIFEST_PATH = path.join(PACKAGE_DIR, 'vokie.plugin.json');

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const BRIDGE_PROTOCOL = 'motedeck-vokie-bridge';
const BRIDGE_RETRY_MS = 2500;
const BRIDGE_PING_MS = 15000;
const BRIDGE_IDLE_MS = 45000;
const MAX_FRAME_BYTES = 8 * 1024 * 1024;

const LOG_PATH = path.join(
  process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
  'RemoteMic',
  'RC003',
  'logs',
  'vokie-plugin.log'
);

// 上一次会话的 requestId：Vokie 侧只要没收到配对的 session_stop，就会一直占着
// activeSession，之后所有会话都被 busy 拒绝。Worker 重启后拿这条记录补一个
// session_stop，把卡住的会话清掉。
const SESSION_RECORD_PATH = path.join(
  process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
  'RemoteMic',
  'RC003',
  'vokie-session.json'
);

function rememberSession(requestId) {
  try {
    fs.mkdirSync(path.dirname(SESSION_RECORD_PATH), { recursive: true });
    fs.writeFileSync(SESSION_RECORD_PATH, JSON.stringify({ requestId }), 'utf8');
  } catch {
    // 记录失败只影响下次启动的自愈。
  }
}

function forgetSession() {
  try {
    fs.rmSync(SESSION_RECORD_PATH, { force: true });
  } catch {
    // 删不掉也无妨：下次会话会覆盖这份记录。
  }
}

function readRememberedSession() {
  try {
    const value = JSON.parse(fs.readFileSync(SESSION_RECORD_PATH, 'utf8'));
    const requestId = value && typeof value.requestId === 'string'
      ? value.requestId.trim()
      : '';
    return requestId;
  } catch {
    return '';
  }
}

function log(...args) {
  // Worker 的 stdout/stderr 只是日志，不参与协议。
  console.error('[motedeck]', ...args);
  try {
    const text = args
      .map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
      .join(' ');
    fs.appendFileSync(LOG_PATH, `${new Date().toISOString()} ${text}\n`);
  } catch {
    // 落盘失败不影响协议。
  }
}

function readManifest() {
  const raw = fs.readFileSync(MANIFEST_PATH, 'utf8');
  const manifest = JSON.parse(raw);
  if (!manifest || typeof manifest.id !== 'string') {
    throw new Error('vokie.plugin.json 缺少 id');
  }
  return manifest;
}

const MANIFEST = readManifest();

// Host 会把注册时保存的 manifest 与 plugin_hello 里的副本逐字段比对，
// 因此这里只回显协议字段，且路径保持包内相对路径。
const MANIFEST_WIRE = {
  id: MANIFEST.id,
  name: MANIFEST.name,
  version: MANIFEST.version,
  apiVersion: MANIFEST.apiVersion,
  platforms: MANIFEST.platforms,
  transports: MANIFEST.transports,
  capabilities: MANIFEST.capabilities,
  permissions: MANIFEST.permissions,
  icon: MANIFEST.icon,
  ui: { entrypoint: MANIFEST.ui && MANIFEST.ui.entrypoint },
};

// ---------------------------------------------------------------------------
// 最小 WebSocket 客户端（RFC 6455，仅文本帧 + ping/pong/close）
// ---------------------------------------------------------------------------

class WebSocketClient {
  constructor(rawUrl) {
    this.url = new URL(rawUrl);
    this.socket = null;
    this.buffer = Buffer.alloc(0);
    this.opened = false;
    this.closed = false;
    this.fragments = [];
    this.fragmentOpcode = 0;
    this.onText = () => {};
    this.onOpen = () => {};
    this.onClose = () => {};
  }

  connect() {
    return new Promise((resolve, reject) => {
      const key = crypto.randomBytes(16).toString('base64');
      const expected = crypto
        .createHash('sha1')
        .update(key + WS_GUID)
        .digest('base64');
      const port = Number(this.url.port || (this.url.protocol === 'wss:' ? 443 : 80));
      const socket = net.connect({ host: this.url.hostname, port });
      this.socket = socket;
      socket.setNoDelay(true);
      socket.setKeepAlive(true, 15000);

      let settled = false;
      const fail = (error) => {
        if (settled) return;
        settled = true;
        reject(error);
      };

      const timer = setTimeout(() => {
        fail(new Error('websocket handshake timeout'));
        socket.destroy();
      }, 10000);

      socket.on('error', (error) => {
        fail(error);
        this._teardown();
      });
      socket.on('close', () => {
        clearTimeout(timer);
        fail(new Error('websocket closed before handshake'));
        this._teardown();
      });
      socket.on('connect', () => {
        if (this.url.protocol === 'wss:') {
          fail(new Error('wss is not supported by this worker'));
          socket.destroy();
          return;
        }
        const target = `${this.url.pathname || '/'}${this.url.search || ''}`;
        socket.write(
          `GET ${target} HTTP/1.1\r\n` +
            `Host: ${this.url.host}\r\n` +
            'Upgrade: websocket\r\n' +
            'Connection: Upgrade\r\n' +
            `Sec-WebSocket-Key: ${key}\r\n` +
            'Sec-WebSocket-Version: 13\r\n\r\n'
        );
      });

      socket.on('data', (chunk) => {
        this.buffer = Buffer.concat([this.buffer, chunk]);
        if (!this.opened) {
          const headerEnd = this.buffer.indexOf('\r\n\r\n');
          if (headerEnd < 0) return;
          const head = this.buffer.subarray(0, headerEnd).toString('latin1');
          this.buffer = this.buffer.subarray(headerEnd + 4);
          const lines = head.split('\r\n');
          if (!/^HTTP\/1\.[01]\s+101/.test(lines[0] || '')) {
            clearTimeout(timer);
            fail(new Error(`websocket handshake rejected: ${lines[0] || ''}`));
            socket.destroy();
            return;
          }
          const headers = {};
          for (const line of lines.slice(1)) {
            const index = line.indexOf(':');
            if (index > 0) {
              headers[line.slice(0, index).trim().toLowerCase()] = line
                .slice(index + 1)
                .trim();
            }
          }
          if (headers['sec-websocket-accept'] !== expected) {
            clearTimeout(timer);
            fail(new Error('websocket accept mismatch'));
            socket.destroy();
            return;
          }
          clearTimeout(timer);
          this.opened = true;
          settled = true;
          resolve();
          this.onOpen();
          if (this.buffer.length) this._consume();
          return;
        }
        this._consume();
      });
    });
  }

  sendJson(payload) {
    this._sendFrame(0x1, Buffer.from(JSON.stringify(payload), 'utf8'));
  }

  sendBinary(payload) {
    this._sendFrame(0x2, payload);
  }

  ping() {
    this._sendFrame(0x9, Buffer.alloc(0));
  }

  close(code = 1000, reason = '') {
    if (this.closed) return;
    const payload = Buffer.alloc(2 + Buffer.byteLength(reason));
    payload.writeUInt16BE(code, 0);
    payload.write(reason, 2, 'utf8');
    this._sendFrame(0x8, payload);
    this._teardown();
    try {
      if (this.socket) this.socket.end();
    } catch {
      /* 已经断开 */
    }
  }

  _consume() {
    for (;;) {
      const frame = this._readFrame();
      if (!frame) return;
      if (frame.opcode === 0x8) {
        const payload = Buffer.alloc(2);
        payload.writeUInt16BE(1000, 0);
        this._sendFrame(0x8, payload);
        this._teardown();
        try {
          if (this.socket) this.socket.end();
        } catch {
          /* 已经断开 */
        }
        return;
      }
      if (frame.opcode === 0x9) {
        this._sendFrame(0xa, frame.payload);
        continue;
      }
      if (frame.opcode === 0xa) continue;
      if (frame.opcode === 0x1 || frame.opcode === 0x2) {
        if (frame.fin) {
          this._deliver(frame.opcode, frame.payload);
        } else {
          this.fragmentOpcode = frame.opcode;
          this.fragments = [frame.payload];
        }
        continue;
      }
      if (frame.opcode === 0x0) {
        this.fragments.push(frame.payload);
        if (frame.fin) {
          const payload = Buffer.concat(this.fragments);
          this.fragments = [];
          this._deliver(this.fragmentOpcode, payload);
        }
      }
    }
  }

  _deliver(opcode, payload) {
    if (opcode !== 0x1) return;
    this.onText(payload.toString('utf8'));
  }

  _readFrame() {
    const buffer = this.buffer;
    if (buffer.length < 2) return null;
    const fin = (buffer[0] & 0x80) !== 0;
    const opcode = buffer[0] & 0x0f;
    const masked = (buffer[1] & 0x80) !== 0;
    let length = buffer[1] & 0x7f;
    let offset = 2;
    if (length === 126) {
      if (buffer.length < 4) return null;
      length = buffer.readUInt16BE(2);
      offset = 4;
    } else if (length === 127) {
      if (buffer.length < 10) return null;
      const big = buffer.readBigUInt64BE(2);
      if (big > BigInt(MAX_FRAME_BYTES)) {
        log('frame too large, closing');
        this.close(1009, 'frame_too_large');
        return null;
      }
      length = Number(big);
      offset = 10;
    }
    let mask = null;
    if (masked) {
      if (buffer.length < offset + 4) return null;
      mask = buffer.subarray(offset, offset + 4);
      offset += 4;
    }
    if (buffer.length < offset + length) return null;
    // 复制一份，避免后续 buffer 重分配时引用悬空。
    const payload = Buffer.from(buffer.subarray(offset, offset + length));
    this.buffer = buffer.subarray(offset + length);
    if (mask) {
      for (let i = 0; i < payload.length; i += 1) {
        payload[i] ^= mask[i % 4];
      }
    }
    return { fin, opcode, payload };
  }

  _sendFrame(opcode, payload) {
    if (this.closed || !this.socket) return;
    const mask = crypto.randomBytes(4);
    const length = payload.length;
    let header;
    if (length < 126) {
      header = Buffer.alloc(2);
      header[1] = 0x80 | length;
    } else if (length < 65536) {
      header = Buffer.alloc(4);
      header[1] = 0x80 | 126;
      header.writeUInt16BE(length, 2);
    } else {
      header = Buffer.alloc(10);
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(length), 2);
    }
    header[0] = 0x80 | opcode;
    const body = Buffer.alloc(length);
    for (let i = 0; i < length; i += 1) {
      body[i] = payload[i] ^ mask[i % 4];
    }
    try {
      this.socket.write(Buffer.concat([header, mask, body]));
    } catch (error) {
      log('websocket write failed:', error.message);
    }
  }

  _teardown() {
    if (this.closed) return;
    this.closed = true;
    try {
      if (this.socket) this.socket.destroy();
    } catch {
      /* 已经断开 */
    }
    this.onClose();
  }
}

// ---------------------------------------------------------------------------
// Vokie 语音模式：优先读 Vokie 自己的快捷键配置，读不到就用默认值
// ---------------------------------------------------------------------------

const MODIFIER_TOKENS = new Set([
  'ctrl',
  'shift',
  'alt',
  'win',
  'lctrl',
  'rctrl',
  'lshift',
  'rshift',
  'lalt',
  'ralt',
  'lwin',
  'rwin',
]);

const KEY_NAME_TOKENS = {
  rightalt: 'ralt',
  leftalt: 'lalt',
  rightctrl: 'rctrl',
  leftctrl: 'lctrl',
  rightshift: 'rshift',
  leftshift: 'lshift',
  rightmeta: 'rwin',
  leftmeta: 'lwin',
  rightwin: 'rwin',
  leftwin: 'lwin',
  space: 'space',
  spacebar: 'space',
  enter: 'enter',
  return: 'enter',
  tab: 'tab',
  escape: 'esc',
  esc: 'esc',
  backspace: 'backspace',
  delete: 'delete',
  insert: 'insert',
  home: 'home',
  end: 'end',
  pageup: 'pageup',
  pagedown: 'pagedown',
  up: 'up',
  down: 'down',
  left: 'left',
  right: 'right',
  arrowup: 'up',
  arrowdown: 'down',
  arrowleft: 'left',
  arrowright: 'right',
  ' ': 'space',
};

const MODE_DEFINITIONS = [
  { id: 'vokie-ptt', name: '语音输入', configKey: 'pttConfig', fallback: 'ralt' },
  // 免提输入（toggleConfig）是“按一下开、再按一下关”，遥控器只能按住说话，
  // 所以这里不列出来，免得给出一个按住就断的键。
  { id: 'vokie-translation', name: '翻译输入', configKey: 'translationConfig', fallback: 'ralt+t' },
  // 同声传译（实时翻译）按用户要求不列出。
  { id: 'vokie-command', name: '语音指令', configKey: 'commandConfig', fallback: 'ralt+space' },
  // 闪记：读 Vokie 的 quickNoteConfig。用户不需要这条键时，在语音键列表里
  // 自己关掉或删掉即可。
  { id: 'vokie-quick-note', name: '闪记', configKey: 'quickNoteConfig', fallback: 'ralt+n' },
];

function vokieSettingsPath() {
  const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
  return path.join(appData, 'vokie', 'app-settings.json');
}

function toToken(raw) {
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim();
  if (!trimmed) return '';
  const lower = trimmed.toLowerCase();
  if (KEY_NAME_TOKENS[lower]) return KEY_NAME_TOKENS[lower];
  if (/^[a-z]$/.test(lower)) return lower;
  if (/^[0-9]$/.test(lower)) return lower;
  if (/^f([1-9]|1[0-9]|2[0-4])$/.test(lower)) return lower;
  return '';
}

function hotkeyFromVokieConfig(config, fallback) {
  if (!config || !Array.isArray(config.keys) || config.keys.length === 0) {
    return fallback;
  }
  const modifiers = [];
  let key = '';
  for (const entry of config.keys) {
    const token = toToken(entry);
    if (!token) continue;
    if (MODIFIER_TOKENS.has(token)) {
      if (!modifiers.includes(token)) modifiers.push(token);
    } else {
      key = token;
    }
  }
  if (!key) return modifiers.length === 1 ? modifiers[0] : fallback;
  return [...modifiers, key].join('+');
}

function buildModes() {
  let settings = null;
  try {
    settings = JSON.parse(fs.readFileSync(vokieSettingsPath(), 'utf8'));
  } catch {
    settings = null;
  }
  const modes = [];
  const seen = new Set();
  for (const definition of MODE_DEFINITIONS) {
    const hotkey = hotkeyFromVokieConfig(
      settings ? settings[definition.configKey] : null,
      definition.fallback
    );
    if (!hotkey || seen.has(hotkey)) continue;
    seen.add(hotkey);
    modes.push({ id: definition.id, name: definition.name, hotkey });
  }
  return modes;
}

// ---------------------------------------------------------------------------
// Vokie 语音会话：把 MoteDeck 推来的 PCM 直接喂给 Vokie
//
// 不再靠模拟热键让 Vokie 开电脑麦克风，而是按 Vokie 外部设备协议主动开
// session，再用 WebSocket 二进制帧推送 16k 单声道 PCM。
// ---------------------------------------------------------------------------

const AUDIO_SAMPLE_RATE = 16000;
const AUDIO_CHANNELS = 1;
const AUDIO_FORMAT = 'pcm_s16le';
const MAX_PENDING_AUDIO_FRAMES = 200;
const SESSION_ACCEPT_TIMEOUT_MS = 6000;

let session = null;

function readTranslationTargetLanguage() {
  try {
    const settings = JSON.parse(fs.readFileSync(vokieSettingsPath(), 'utf8'));
    const value = settings ? settings.translationTargetLanguage : '';
    return typeof value === 'string' ? value.trim() : '';
  } catch {
    return '';
  }
}

function resolveSessionMode(name) {
  const label = typeof name === 'string' ? name : '';
  // 闪记：Vokie 的 quickNote 录音管线（recording + outputRoute=quickNote）。
  if (label.includes('闪记')) {
    return { mode: 'recording', extra: { outputRoute: 'quickNote' } };
  }
  if (label.includes('翻译')) {
    const target = readTranslationTargetLanguage();
    // 翻译会话必须带非空目标语言，拿不到就退回普通语音输入。
    if (target) return { mode: 'translation', extra: { targetLanguage: target } };
  }
  // 语音指令：Vokie 1.6.6 起插件协议支持 command（需要 manifest 里声明
  // capabilities.command），走它自己的指令会话。
  if (label.includes('指令')) {
    return { mode: 'command', extra: {} };
  }
  return { mode: 'ptt', extra: {} };
}

function clearSessionTimers(entry) {
  if (entry.acceptTimer) {
    clearTimeout(entry.acceptTimer);
    entry.acceptTimer = null;
  }
}

// 只要发过 session_start，就必须配对一条 session_stop。
// 否则 Vokie 侧 activeSession 会一直占着，之后所有 session_start 都被 busy 静默拒绝。
function sendSessionStop(entry, reason) {
  if (entry.stopSent) return;
  entry.stopSent = true;
  send({
    type: 'session_stop',
    requestId: entry.requestId,
    timestampMs: Date.now(),
    reason,
  });
}

function dropSession(entry, reason) {
  clearSessionTimers(entry);
  if (session === entry) {
    session = null;
    forgetSession();
  }
  if (reason) sendSessionStop(entry, reason);
}

function finishVoiceSession(entry) {
  clearSessionTimers(entry);
  if (session === entry) {
    session = null;
    forgetSession();
  }
  sendSessionStop(entry, 'user');
}

function abortVoiceSession(reason = 'device') {
  const entry = session;
  if (!entry) return;
  dropSession(entry, reason);
}

function sendAudioFrame(entry, pcm) {
  if (!client || !client.opened) return;
  const header = Buffer.from(
    JSON.stringify({
      type: 'audio',
      requestId: entry.requestId,
      sequence: entry.sequence,
      sampleRate: AUDIO_SAMPLE_RATE,
      channels: AUDIO_CHANNELS,
      format: AUDIO_FORMAT,
    }),
    'utf8'
  );
  const head = Buffer.alloc(4);
  head.writeUInt32BE(header.length, 0);
  client.sendBinary(Buffer.concat([head, header, pcm]));
  entry.sequence += 1;
}

function startVoiceSession(name, forcedMode) {
  if (!started || stopped || shuttingDown) {
    log('audio_start ignored: plugin not ready');
    return;
  }
  abortVoiceSession('device');
  const resolved = forcedMode
    ? { mode: forcedMode, extra: {} }
    : resolveSessionMode(name);
  log(`audio_start received: ${name} host=${Boolean(client && client.opened)} bridge=${LINK.status}`);
  const entry = {
    requestId: crypto.randomUUID(),
    mode: resolved.mode,
    label: name,
    sequence: 0,
    accepted: false,
    stopping: false,
    stopSent: false,
    pending: [],
    acceptTimer: null,
  };
  session = entry;
  rememberSession(entry.requestId);
  entry.acceptTimer = setTimeout(() => {
    if (session !== entry) return;
    log('vokie session was not accepted in time');
    dropSession(entry, 'error');
  }, SESSION_ACCEPT_TIMEOUT_MS);
  log(`session_start ${entry.mode} ${entry.requestId}`);
  send({
    type: 'session_start',
    requestId: entry.requestId,
    timestampMs: Date.now(),
    mode: entry.mode,
    options: {
      ...resolved.extra,
      audioSource: {
        type: 'stream',
        format: AUDIO_FORMAT,
        sampleRate: AUDIO_SAMPLE_RATE,
        channels: AUDIO_CHANNELS,
      },
    },
  });
}

function pushVoiceAudio(base64) {
  const entry = session;
  if (!entry || entry.stopping) return;
  if (typeof base64 !== 'string' || !base64) return;
  const pcm = Buffer.from(base64, 'base64');
  if (!pcm.length || pcm.length % 2 !== 0) return;
  if (!entry.accepted) {
    entry.pending.push(pcm);
    if (entry.pending.length > MAX_PENDING_AUDIO_FRAMES) entry.pending.shift();
    return;
  }
  sendAudioFrame(entry, pcm);
}

function stopVoiceSession() {
  const entry = session;
  if (!entry) return;
  entry.stopping = true;
  // 还没被 Vokie 接受时先留着，等 session_accepted 再冲掉缓存并收尾。
  if (entry.accepted) finishVoiceSession(entry);
}

function handleSessionResponse(message) {
  const entry = session;
  if (!entry || message.requestId !== entry.requestId) return;
  if (message.type === 'session_rejected') {
    log(`session_rejected: ${message.reason || 'unknown'}`);
    // 闪记 / 语音指令会话被拒（老版本 Vokie 没开放）时，退回普通语音输入，
    // 至少让这次按键能正常说话，而不是没反应。
    const retryAsPtt = (entry.mode === 'recording' || entry.mode === 'command')
      && message.reason !== 'busy';
    const label = entry.label;
    dropSession(entry);
    if (retryAsPtt) {
      log('quick note session rejected, retrying as ptt');
      startVoiceSession(label, 'ptt');
    }
    return;
  }
  entry.accepted = true;
  clearSessionTimers(entry);
  // Vokie 这时才真正弹出自己的输入框；MoteDeck 靠这条消息收掉模式气泡，
  // 所以不能提前在 audio_start 时就告诉它。
  LINK.notifySessionAccepted(entry.mode);
  const pending = entry.pending.splice(0);
  for (const pcm of pending) sendAudioFrame(entry, pcm);
  if (entry.stopping) finishVoiceSession(entry);
}

// ---------------------------------------------------------------------------
// MoteDeck 回环连接：发现文件 → TCP 行分隔 JSON
// ---------------------------------------------------------------------------

function discoveryPath() {
  const localAppData =
    process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  return path.join(localAppData, 'RemoteMic', 'RC003', 'vokie-bridge.json');
}

function readDiscovery() {
  try {
    const info = JSON.parse(fs.readFileSync(discoveryPath(), 'utf8'));
    if (!info || info.protocol !== BRIDGE_PROTOCOL) return null;
    const port = Number(info.port);
    if (!Number.isInteger(port) || port <= 0 || port > 65535) return null;
    if (typeof info.token !== 'string' || !info.token) return null;
    const host = typeof info.host === 'string' && info.host ? info.host : '127.0.0.1';
    return { host, port, token: info.token, app: info.app, appVersion: info.appVersion };
  } catch {
    return null;
  }
}

class MoteDeckLink {
  constructor(onChange) {
    this.onChange = onChange;
    this.running = false;
    this.socket = null;
    this.buffer = '';
    this.retryTimer = null;
    this.pingTimer = null;
    this.lastSeen = 0;
    this.status = 'idle';
    this.message = '';
    this.app = '';
    this.appVersion = '';
    this.port = 0;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this._setStatus('waiting', '等待 MoteDeck 开启插件连接');
    this._attempt();
  }

  stop() {
    this.running = false;
    abortVoiceSession('device');
    this._clearTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      this._sendOn(socket, { type: 'bye' });
      try {
        socket.end();
      } catch {
        /* 已经断开 */
      }
      try {
        socket.destroy();
      } catch {
        /* 已经断开 */
      }
    }
    this._setStatus('idle', '');
  }

  _clearTimers() {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  _scheduleRetry() {
    if (!this.running || this.retryTimer) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this._attempt();
    }, BRIDGE_RETRY_MS);
  }

  _attempt() {
    if (!this.running) return;
    const discovery = readDiscovery();
    if (!discovery) {
      this._setStatus('waiting', '等待 MoteDeck 开启插件连接');
      this._scheduleRetry();
      return;
    }
    this._setStatus('connecting', '正在连接 MoteDeck');
    this.port = discovery.port;
    const socket = net.connect({ host: discovery.host, port: discovery.port });
    this.socket = socket;
    this.buffer = '';
    this.lastSeen = Date.now();
    socket.setNoDelay(true);
    socket.setEncoding('utf8');

    socket.on('connect', () => {
      this._send({
        type: 'hello',
        token: discovery.token,
        plugin: { name: MANIFEST.name, version: MANIFEST.version },
        modes: buildModes(),
      });
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => {
        if (!this.socket) return;
        if (Date.now() - this.lastSeen > BRIDGE_IDLE_MS) {
          log('motedeck bridge idle, reconnecting');
          this._dropSocket();
          this._setStatus('waiting', '与 MoteDeck 的连接已断开');
          this._scheduleRetry();
          return;
        }
        this._send({ type: 'ping' });
      }, BRIDGE_PING_MS);
    });

    socket.on('data', (chunk) => {
      this.lastSeen = Date.now();
      this.buffer += chunk;
      for (;;) {
        const index = this.buffer.indexOf('\n');
        if (index < 0) break;
        const line = this.buffer.slice(0, index);
        this.buffer = this.buffer.slice(index + 1);
        if (!line.trim()) continue;
        this._handleLine(line);
      }
      // 防御异常对端：单行过长时直接重来。
      if (this.buffer.length > 256 * 1024) this.buffer = '';
    });

    socket.on('error', (error) => {
      log('motedeck bridge error:', error.message);
    });

    socket.on('close', () => {
      if (this.socket !== socket) return;
      this.socket = null;
      abortVoiceSession('device');
      if (!this.running) return;
      this._setStatus('waiting', 'MoteDeck 未开启插件连接');
      this._scheduleRetry();
    });
  }

  _dropSocket() {
    abortVoiceSession('device');
    const socket = this.socket;
    this.socket = null;
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (!socket) return;
    try {
      socket.destroy();
    } catch {
      /* 已经断开 */
    }
  }

  _handleLine(line) {
    let payload;
    try {
      payload = JSON.parse(line);
    } catch {
      return;
    }
    if (!payload || typeof payload !== 'object') return;
    if (payload.type === 'hello_ok') {
      this.app = typeof payload.app === 'string' ? payload.app : 'MoteDeck';
      this.appVersion =
        typeof payload.appVersion === 'string' ? payload.appVersion : '';
      this._setStatus('connected', '');
      return;
    }
    if (payload.type === 'hello_rejected') {
      this.message = 'MoteDeck 拒绝了插件连接';
      this._dropSocket();
      this._setStatus('error', this.message);
      this._scheduleRetry();
      return;
    }
    if (payload.type === 'goodbye') {
      this.message = 'MoteDeck 已断开插件连接';
      this._dropSocket();
      this._setStatus('waiting', this.message);
      this._scheduleRetry();
      return;
    }
    if (payload.type === 'error') {
      log('motedeck bridge reported:', payload.reason || payload.type);
      return;
    }
    if (payload.type === 'audio_start') {
      startVoiceSession(payload.mode);
      return;
    }
    if (payload.type === 'audio') {
      pushVoiceAudio(payload.pcm);
      return;
    }
    if (payload.type === 'audio_stop') {
      stopVoiceSession();
    }
  }

  _send(payload) {
    if (!this.socket) return;
    this._sendOn(this.socket, payload);
  }

  notifySessionAccepted(mode) {
    this._send({ type: 'session_accepted', mode: String(mode || '') });
  }

  _sendOn(socket, payload) {
    try {
      socket.write(`${JSON.stringify(payload)}\n`);
    } catch (error) {
      log('motedeck bridge write failed:', error.message);
    }
  }

  _setStatus(status, message) {
    if (this.status === status && this.message === message) return;
    this.status = status;
    this.message = message;
    this.onChange();
  }
}

// ---------------------------------------------------------------------------
// Vokie Host 生命周期
// ---------------------------------------------------------------------------

const LINK = new MoteDeckLink(() => publishState());

let client = null;
let started = false;
let stopped = false;
let shuttingDown = false;
let connectedToMoteDeck = false; // 用户是否在插件页打开「连接本软件」

function coreState() {
  if (shuttingDown || stopped) return 'stopped';
  if (!started) return 'starting';
  if (connectedToMoteDeck && LINK.status === 'connected') return 'connected';
  return 'ready';
}

function publishState() {
  if (!client || !client.opened) return;
  const isConnected = connectedToMoteDeck && LINK.status === 'connected';
  client.sendJson({
    type: 'state',
    state: coreState(),
    extensions: {
      motedeck: {
        status: LINK.status,
        message: LINK.message,
        app: LINK.app,
        appVersion: LINK.appVersion,
        port: LINK.port,
        connected: isConnected,
        enabled: connectedToMoteDeck,
        modes: buildModes(),
      },
    },
  });
}

function applyConnectionSetting(value) {
  connectedToMoteDeck = value;
  if (!started) {
    // Host 会在 start 之前下发持久化配置，这里只记住用户意图。
    publishState();
    return;
  }
  if (value) {
    LINK.start();
  } else {
    LINK.stop();
  }
  publishState();
}

function parseConnectionConfig(config) {
  if (config === undefined || config === null) return { value: false };
  if (typeof config !== 'object' || Array.isArray(config)) {
    return { error: 'invalid_config' };
  }
  if (!('connect' in config)) return { value: false };
  if (typeof config.connect !== 'boolean') return { error: 'invalid_connect_flag' };
  return { value: config.connect };
}

function send(payload) {
  if (client && client.opened) {
    client.sendJson(payload);
    return;
  }
  log(`host link not open, dropped ${payload && payload.type}`);
}

function handleHostMessage(text) {
  let message;
  try {
    message = JSON.parse(text);
  } catch {
    log('ignoring non-JSON host message');
    return;
  }
  if (!message || typeof message !== 'object') return;

  switch (message.type) {
    case 'handshake_ok': {
      log(`handshake ok: ${message.pluginId} (${message.connectionId})`);
      publishState();
      // 上一次进程没来得及收尾的会话：补一条 session_stop，把 Vokie 侧
      // 占着的 activeSession 清掉，否则之后每次都会被告知 busy。
      const staleRequestId = readRememberedSession();
      if (staleRequestId) {
        send({
          type: 'session_stop',
          requestId: staleRequestId,
          timestampMs: Date.now(),
          reason: 'restart',
        });
        forgetSession();
        log(`sent stale session_stop ${staleRequestId}`);
      }
      break;
    }
    case 'initialize':
      send({ type: 'initialized' });
      break;
    case 'start':
      started = true;
      stopped = false;
      if (connectedToMoteDeck) LINK.start();
      send({ type: 'ready' });
      publishState();
      break;
    case 'stop':
      started = false;
      stopped = true;
      abortVoiceSession('device');
      LINK.stop();
      send({ type: 'stopped' });
      publishState();
      break;
    case 'shutdown':
      shuttingDown = true;
      started = false;
      stopped = true;
      abortVoiceSession('device');
      LINK.stop();
      send({ type: 'destroyed' });
      publishState();
      setTimeout(() => process.exit(0), 40);
      break;
    case 'session_accepted':
    case 'session_rejected':
      handleSessionResponse(message);
      break;
    case 'configuration_changed':
      handleConfiguration(message, true);
      break;
    case 'configure':
      handleConfiguration(message, false);
      break;
    default:
      break;
  }
}

function handleConfiguration(message, requiresRequestId) {
  const requestId = typeof message.requestId === 'string' ? message.requestId : '';
  const parsed = parseConnectionConfig(message.config);
  if (parsed.error) {
    log(`configuration rejected: ${parsed.error}`);
    if (requestId) {
      send({
        type: 'configuration_rejected',
        requestId,
        error: parsed.error,
      });
    } else {
      send({ type: 'error', code: 'lifecycle_failed', message: parsed.error });
    }
    return;
  }
  applyConnectionSetting(parsed.value);
  if (requestId) {
    send({ type: 'configured', requestId });
  } else if (!requiresRequestId) {
    send({ type: 'configured' });
  }
}

async function main() {
  const url = process.env.VOKIE_PLUGIN_WS_URL;
  const token = process.env.VOKIE_PLUGIN_TOKEN;
  if (!url || !token) {
    log('missing VOKIE_PLUGIN_WS_URL / VOKIE_PLUGIN_TOKEN');
    process.exit(1);
  }

  client = new WebSocketClient(url);
  client.onText = handleHostMessage;
  client.onClose = () => {
    log('vokie host socket closed');
    LINK.stop();
    process.exit(0);
  };

  try {
    await client.connect();
  } catch (error) {
    log('cannot reach vokie host:', error.message);
    process.exit(1);
  }

  client.sendJson({
    type: 'plugin_hello',
    token,
    manifest: MANIFEST_WIRE,
  });
  log(`plugin_hello sent for ${MANIFEST.id}`);
}

process.on('uncaughtException', (error) => {
  log('uncaught exception:', error && error.message ? error.message : error);
  LINK.stop();
  process.exit(1);
});
process.on('unhandledRejection', (error) => {
  log('unhandled rejection:', error && error.message ? error.message : error);
});
process.on('SIGINT', () => {
  shuttingDown = true;
  LINK.stop();
  process.exit(0);
});
process.on('SIGTERM', () => {
  shuttingDown = true;
  LINK.stop();
  process.exit(0);
});

main();
