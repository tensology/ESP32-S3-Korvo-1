// Translation section: AssemblyAI key persistence in SQLite via /api/settings.
(function () {
  const FALLBACK_KOKORO_VOICES = ['af_alloy', 'af_aoede', 'af_bella', 'af_heart', 'af_jessica', 'af_kore', 'af_nicole', 'af_nova', 'af_river', 'af_sarah', 'af_sky', 'am_adam', 'am_echo', 'am_eric', 'am_fenrir', 'am_liam', 'am_michael', 'am_onyx', 'am_puck', 'am_santa', 'bf_alice', 'bf_emma', 'bf_isabella', 'bf_lily', 'bm_daniel', 'bm_fable', 'bm_george', 'bm_lewis', 'ef_dora', 'em_alex', 'em_santa', 'ff_siwis', 'hf_alpha', 'hf_beta', 'hm_omega', 'hm_psi', 'if_sara', 'im_nicola', 'jf_alpha', 'jf_gongitsune', 'jf_nezumi', 'jf_tebukuro', 'jm_kumo', 'pf_dora', 'pm_alex', 'pm_santa', 'zf_xiaobei', 'zf_xiaoni', 'zf_xiaoxiao', 'zf_xiaoyi', 'zm_yunjian', 'zm_yunxi', 'zm_yunxia', 'zm_yunyang'];
  const DEFAULT_VOICE_BY_TARGET = { en: 'af_heart', ja: 'jf_alpha', es: 'ef_dora', fr: 'ff_siwis', it: 'if_sara', pt: 'pf_dora', hi: 'hf_alpha', 'zh-cn': 'zf_xiaobei', 'zh-tw': 'zf_xiaobei' };
  const VOICE_PREFIXES_BY_TARGET = { en: ['a', 'b'], es: ['e'], fr: ['f'], hi: ['h'], it: ['i'], ja: ['j'], pt: ['p'], 'zh-cn': ['z'], 'zh-tw': ['z'] };
  let availableKokoroVoices = FALLBACK_KOKORO_VOICES.slice();
  let translationAutoWs = null;
  let translationAutoQueue = [];
  let translationAutoBusy = false;
  let translationAutoLastQueuedSentence = '';
  let translationAutoLastSentenceId = 0;
  let translationAutoLastRequestAt = 0;
  let translationTtsObsTimer = null;
  const TRANSLATION_AUTO_MIN_GAP_MS = 700;
  /** Show Whisper partials in the auto-transcribe line (same fluidity as the ASR tab). */
  let translationAutoPreviewMode = 'live_partial';
  let translationAutoDesired = false;
  let translationAutoReconnectTimer = null;
  let translationAutoReconnectAttempts = 0;
  const TRANSLATION_AUTO_RECONNECT_MAX = 20;
  const TRANSLATION_AUTO_RECONNECT_BASE_MS = 700;

  function translationStatusEl() {
    return document.getElementById('translationStatus');
  }

  function setPlaybackTargetLocked(locked) {
    const el = document.getElementById('translationPlaybackTarget');
    if (!el) return;
    el.disabled = !!locked;
    el.title = locked
      ? 'Stop auto transcribing to change TTS output.'
      : '';
  }

  function translationTtsObservabilityEl() {
    return document.getElementById('translationTtsObservability');
  }

  function renderTtsObservability(message) {
    const el = translationTtsObservabilityEl();
    if (!el) return;
    el.textContent = message || 'TTS session metrics: idle';
  }

  function formatTtsOrchestrator(orchestrator) {
    if (!orchestrator || typeof orchestrator !== 'object') return '';
    return `queue=${Number(orchestrator.queue_depth || 0)} · enq=${Number(orchestrator.enqueued_total || 0)} · replaced=${Number(orchestrator.replaced_total || 0)} · done=${Number(orchestrator.completed_total || 0)} · preempted=${Number(orchestrator.preempted_total || 0)}`;
  }

  async function pollTtsSessionsNow() {
    const board = boardBaseUrl();
    if (!board) {
      renderTtsObservability('TTS session metrics: set board host to monitor queue health');
      return;
    }
    try {
      const res = await fetch('/api/translation/tts-sessions');
      if (!res.ok) return;
      const data = await res.json();
      const sessions = Array.isArray(data && data.sessions) ? data.sessions : [];
      const target = sessions.find((s) => String(s.board_base || '') === board);
      if (!target) {
        renderTtsObservability('TTS session metrics: no active session yet');
        return;
      }
      renderTtsObservability(
        `TTS session: queue=${Number(target.queue_depth || 0)} · active=${target.active ? 'yes' : 'no'} · enq=${Number(target.enqueued_total || 0)} · replaced=${Number(target.replaced_total || 0)} · done=${Number(target.completed_total || 0)} · fail=${Number(target.failed_total || 0)} · preempted=${Number(target.preempted_total || 0)}`
      );
    } catch (_) {}
  }

  function startTtsObservabilityPolling() {
    if (translationTtsObsTimer) return;
    pollTtsSessionsNow();
    translationTtsObsTimer = setInterval(pollTtsSessionsNow, 1800);
  }

  function stopTtsObservabilityPolling() {
    if (!translationTtsObsTimer) return;
    clearInterval(translationTtsObsTimer);
    translationTtsObsTimer = null;
  }

  function clearAutoReconnectTimer() {
    if (!translationAutoReconnectTimer) return;
    clearTimeout(translationAutoReconnectTimer);
    translationAutoReconnectTimer = null;
  }

  /** server_local = Kokoro + afplay on the Mac; board_inject = stream to ESP32 */
  function ttsPlaybackLabel(data) {
    return data && data.playback_target === 'server_local' ? 'this Mac' : 'device';
  }

  function normalizeBoardHost(raw) {
    let v = String(raw || '').trim();
    if (!v) return '';
    v = v.replace(/^https?:\/\//i, '');
    const slash = v.indexOf('/');
    if (slash >= 0) v = v.slice(0, slash);
    return v.trim();
  }

  /** Same LAN URL the server uses to POST /api/audio/inject (prefers #boardIp, else saved WiFi / board IP). */
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

  function ensureBoardIpFieldFromStorage() {
    const ipEl = document.getElementById('boardIp');
    if (!ipEl) return;
    const cur = (ipEl.value || '').trim();
    if (cur) return;
    try {
      const s = (localStorage.getItem('korvo_board_ip') || localStorage.getItem('korvo_esp_ip') || '').trim();
      if (s) {
        const n = normalizeBoardHost(s);
        ipEl.value = n || s;
      }
    } catch (_) {}
  }

  /** Apply Audio-tab output volume to the ESP immediately so TTS is not silent if the slider never POSTed. */
  async function flushBoardPlaybackVolumeIfPossible() {
    const base = boardBaseUrl();
    if (!base || typeof window.getBoardOutputVolumePercent !== 'function') return;
    const pct = window.getBoardOutputVolumePercent();
    try {
      const signal =
        typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
          ? AbortSignal.timeout(1200)
          : undefined;
      await fetch(`${base}/api/audio/output-volume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ volume: pct }),
        signal: signal,
      });
    } catch (_) {}
  }

  async function saveTranslationSettings() {
    const keyEl = document.getElementById('assemblyAiApiKey');
    const status = translationStatusEl();
    if (!keyEl) return;
    const key = keyEl.value.trim();
    if (!key) {
      if (status) status.textContent = 'Please add an AssemblyAI API key.';
      return;
    }
    if (/\s/.test(key)) {
      if (status) status.textContent = 'AssemblyAI API key cannot contain spaces.';
      if (typeof toast === 'function') toast('Invalid AssemblyAI key format', 'error');
      return;
    }
    if (key.length < 20) {
      if (status) status.textContent = 'AssemblyAI API key looks too short.';
      if (typeof toast === 'function') toast('AssemblyAI key looks invalid', 'error');
      return;
    }
    if (!/^[A-Za-z0-9._-]+$/.test(key)) {
      if (status) status.textContent = 'AssemblyAI API key has invalid characters.';
      if (typeof toast === 'function') toast('Invalid AssemblyAI key format', 'error');
      return;
    }
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assemblyai_api_key: key }),
    });
    if (!res.ok) {
      if (status) status.textContent = 'Failed to save AssemblyAI API key.';
      if (typeof toast === 'function') toast('Failed to save AssemblyAI key', 'error');
      return;
    }
    if (status) status.textContent = 'AssemblyAI API key saved to SQLite settings.';
    if (typeof toast === 'function') toast('AssemblyAI key saved');
  }

  function initTranslationSection() {
    const status = translationStatusEl();
    if (status && !status.dataset.init) {
      status.dataset.init = '1';
      if (!status.textContent.trim()) status.textContent = 'Realtime Translation Coming Soon!';
    }
    setTranslationAutoBadge('disconnected', 'Off');
    ensureBoardIpFieldFromStorage();
    setKokoroVoiceOptions(FALLBACK_KOKORO_VOICES, 'af_heart');
    loadKokoroVoices();
    bindTargetLanguageVoiceDefault();
  }

  function setKokoroVoiceOptions(voices, selectedVoice) {
    const voiceEl = document.getElementById('translationKokoroVoice');
    if (!voiceEl) return;
    const list = Array.isArray(voices) && voices.length ? voices : FALLBACK_KOKORO_VOICES;
    const selected = (selectedVoice || 'af_heart').trim();
    voiceEl.innerHTML = '';
    list.forEach((v) => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      if (v === selected) opt.selected = true;
      voiceEl.appendChild(opt);
    });
  }

  async function loadKokoroVoices() {
    const voiceEl = document.getElementById('translationKokoroVoice');
    if (!voiceEl) return;
    try {
      const res = await fetch('/api/tts/kokoro/voices');
      if (!res.ok) return;
      const data = await res.json();
      const voices = Array.isArray(data.voices) ? data.voices : [];
      const selected = (data.default || 'af_heart').trim();
      if (!voices.length) {
        availableKokoroVoices = FALLBACK_KOKORO_VOICES.slice();
        setKokoroVoiceOptions(FALLBACK_KOKORO_VOICES, selected);
        applyTargetLanguageVoices();
        return;
      }
      availableKokoroVoices = voices.slice();
      setKokoroVoiceOptions(voices, selected);
      applyTargetLanguageVoices();
    } catch (_) {
      availableKokoroVoices = FALLBACK_KOKORO_VOICES.slice();
      setKokoroVoiceOptions(FALLBACK_KOKORO_VOICES, 'af_heart');
      applyTargetLanguageVoices();
    }
  }

  function voicesForTarget(targetCode) {
    const code = (targetCode || 'en').trim().toLowerCase();
    const prefixes = VOICE_PREFIXES_BY_TARGET[code];
    if (!prefixes || !prefixes.length) return availableKokoroVoices.filter((v) => /^[ab]/.test(v));
    const out = availableKokoroVoices.filter((v) => prefixes.some((p) => v.startsWith(p)));
    return out.length ? out : availableKokoroVoices.filter((v) => /^[ab]/.test(v));
  }

  function applyTargetLanguageVoices() {
    const tgtEl = document.getElementById('translationTargetLang');
    const voiceEl = document.getElementById('translationKokoroVoice');
    if (!tgtEl || !voiceEl) return;
    const target = (tgtEl.value || 'ja').trim().toLowerCase();
    const current = (voiceEl.value || '').trim();
    const preferred = DEFAULT_VOICE_BY_TARGET[target] || 'af_heart';
    const list = voicesForTarget(target);
    const pick = list.includes(current) ? current : (list.includes(preferred) ? preferred : (list[0] || 'af_heart'));
    setKokoroVoiceOptions(list, pick);
  }

  function bindTargetLanguageVoiceDefault() {
    const tgtEl = document.getElementById('translationTargetLang');
    const srcEl = document.getElementById('translationSourceLang');
    const voiceEl = document.getElementById('translationKokoroVoice');
    if (!tgtEl || !srcEl || !voiceEl || tgtEl.dataset.voiceBound === '1') return;
    tgtEl.dataset.voiceBound = '1';
    const enforceDistinctLanguages = () => {
      const source = (srcEl.value || 'en').trim().toLowerCase();
      const target = (tgtEl.value || 'ja').trim().toLowerCase();
      if (source === target) {
        tgtEl.value = source === 'en' ? 'ja' : 'en';
      }
    };
    srcEl.addEventListener('change', () => {
      enforceDistinctLanguages();
      applyTargetLanguageVoices();
    });
    tgtEl.addEventListener('change', () => {
      enforceDistinctLanguages();
      applyTargetLanguageVoices();
    });
    enforceDistinctLanguages();
    applyTargetLanguageVoices();
  }

  async function translateTextGoogle() {
    const status = translationStatusEl();
    const srcEl = document.getElementById('translationSourceLang');
    const tgtEl = document.getElementById('translationTargetLang');
    const inEl = document.getElementById('translationInputText');
    const outEl = document.getElementById('translationOutputText');
    const speakEl = document.getElementById('translationSpeakTarget');
    const playbackEl = document.getElementById('translationPlaybackTarget');
    const voiceEl = document.getElementById('translationKokoroVoice');
    if (!srcEl || !tgtEl || !inEl || !outEl || !speakEl || !voiceEl || !playbackEl) return;
    const source_language = (srcEl.value || 'en').trim().toLowerCase();
    const target_language = (tgtEl.value || 'ja').trim().toLowerCase();
    const kokoro_voice = (voiceEl.value || 'af_heart').trim();
    const speak_target = !!speakEl.checked;
    const playback_target = (playbackEl.value || 'board_inject').trim();
    const text = (inEl.value || '').trim();
    ensureBoardIpFieldFromStorage();
    const board_url = boardBaseUrl();
    if (!text) {
      if (status) status.textContent = 'Add text to translate first.';
      if (typeof toast === 'function') toast('No text to translate', 'error');
      return;
    }
    if (!/^[a-z-]{2,12}$/.test(source_language) || !/^[a-z-]{2,12}$/.test(target_language)) {
      if (status) status.textContent = 'Use valid language codes (e.g. en, ja, es, zh-cn).';
      if (typeof toast === 'function') toast('Invalid language code', 'error');
      return;
    }
    outEl.value = '';
    if (status) status.textContent = 'Translating...';
    if (speak_target && playback_target === 'board_inject' && !board_url) {
      if (status) status.textContent = 'Set board host on the Audio tab (or connect WiFi once so the IP is saved), then retry.';
      if (typeof toast === 'function') toast('Set board host for device TTS', 'error');
      return;
    }
    if (speak_target && playback_target === 'board_inject' && board_url) {
      await flushBoardPlaybackVolumeIfPossible();
    }
    const startedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const res = await fetch('/api/translate/google', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        source_language,
        target_language,
        speak_target,
        kokoro_voice,
        kokoro_speed: 1.0,
        playback_target,
        board_url,
        stream_key: (typeof _boardStreamUrl === 'function' ? (_boardStreamUrl() || '') : ''),
        stream_volume: 1,
        tts_sentence_stream: true,
      }),
    });
    if (!res.ok) {
      const msg = await res.text();
      if (res.status === 404) {
        if (status) status.textContent = 'Translation API not loaded (restart korvo-server to pick up new route).';
        if (typeof toast === 'function') toast('Restart server: translation route missing', 'error');
        outEl.value = '';
        return;
      }
      if (status) status.textContent = 'Translation failed.';
      if (typeof toast === 'function') toast('Google translation failed', 'error');
      outEl.value = msg || '';
      return;
    }
    const data = await res.json();
    const endedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const totalMs = Math.max(0, Math.round(endedAt - startedAt));
    outEl.value = data.translated_text || '';
    inEl.value = '';
    try {
      console.log('[translation-debug]', {
        source_text: text,
        translated_text: data.translated_text || '',
        tts_input_text: data.tts_input_text || '',
        source_language,
        target_language,
        speak_target: !!speak_target,
        kokoro_voice,
        kokoro_lang: data.kokoro_lang || '',
        speak_done: !!data.speak_done,
        speak_pending: !!data.speak_pending,
        speak_error: data.speak_error || '',
        tts_board_chunks: data.tts_board_chunks,
        request_ms: totalMs,
        translate_ms_server: data.translate_ms,
        total_ms_server: data.total_ms,
      });
    } catch (_) {}
    if (data.speak_target && data.speak_pending) {
      const n = Number(data.tts_board_chunks) || 0;
      const where = ttsPlaybackLabel(data);
      const orch = formatTtsOrchestrator(data.tts_orchestrator);
      if (status) {
        status.textContent =
          n > 1
            ? `Translated ${source_language} → ${target_language} in ${totalMs}ms · TTS starting on ${where} (${n} phrases, in background${orch ? ` · ${orch}` : ''})`
            : `Translated ${source_language} → ${target_language} in ${totalMs}ms · TTS starting on ${where} (in background${orch ? ` · ${orch}` : ''})`;
      }
    } else if (data.speak_target && data.speak_done) {
      const n = Number(data.tts_board_chunks) || 0;
      const where = ttsPlaybackLabel(data);
      if (n > 1) {
        if (status) status.textContent = `Translated ${source_language} → ${target_language} · played on ${where} (${n} phrases)`;
      } else if (status) {
        status.textContent = `Translated ${source_language} → ${target_language} in ${totalMs}ms and played on ${where}`;
      }
    } else if (data.speak_target && data.speak_error) {
      if (status) status.textContent = `Translated ${source_language} → ${target_language}, but speak failed: ${data.speak_error}`;
      if (typeof toast === 'function') toast('Speak target failed', 'error');
    } else {
      if (status) status.textContent = `Translated ${source_language} → ${target_language} in ${totalMs}ms`;
    }
    if (data.tts_orchestrator) {
      renderTtsObservability(`TTS session: ${formatTtsOrchestrator(data.tts_orchestrator)}`);
    } else if (playback_target === 'board_inject') {
      startTtsObservabilityPolling();
    }
    if (typeof toast === 'function') toast('Translation complete');
  }

  async function copyTranslationOutput() {
    const outEl = document.getElementById('translationOutputText');
    const status = translationStatusEl();
    const text = outEl ? String(outEl.value || '').trim() : '';
    if (!text) {
      if (status) status.textContent = 'Nothing to copy yet.';
      if (typeof toast === 'function') toast('No translated output to copy', 'error');
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else if (outEl) {
        outEl.focus();
        outEl.select();
        document.execCommand('copy');
      }
      if (status) status.textContent = 'Translated output copied.';
      if (typeof toast === 'function') toast('Output copied');
    } catch (_) {
      if (status) status.textContent = 'Copy failed. Select text manually.';
      if (typeof toast === 'function') toast('Copy failed', 'error');
    }
  }

  function translationAutoStatusEl() {
    return document.getElementById('translationAutoTranscribeStatus');
  }

  function setTranslationAutoBadge(state, text) {
    const el = translationAutoStatusEl();
    if (!el) return;
    const s = (state || 'disconnected').trim();
    el.className = `badge translation-auto-status ${s}`;
    el.textContent = text || '';
  }

  function translationAutoLiveEl() {
    return document.getElementById('translationAutoTranscribeLive');
  }

  function normalizeSnippet(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
  }

  function shouldSkipAutoSentence(text) {
    const t = normalizeSnippet(text);
    if (!t) return true;
    const upper = t.toUpperCase();
    if (upper === '[BLANK_AUDIO]' || upper === 'BLANK_AUDIO') return true;
    if (upper === '[SILENCE]' || upper === 'SILENCE') return true;
    if (upper === '[NOISE]' || upper === 'NOISE') return true;
    if (/^\[[A-Z_]+\]$/.test(upper)) return true;
    return false;
  }

  function sanitizeAsrChunk(text) {
    let t = String(text || '');
    t = t.replace(/\[[^\]]*\]/g, ' ');
    t = t.replace(/\([^)]*\)/g, ' ');
    t = t.replace(/<[^>]*>/g, ' ');
    t = normalizeSnippet(t);
    if (!t) return '';
    const fillerOnly = /^(soft music|music|applause|laughs?|laughter|uh|um|hmm|mmm|mm-hmm|ah|er|uh-huh)$/i;
    if (fillerOnly.test(t)) return '';
    const fillerWords = /\b(uh|um|hmm|mm-hmm|uh-huh)\b/gi;
    return normalizeSnippet(t.replace(fillerWords, ' '));
  }

  function isDuplicateSentence(sentence, sentenceId) {
    const sid = Number(sentenceId || 0);
    if (sid > 0 && sid <= translationAutoLastSentenceId) return true;
    const cur = normalizeSnippet(String(sentence || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' '));
    const prev = normalizeSnippet(String(translationAutoLastQueuedSentence || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' '));
    if (cur && prev && cur === prev) return true;
    return false;
  }

  async function processAutoQueue() {
    if (translationAutoBusy || !translationAutoQueue.length) return;
    translationAutoBusy = true;
    // Keep live mode realtime: translate newest sentence only, drop stale backlog.
    const sentence = translationAutoQueue[translationAutoQueue.length - 1];
    translationAutoQueue = [];
    try {
      const now = Date.now();
      const sinceLast = now - translationAutoLastRequestAt;
      if (sinceLast < TRANSLATION_AUTO_MIN_GAP_MS) {
        await new Promise((resolve) => setTimeout(resolve, TRANSLATION_AUTO_MIN_GAP_MS - sinceLast));
      }
      await translateTextGoogleWithText(sentence);
      translationAutoLastRequestAt = Date.now();
    } finally {
      translationAutoBusy = false;
      if (translationAutoQueue.length) processAutoQueue();
    }
  }

  async function translateTextGoogleWithText(inputText) {
    const status = translationStatusEl();
    const srcEl = document.getElementById('translationSourceLang');
    const tgtEl = document.getElementById('translationTargetLang');
    const outEl = document.getElementById('translationOutputText');
    const speakEl = document.getElementById('translationSpeakTarget');
    const playbackEl = document.getElementById('translationPlaybackTarget');
    const voiceEl = document.getElementById('translationKokoroVoice');
    const inEl = document.getElementById('translationInputText');
    if (!srcEl || !tgtEl || !outEl || !speakEl || !voiceEl || !inEl || !playbackEl) return;
    const text = normalizeSnippet(inputText);
    if (!text || shouldSkipAutoSentence(text)) return;
    const source_language = (srcEl.value || 'en').trim().toLowerCase();
    const target_language = (tgtEl.value || 'ja').trim().toLowerCase();
    const kokoro_voice = (voiceEl.value || 'af_heart').trim();
    const speak_target = !!speakEl.checked;
    const playback_target = (playbackEl.value || 'board_inject').trim();
    ensureBoardIpFieldFromStorage();
    const board_url = boardBaseUrl();
    inEl.value = text;
    if (status) status.textContent = 'Auto translating sentence...';
    if (speak_target && playback_target === 'board_inject' && !board_url) {
      if (status) status.textContent = 'Set board host on the Audio tab (or save WiFi IP) so auto TTS can reach the ESP.';
      return;
    }
    if (speak_target && playback_target === 'board_inject' && board_url) {
      await flushBoardPlaybackVolumeIfPossible();
    }
    const stream_key =
      typeof _boardStreamUrl === 'function' ? (_boardStreamUrl() || '') : `${board_url.replace(/\/$/, '')}/api/audio/stream`;
    const startedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const res = await fetch('/api/translate/google', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        source_language,
        target_language,
        speak_target,
        kokoro_voice,
        kokoro_speed: 1.0,
        playback_target,
        board_url,
        stream_key,
        stream_volume: 1,
        tts_sentence_stream: true,
      }),
    });
    if (!res.ok) {
      const msg = await res.text();
      if (status) status.textContent = `Auto translation failed: ${msg}`;
      return;
    }
    const data = await res.json();
    const endedAt = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const totalMs = Math.max(0, Math.round(endedAt - startedAt));
    outEl.value = data.translated_text || '';
    inEl.value = '';
    if (data.speak_target && data.speak_pending) {
      const nc = Number(data.tts_board_chunks) || 0;
      const where = ttsPlaybackLabel(data);
      const orch = formatTtsOrchestrator(data.tts_orchestrator);
      if (status) {
        status.textContent =
          nc > 1
            ? `Auto translated in ${totalMs}ms · TTS streaming to ${where} (${nc} phrases, ${data.tts_vendor_used || 'kokoro'}${orch ? ` · ${orch}` : ''})`
            : `Auto translated in ${totalMs}ms · TTS streaming to ${where} (${data.tts_vendor_used || 'kokoro'}${orch ? ` · ${orch}` : ''})`;
      }
    } else if (data.speak_target && data.speak_done) {
      const nc = Number(data.tts_board_chunks) || 0;
      const where = ttsPlaybackLabel(data);
      if (nc > 1 && status) {
        status.textContent = `Auto translated in ${totalMs}ms · played on ${where} (${nc} phrases, ${data.tts_vendor_used || 'kokoro'})`;
      } else if (status) {
        status.textContent = `Auto translated in ${totalMs}ms and played on ${where} (${data.tts_vendor_used || 'kokoro'})`;
      }
    } else if (data.speak_target && data.speak_error) {
      if (status) status.textContent = `Auto translated, speak failed: ${data.speak_error}`;
    } else {
      if (status) status.textContent = `Auto translated sentence in ${totalMs}ms.`;
    }
    if (data.tts_orchestrator) {
      renderTtsObservability(`TTS session: ${formatTtsOrchestrator(data.tts_orchestrator)}`);
    } else if (playback_target === 'board_inject') {
      startTtsObservabilityPolling();
    }
  }

  async function toggleTranslationAutoTranscribe() {
    const btn = document.getElementById('translationAutoTranscribeBtn');
    const st = translationAutoStatusEl();
    const live = translationAutoLiveEl();
    if (!btn || !st || !live) return;
    if (translationAutoWs) {
      translationAutoDesired = false;
      clearAutoReconnectTimer();
      try { translationAutoWs.close(); } catch (_) {}
      translationAutoWs = null;
      btn.textContent = '▶️ Start auto transcribing';
      setTranslationAutoBadge('disconnected', 'Off');
      setPlaybackTargetLocked(false);
      stopTtsObservabilityPolling();
      return;
    }
    let streamUrl = '';
    if (typeof _boardStreamUrl === 'function') streamUrl = _boardStreamUrl();
    if (!streamUrl) {
      setTranslationAutoBadge('disconnected', 'No board');
      if (typeof toast === 'function') toast('Set board host first', 'error');
      return;
    }
    const modelEl = document.getElementById('whisperModel');
    const stepEl = document.getElementById('transcribeStepSec');
    const winEl = document.getElementById('transcribeWindowSec');
    const model = (modelEl && modelEl.value) ? modelEl.value.trim() : 'base.en';
    const step_sec = String(parseFloat((stepEl && stepEl.value) || '1.25') || 1.25);
    const window_sec = String(parseFloat((winEl && winEl.value) || '5') || 5);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const q = new URLSearchParams({ board_url: streamUrl, model, step_sec, window_sec });
    const wsUrl = `${proto}//${location.host}/ws/audio/transcribe?${q.toString()}`;
    translationAutoQueue = [];
    translationAutoBusy = false;
    translationAutoLastQueuedSentence = '';
    translationAutoLastSentenceId = 0;
    translationAutoDesired = true;
    translationAutoReconnectAttempts = 0;
    clearAutoReconnectTimer();
    let gotTranscribePayload = false;
    live.textContent = 'Connecting…';
    setTranslationAutoBadge('scanning', 'Connecting');
    setPlaybackTargetLocked(true);
    startTtsObservabilityPolling();
    const connectAutoWs = () => {
      if (!translationAutoDesired) return;
      if (translationAutoWs) return;
      translationAutoWs = new WebSocket(wsUrl);
      translationAutoWs.onopen = () => {
        translationAutoReconnectAttempts = 0;
        btn.textContent = '⏹ Stop auto transcribing';
        setTranslationAutoBadge('scanning', 'Connecting');
        if (!live.textContent || live.textContent === 'Connecting…') {
          live.textContent = 'Listening… speak near the board mic.';
        }
      };
      translationAutoWs.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === 'sentence') {
        const s = sanitizeAsrChunk(msg.text || '');
        if (s && !shouldSkipAutoSentence(s) && !isDuplicateSentence(s, msg.sentence_id)) {
          gotTranscribePayload = true;
          setTranslationAutoBadge('connected', 'Live');
          translationAutoQueue.push(s);
          if (translationAutoQueue.length > 3) {
            translationAutoQueue = translationAutoQueue.slice(-1);
          }
          translationAutoLastQueuedSentence = s;
          if (Number(msg.sentence_id || 0) > 0) translationAutoLastSentenceId = Number(msg.sentence_id);
          live.textContent = s;
          processAutoQueue();
        }
      } else if (msg.type === 'silence_clear') {
        // Server-driven stale sentence reset after sustained silence.
        live.textContent = '…';
        translationAutoLastQueuedSentence = '';
        translationAutoQueue = [];
      } else if (msg.type === 'partial') {
        if (translationAutoPreviewMode === 'live_partial') {
          const full = normalizeSnippet(msg.text || '');
          const delta = normalizeSnippet(msg.delta != null ? String(msg.delta) : '');
          const cleanDelta = sanitizeAsrChunk(delta);
          const cleanFull = sanitizeAsrChunk(full);
          const fallbackRaw = cleanDelta ? '' : (cleanFull ? '' : (delta || full));
          const show = cleanDelta || cleanFull || fallbackRaw;
          if (show && !shouldSkipAutoSentence(show)) {
            gotTranscribePayload = true;
            setTranslationAutoBadge('connected', 'Live');
            live.textContent = show;
          }
        }
      } else if (msg.type === 'error') {
        setTranslationAutoBadge('disconnected', 'Error');
      } else if (msg.type === 'ready') {
        setTranslationAutoBadge('scanning', 'Listening');
        if (!live.textContent || live.textContent === 'Connecting…') {
          live.textContent = 'Listening… speak near the board mic.';
        }
      }
      };
      translationAutoWs.onerror = () => {
        if (translationAutoDesired) {
          setTranslationAutoBadge('scanning', 'Reconnecting');
        } else {
          setTranslationAutoBadge('disconnected', 'WS error');
        }
      };
      translationAutoWs.onclose = async () => {
        translationAutoWs = null;
        if (translationAutoDesired) {
          const attempt = Math.min(translationAutoReconnectAttempts + 1, TRANSLATION_AUTO_RECONNECT_MAX);
          translationAutoReconnectAttempts = attempt;
          const delay = Math.min(8000, TRANSLATION_AUTO_RECONNECT_BASE_MS * Math.pow(1.35, Math.max(0, attempt - 1)));
          setTranslationAutoBadge('scanning', `Reconnecting (${attempt})`);
          clearAutoReconnectTimer();
          translationAutoReconnectTimer = setTimeout(() => {
            translationAutoReconnectTimer = null;
            connectAutoWs();
          }, delay);
          return;
        }
        btn.textContent = '▶️ Start auto transcribing';
        setTranslationAutoBadge('disconnected', gotTranscribePayload ? 'Off' : 'Disconnected');
        setPlaybackTargetLocked(false);
        stopTtsObservabilityPolling();
      };
    };
    connectAutoWs();
  }

  window.saveTranslationSettings = saveTranslationSettings;
  window.initTranslationSection = initTranslationSection;
  window.translateTextGoogle = translateTextGoogle;
  window.copyTranslationOutput = copyTranslationOutput;
  window.toggleTranslationAutoTranscribe = toggleTranslationAutoTranscribe;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTranslationSection, { once: true });
  } else {
    initTranslationSection();
  }
})();
