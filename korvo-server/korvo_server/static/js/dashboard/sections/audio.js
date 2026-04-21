(function () {
  const BOARD_OUT_VOL_KEY = 'korvo_board_output_volume';

  let pushCtx = null;
  let pushStream = null;
  let pushNode = null;
  let pushMicWs = null;
  let pushMicOn = false;
  let pushResampleTail = null;
  let pushResamplePos = 0;
  let pushCaptureRate = 16000;
  let pushTargetRate = 16000;
  let pushPcmAccumulator = new Int16Array(0);
  const PUSH_MIC_SEND_SAMPLES = 1600;
  const PUSH_MIC_MAX_QUEUE = 10;
  let pushMicSendQueue = [];
  let pushFilePollTimer = null;
  let boardVolPostTimer = null;
  let fileProgressUserSeeking = false;
  let fileProgressDragActive = false;
  let lastFileDurationSec = null;
  /** Max position seen this session (helps approximate slider when ffprobe duration is missing). */
  let fileStreamPeakPosition = 0;

  const SVG_PLAY = '<svg class="audio-svg-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
  const SVG_PAUSE = '<svg class="audio-svg-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M6 5h4v14H6V5zm8 0h4v14h-4V5z"/></svg>';

  function pushStatusEl() { return document.getElementById('audioPushStatus'); }
  function pushMicBtnEl() { return document.getElementById('audioPushMicBtn'); }
  function pushFileToggleBtnEl() { return document.getElementById('audioFileStreamToggleBtn'); }
  function pushStopBtnEl() { return document.getElementById('audioFileStopBtn'); }
  function pushProgressEl() { return document.getElementById('audioPushProgress'); }
  function fileProgressEl() { return document.getElementById('audioFileProgress'); }
  function fileTimeLabelEl() { return document.getElementById('audioFileTimeLabel'); }

  function setPushStatus(text) {
    const el = pushStatusEl();
    if (el) el.textContent = text;
  }

  function setPushProgress(text) {
    const el = pushProgressEl();
    if (el) el.textContent = text;
  }

  function formatTime(sec) {
    if (!Number.isFinite(sec) || sec < 0) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  /** Coerce JSON number or numeric string from the API (avoids stuck UI if types differ). */
  function parseApiSeconds(v) {
    if (v == null || v === '') return null;
    if (typeof v === 'number') return Number.isFinite(v) ? v : null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function finishFileProgressDrag(ev) {
    if (!fileProgressDragActive) return;
    fileProgressDragActive = false;
    fileProgressUserSeeking = false;
    const progress = fileProgressEl();
    try {
      const pid = ev && typeof ev.pointerId === 'number' ? ev.pointerId : null;
      if (pid != null && progress && progress.releasePointerCapture) {
        progress.releasePointerCapture(pid);
      }
    } catch (_) {}
    void seekFileFromProgress();
  }

  function flashPushCue(el) {
    if (!el) return;
    el.classList.remove('audio-push-action--cue');
    void el.offsetWidth;
    el.classList.add('audio-push-action--cue');
    window.setTimeout(() => {
      el.classList.remove('audio-push-action--cue');
    }, 2400);
  }

  async function fireBoardPushCue() {
    const base = boardBaseUrl();
    if (!base) return;
    try {
      await fetch(`${base}/api/audio/push-cue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
    } catch (_) {}
  }

  function normalizeBoardHost(raw) {
    let v = String(raw || '').trim();
    if (!v) return '';
    v = v.replace(/^https?:\/\//i, '');
    const slash = v.indexOf('/');
    if (slash >= 0) v = v.slice(0, slash);
    return v.trim();
  }

  function boardBaseUrl() {
    let host = '';
    const ipEl = document.getElementById('boardIp');
    if (ipEl && ipEl.value && ipEl.value.trim()) {
      host = ipEl.value.trim();
    } else {
      try {
        host = (localStorage.getItem('korvo_board_ip') || localStorage.getItem('korvo_esp_ip') || '').trim();
      } catch (_) {
        host = '';
      }
    }
    host = normalizeBoardHost(host);
    if (!host) return '';
    return `http://${host}`;
  }

  function getBoardOutputVolumePercent() {
    const el = document.getElementById('audioBoardVolume');
    let p = el ? parseInt(el.value, 10) : NaN;
    if (!Number.isFinite(p)) {
      try {
        p = parseInt(localStorage.getItem(BOARD_OUT_VOL_KEY) || '100', 10);
      } catch (_) {
        p = 100;
      }
    }
    if (!Number.isFinite(p)) p = 100;
    if (p < 0) p = 0;
    if (p > 100) p = 100;
    return p;
  }

  function getBoardOutputVolumeLinear() {
    const x = getBoardOutputVolumePercent() / 100;
    if (!Number.isFinite(x)) return 1;
    return Math.min(1, Math.max(0, x));
  }

  async function syncBoardOutputVolumeFromDevice() {
    const base = boardBaseUrl();
    const el = document.getElementById('audioBoardVolume');
    const lab = document.getElementById('audioBoardVolumeValue');
    if (!base || !el) return;
    try {
      let hasSaved = false;
      try {
        hasSaved = localStorage.getItem(BOARD_OUT_VOL_KEY) != null;
      } catch (_) {}
      if (hasSaved) return;
      const res = await fetch(`${base}/api/audio/output-volume`, { method: 'GET', cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      const v = parseInt(data.volume, 10);
      if (!Number.isFinite(v) || v < 0 || v > 100) return;
      el.value = String(v);
      if (lab) lab.textContent = String(v);
      try {
        localStorage.setItem(BOARD_OUT_VOL_KEY, String(v));
      } catch (_) {}
    } catch (_) {}
  }

  function schedulePostBoardVolumeToDevice() {
    const base = boardBaseUrl();
    if (!base) return;
    if (boardVolPostTimer) window.clearTimeout(boardVolPostTimer);
    boardVolPostTimer = window.setTimeout(async () => {
      boardVolPostTimer = null;
      const pct = getBoardOutputVolumePercent();
      try {
        await fetch(`${base}/api/audio/output-volume`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ volume: pct }),
        });
      } catch (_) {}
    }, 280);
  }

  function initBoardVolumeControl() {
    const el = document.getElementById('audioBoardVolume');
    const lab = document.getElementById('audioBoardVolumeValue');
    if (!el) return;
    let saved = 100;
    try {
      saved = parseInt(localStorage.getItem(BOARD_OUT_VOL_KEY) || '100', 10);
    } catch (_) {
      saved = 100;
    }
    if (!Number.isFinite(saved) || saved < 0 || saved > 100) saved = 100;
    el.value = String(saved);
    if (lab) lab.textContent = String(saved);
    const sync = () => {
      let v = parseInt(el.value, 10);
      if (!Number.isFinite(v)) v = 100;
      if (v < 0) v = 0;
      if (v > 100) v = 100;
      el.setAttribute('aria-valuenow', String(v));
      if (lab) lab.textContent = String(v);
      try {
        localStorage.setItem(BOARD_OUT_VOL_KEY, String(v));
      } catch (_) {}
      schedulePostBoardVolumeToDevice();
      if (typeof window.__korvoApplyLiveMicGain === 'function') {
        window.__korvoApplyLiveMicGain();
      }
    };
    el.addEventListener('input', sync);
    el.addEventListener('change', sync);
    sync();
    syncBoardOutputVolumeFromDevice();
  }

  function concatInt16(a, b) {
    if (!a || !a.length) return b;
    if (!b || !b.length) return a;
    const out = new Int16Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function downsampleTo16k(inputFloat32) {
    if (!inputFloat32 || !inputFloat32.length) return new Int16Array(0);
    const inRate = Math.max(1, Number(pushCaptureRate) || 16000);
    const ratio = inRate / pushTargetRate;
    const source = pushResampleTail ? (() => {
      const merged = new Float32Array(pushResampleTail.length + inputFloat32.length);
      merged.set(pushResampleTail, 0);
      merged.set(inputFloat32, pushResampleTail.length);
      return merged;
    })() : inputFloat32;
    const est = Math.max(0, Math.floor((source.length - pushResamplePos) / ratio));
    const out = new Int16Array(est);
    let o = 0;
    while ((pushResamplePos + ratio) <= source.length) {
      const idx = Math.floor(pushResamplePos);
      const frac = pushResamplePos - idx;
      const s0 = source[idx] || 0;
      const s1 = source[idx + 1] || s0;
      let s = s0 + (s1 - s0) * frac;
      if (s > 1) s = 1;
      if (s < -1) s = -1;
      out[o++] = s < 0 ? (s * 32768) : (s * 32767);
      pushResamplePos += ratio;
    }
    const keepFrom = Math.max(0, Math.floor(pushResamplePos) - 1);
    pushResampleTail = source.subarray(keepFrom);
    pushResamplePos = pushResamplePos - keepFrom;
    return o === out.length ? out : out.subarray(0, o);
  }

  function micWsUrl(base) {
    try {
      const u = new URL(base);
      const wsProto = u.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${wsProto}//${location.host}/ws/audio/push-mic?board_url=${encodeURIComponent(base)}`;
    } catch (_) {
      const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${wsProto}//${location.host}/ws/audio/push-mic?board_url=${encodeURIComponent(base)}`;
    }
  }

  function flushMicWsQueue() {
    if (!pushMicWs || pushMicWs.readyState !== WebSocket.OPEN) return;
    while (pushMicSendQueue.length > 0) {
      if (pushMicWs.bufferedAmount > 200000) break;
      const chunk = pushMicSendQueue.shift();
      try {
        pushMicWs.send(chunk);
      } catch (_) {
        break;
      }
    }
  }

  function scaleInt16PcmWithGain(int16Arr, gainLinear) {
    if (!int16Arr || !int16Arr.length) return int16Arr;
    const g = Number(gainLinear);
    if (!Number.isFinite(g) || g >= 0.999) {
      return int16Arr;
    }
    const gg = Math.min(1, Math.max(0, g));
    const out = new Int16Array(int16Arr.length);
    for (let i = 0; i < int16Arr.length; i++) {
      let v = Math.round(int16Arr[i] * gg);
      if (v > 32767) v = 32767;
      if (v < -32768) v = -32768;
      out[i] = v;
    }
    return out;
  }

  function queuePcmChunk(int16Arr) {
    if (!int16Arr || !int16Arr.length) return;
    const scaled = scaleInt16PcmWithGain(int16Arr, getBoardOutputVolumeLinear());
    const chunk = new Uint8Array(scaled.buffer.slice(scaled.byteOffset, scaled.byteOffset + scaled.byteLength));
    if (pushMicSendQueue.length >= PUSH_MIC_MAX_QUEUE) {
      pushMicSendQueue.shift();
    }
    pushMicSendQueue.push(chunk);
    flushMicWsQueue();
  }

  function canUseMicCapture() {
    const host = (location.hostname || '').toLowerCase();
    if (window.isSecureContext) return true;
    return host === 'localhost' || host === '127.0.0.1' || host === '::1';
  }

  async function requestUserMicStream() {
    if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function') {
      return navigator.mediaDevices.getUserMedia({ audio: true });
    }
    const legacy = navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia;
    if (typeof legacy === 'function') {
      return new Promise((resolve, reject) => {
        legacy.call(navigator, { audio: true }, resolve, reject);
      });
    }
    throw new Error('Browser has no getUserMedia support');
  }

  function stopPushMicInternal() {
    if (!pushMicOn) return;
    pushMicOn = false;
    try { if (pushNode) pushNode.disconnect(); } catch (_) {}
    try { if (pushStream) pushStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    try { if (pushCtx) pushCtx.close(); } catch (_) {}
    try { if (pushMicWs) pushMicWs.close(); } catch (_) {}
    pushNode = null; pushStream = null; pushCtx = null;
    pushMicWs = null;
    pushMicSendQueue = [];
    pushResampleTail = null;
    pushResamplePos = 0;
    pushPcmAccumulator = new Int16Array(0);
    const btn = pushMicBtnEl();
    if (btn) btn.textContent = '🎙️ Start push mic';
    setMicPushUiDisabled(false);
  }

  async function stopFileStreamServerOnly() {
    try {
      await fetch('/api/audio/push-file/stop', { method: 'POST' });
    } catch (_) {}
    if (pushFilePollTimer) {
      window.clearInterval(pushFilePollTimer);
      pushFilePollTimer = null;
    }
    setFileStreamingUi(false);
    resetFilePlaybackUi();
  }

  /** While mic is live, file stream cannot start. Re-enabling must always clear disabled (ignore stale data-stream-state). */
  function setMicPushUiDisabled(disabled) {
    const t = pushFileToggleBtnEl();
    if (!t) return;
    if (!disabled) {
      t.disabled = false;
      return;
    }
    if ((t.dataset.streamState || 'idle') === 'idle') t.disabled = true;
  }

  function setFileToggleIdle() {
    const btn = pushFileToggleBtnEl();
    if (!btn) return;
    btn.dataset.streamState = 'idle';
    btn.title = 'Start streaming selected file';
    btn.innerHTML = `<span class="audio-btn-ico">${SVG_PLAY}</span><span class="audio-btn-text">Start</span>`;
  }

  function updateFileToggleFromStatus(data) {
    const btn = pushFileToggleBtnEl();
    if (!btn) return;
    const active = !!data.active;
    const paused = !!data.paused;
    if (!active) {
      setFileToggleIdle();
      btn.disabled = !!pushMicOn;
      return;
    }
    btn.disabled = false;
    if (paused) {
      btn.dataset.streamState = 'paused';
      btn.title = 'Resume playback';
      btn.innerHTML = `<span class="audio-btn-ico">${SVG_PLAY}</span><span class="audio-btn-text">Play</span>`;
    } else {
      btn.dataset.streamState = 'playing';
      btn.title = 'Pause playback';
      btn.innerHTML = `<span class="audio-btn-ico">${SVG_PAUSE}</span><span class="audio-btn-text">Pause</span>`;
    }
  }

  function setFileStreamingUi(active) {
    const toggleBtn = pushFileToggleBtnEl();
    const stopBtn = pushStopBtnEl();
    const fileInput = document.getElementById('audioPushFile');
    const progress = fileProgressEl();
    const mic = pushMicBtnEl();
    if (toggleBtn) toggleBtn.disabled = !!pushMicOn && !active;
    if (fileInput) fileInput.disabled = !!active;
    if (mic) mic.disabled = !!active;
    if (stopBtn) stopBtn.disabled = !active;
    if (progress) {
      const dur = lastFileDurationSec;
      progress.disabled = !active || !(Number.isFinite(dur) && dur > 0);
    }
  }

  function resetFilePlaybackUi() {
    lastFileDurationSec = null;
    fileStreamPeakPosition = 0;
    const progress = fileProgressEl();
    const lab = fileTimeLabelEl();
    if (progress) {
      progress.max = 1000;
      progress.value = 0;
      progress.disabled = true;
    }
    if (lab) lab.textContent = '0:00 / —';
    const btn = pushFileToggleBtnEl();
    if (btn) {
      setFileToggleIdle();
      btn.disabled = !!pushMicOn;
    }
  }

  function updateFilePlaybackControls(data) {
    const active = !!data.active;
    const durRaw = parseApiSeconds(data.duration_sec);
    lastFileDurationSec = durRaw != null && durRaw > 0 ? durRaw : null;
    const pos = parseApiSeconds(data.position_sec) ?? 0;
    fileStreamPeakPosition = Math.max(fileStreamPeakPosition, pos);
    const displayDur =
      lastFileDurationSec != null && lastFileDurationSec > 0
        ? lastFileDurationSec
        : Math.max(fileStreamPeakPosition * 1.02, pos + 0.01, 1);
    updateFileToggleFromStatus(data);
    const progress = fileProgressEl();
    const lab = fileTimeLabelEl();
    if (lab) {
      const dStr = lastFileDurationSec != null ? formatTime(lastFileDurationSec) : '—';
      lab.textContent = `${formatTime(pos)} / ${dStr}`;
    }
    if (progress && active && !fileProgressUserSeeking) {
      progress.max = 1000;
      if (lastFileDurationSec != null && lastFileDurationSec > 0) {
        progress.disabled = false;
        const ratio = pos / lastFileDurationSec;
        progress.value = Math.min(1000, Math.max(0, Math.round(ratio * 1000)));
      } else {
        progress.disabled = true;
        const ratio = pos / displayDur;
        progress.value = Math.min(1000, Math.max(0, Math.round(ratio * 1000)));
      }
    }
  }

  async function togglePushMic() {
    const btn = pushMicBtnEl();
    if (pushMicOn) {
      flashPushCue(btn);
      void fireBoardPushCue();
      stopPushMicInternal();
      setPushStatus('Mic push stopped.');
      return;
    }
    const base = boardBaseUrl();
    if (!base) {
      setPushStatus('Set board host first.');
      return;
    }
    if (!canUseMicCapture()) {
      setPushStatus('Mic capture blocked: open dashboard on https:// or localhost (current page is not a secure context).');
      return;
    }
    await stopFileStreamServerOnly();
    flashPushCue(btn);
    void fireBoardPushCue();
    try {
      pushStream = await requestUserMicStream();
      pushCtx = new (window.AudioContext || window.webkitAudioContext)();
      pushCaptureRate = pushCtx.sampleRate || 48000;
      pushTargetRate = 16000;
      try {
        if (pushCtx.state === 'suspended' && typeof pushCtx.resume === 'function') {
          await pushCtx.resume();
        }
      } catch (_) {}
      pushMicWs = new WebSocket(micWsUrl(base));
      pushMicWs.binaryType = 'arraybuffer';
      pushMicWs.onopen = () => {
        setPushStatus(`Mic push active: opening stream... ${Math.round(pushCaptureRate)}Hz -> 16000Hz.`);
        try {
          if (pushCtx && pushCtx.state === 'suspended' && typeof pushCtx.resume === 'function') {
            pushCtx.resume().then(() => flushMicWsQueue()).catch(() => flushMicWsQueue());
          } else {
            flushMicWsQueue();
          }
        } catch (_) {
          flushMicWsQueue();
        }
      };
      pushMicWs.onclose = () => {
        if (pushMicOn) setPushStatus('Mic stream disconnected.');
      };
      pushMicWs.onerror = () => {
        if (pushMicOn) setPushStatus('Mic stream socket error.');
      };
      const src = pushCtx.createMediaStreamSource(pushStream);
      const inChannels = Math.min(2, Math.max(1, Number(src.channelCount) || 1));
      pushNode = pushCtx.createScriptProcessor(2048, inChannels, 1);
      pushNode.onaudioprocess = (ev) => {
        if (!pushMicOn) return;
        const n = ev.inputBuffer.numberOfChannels;
        const len = ev.inputBuffer.length;
        let ch0 = ev.inputBuffer.getChannelData(0);
        if (n > 1) {
          const mix = new Float32Array(len);
          for (let c = 0; c < n; c++) {
            const d = ev.inputBuffer.getChannelData(c);
            for (let i = 0; i < len; i++) mix[i] += d[i];
          }
          const inv = 1 / n;
          for (let i = 0; i < len; i++) mix[i] *= inv;
          ch0 = mix;
        }
        const down = downsampleTo16k(ch0);
        if (!down.length) return;
        pushPcmAccumulator = concatInt16(pushPcmAccumulator, down);
        while (pushPcmAccumulator.length >= PUSH_MIC_SEND_SAMPLES) {
          const sendNow = pushPcmAccumulator.slice(0, PUSH_MIC_SEND_SAMPLES);
          pushPcmAccumulator = pushPcmAccumulator.slice(PUSH_MIC_SEND_SAMPLES);
          queuePcmChunk(sendNow);
        }
      };
      src.connect(pushNode);
      pushNode.connect(pushCtx.destination);
      try {
        if (pushCtx.state === 'suspended' && typeof pushCtx.resume === 'function') {
          await pushCtx.resume();
        }
      } catch (_) {}
      pushMicOn = true;
      setMicPushUiDisabled(true);
      if (btn) btn.textContent = '⏹ Stop push mic';
      setPushStatus(`Mic push active: ${Math.round(pushCaptureRate)}Hz capture -> 16000Hz mono to ESP32.`);
    } catch (e) {
      const msg = String(e && e.message ? e.message : e || '');
      if (/permission|denied|notallowed/i.test(msg)) {
        setPushStatus('Mic permission denied by browser. Allow microphone access and retry.');
      } else if (/secure context|getusermedia|not supported/i.test(msg)) {
        setPushStatus('Mic capture unavailable in this browser/context. Use https:// or localhost, or upload a file instead.');
      } else {
        setPushStatus(`Mic push start failed: ${msg}`);
      }
      pushMicOn = false;
      setMicPushUiDisabled(false);
    }
  }

  async function pushAudioFile() {
    const fileEl = document.getElementById('audioPushFile');
    if (!fileEl || !fileEl.files || !fileEl.files[0]) {
      setPushStatus('Select an audio file first.');
      return;
    }
    const base = boardBaseUrl();
    if (!base) {
      setPushStatus('Set board host first.');
      return;
    }
    stopPushMicInternal();
    const file = fileEl.files[0];
    const toggleBtn = pushFileToggleBtnEl();
    flashPushCue(toggleBtn);
    void fireBoardPushCue();
    try {
      const form = new FormData();
      form.append('board_url', base);
      form.append('audio_file', file);
      form.append('stream_volume', String(getBoardOutputVolumeLinear()));
      setPushStatus(`Preparing stream for ${file.name}...`);
      setPushProgress('Transcoding with FFmpeg and starting realtime stream...');
      const res = await fetch('/api/audio/push-file/start', { method: 'POST', body: form });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || `start failed ${res.status}`);
      }
      const meta = await res.json();
      const md = parseApiSeconds(meta.duration_sec);
      if (md != null && md > 0) {
        lastFileDurationSec = md;
      }
      fileStreamPeakPosition = 0;
      setFileStreamingUi(true);
      await pollPushFileStatus(true);
      if (pushFilePollTimer) window.clearInterval(pushFilePollTimer);
      pushFilePollTimer = window.setInterval(() => { pollPushFileStatus(false); }, 400);
    } catch (e) {
      setPushStatus(`File upload failed: ${e.message || e}`);
      setPushProgress('No active file stream.');
      setFileStreamingUi(false);
      resetFilePlaybackUi();
    }
  }

  async function stopPushFile() {
    const stopBtn = pushStopBtnEl();
    flashPushCue(stopBtn);
    void fireBoardPushCue();
    try {
      await fetch('/api/audio/push-file/stop', { method: 'POST' });
      setPushStatus('Stopping file stream...');
      setPushProgress('Stopping...');
    } catch (e) {
      setPushStatus(`Stop failed: ${e.message || e}`);
    }
  }

  /** Single control: Start (idle) → Pause (playing) → Play (paused); same button, icon swaps. */
  async function toggleFileStreamPrimary() {
    try {
      const res = await fetch('/api/audio/push-file/status', { method: 'GET', cache: 'no-store' });
      if (!res.ok) {
        setPushStatus(`Could not read stream status (${res.status}). Is korvo-server running?`);
        return;
      }
      const data = await res.json();
      if (data.active) {
        const paused = !!data.paused;
        const ep = paused ? '/api/audio/push-file/resume' : '/api/audio/push-file/pause';
        const r2 = await fetch(ep, { method: 'POST' });
        if (!r2.ok) {
          const t = await r2.text();
          setPushStatus(`Pause/resume failed: ${t || r2.status}`);
          return;
        }
        await pollPushFileStatus(false);
        return;
      }
      await pushAudioFile();
    } catch (e) {
      setPushStatus(`Stream control error: ${e && e.message ? e.message : e}`);
    }
  }

  async function seekFileFromProgress() {
    const progress = fileProgressEl();
    if (!progress || lastFileDurationSec == null || !(lastFileDurationSec > 0)) return;
    const t = (Number(progress.value) / 1000) * lastFileDurationSec;
    try {
      const res = await fetch('/api/audio/push-file/seek', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ position_sec: t }),
      });
      if (res.ok) await pollPushFileStatus(false);
    } catch (_) {}
  }

  function initFileProgressControl() {
    const progress = fileProgressEl();
    if (!progress) return;
    progress.addEventListener('pointerdown', (ev) => {
      fileProgressDragActive = true;
      fileProgressUserSeeking = true;
      try {
        if (typeof ev.pointerId === 'number' && progress.setPointerCapture) {
          progress.setPointerCapture(ev.pointerId);
        }
      } catch (_) {}
    });
    window.addEventListener('pointerup', finishFileProgressDrag, true);
    window.addEventListener('pointercancel', finishFileProgressDrag, true);
    progress.addEventListener('input', () => {
      if (!fileProgressDragActive || lastFileDurationSec == null || !(lastFileDurationSec > 0)) return;
      const lab = fileTimeLabelEl();
      if (!lab) return;
      const pos = (Number(progress.value) / 1000) * lastFileDurationSec;
      lab.textContent = `${formatTime(pos)} / ${formatTime(lastFileDurationSec)}`;
    });
    progress.addEventListener('change', () => {
      if (fileProgressDragActive) return;
      fileProgressUserSeeking = false;
      void seekFileFromProgress();
    });
  }

  async function pollPushFileStatus(initial) {
    try {
      const res = await fetch('/api/audio/push-file/status', { method: 'GET', cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (!data.active && !data.done) {
        if (!initial) {
          setFileStreamingUi(false);
          if (pushFilePollTimer) {
            window.clearInterval(pushFilePollTimer);
            pushFilePollTimer = null;
          }
          setPushProgress('No active file stream.');
          resetFilePlaybackUi();
        }
        return;
      }
      const kb = Math.round((Number(data.bytes_sent) || 0) / 1024);
      updateFilePlaybackControls(data);
      if (data.active) {
        setPushStatus(`Streaming ${data.source_name || 'file'} to board...`);
        setPushProgress(`Sent ${kb} KB · ${Number(data.chunks_sent) || 0} chunks`);
        setFileStreamingUi(true);
      } else {
        setFileStreamingUi(false);
        if (pushFilePollTimer) {
          window.clearInterval(pushFilePollTimer);
          pushFilePollTimer = null;
        }
        if (data.error) {
          setPushStatus(`File stream failed: ${data.error}`);
          setPushProgress(`Stopped after ${kb} KB.`);
        } else if (data.stopped) {
          setPushStatus('File stream stopped.');
          setPushProgress(`Stopped after ${kb} KB.`);
        } else {
          setPushStatus('File stream complete.');
          setPushProgress(`Finished streaming ${kb} KB.`);
        }
        resetFilePlaybackUi();
      }
    } catch (_) {}
  }

  let audioSectionInited = false;
  function initAudioSection() {
    if (audioSectionInited) return;
    audioSectionInited = true;
    initBoardVolumeControl();
    initFileProgressControl();
    setPushStatus('Idle.');
    setPushProgress('No active file stream.');
    setFileStreamingUi(false);
    resetFilePlaybackUi();
    const mic = pushMicBtnEl();
    if (mic) mic.disabled = false;
  }

  window.togglePushMic = togglePushMic;
  window.pushAudioFile = pushAudioFile;
  window.stopPushFile = stopPushFile;
  window.toggleFileStreamPrimary = toggleFileStreamPrimary;
  window.initAudioSection = initAudioSection;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initAudioSection(), { once: true });
  } else {
    initAudioSection();
  }
  window.getBoardOutputVolumeLinear = getBoardOutputVolumeLinear;
  window.getBoardOutputVolumePercent = getBoardOutputVolumePercent;
})();
