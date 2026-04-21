// Translation section: AssemblyAI key persistence in SQLite via /api/settings.
(function () {
  function translationStatusEl() {
    return document.getElementById('translationStatus');
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
  }

  async function translateTextGoogle() {
    const status = translationStatusEl();
    const srcEl = document.getElementById('translationSourceLang');
    const tgtEl = document.getElementById('translationTargetLang');
    const inEl = document.getElementById('translationInputText');
    const outEl = document.getElementById('translationOutputText');
    if (!srcEl || !tgtEl || !inEl || !outEl) return;
    const source_language = (srcEl.value || 'en').trim().toLowerCase();
    const target_language = (tgtEl.value || 'ja').trim().toLowerCase();
    const text = (inEl.value || '').trim();
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
    const res = await fetch('/api/translate/google', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, source_language, target_language }),
    });
    if (!res.ok) {
      const msg = await res.text();
      if (status) status.textContent = 'Translation failed.';
      if (typeof toast === 'function') toast('Google translation failed', 'error');
      outEl.value = msg || '';
      return;
    }
    const data = await res.json();
    outEl.value = data.translated_text || '';
    if (status) status.textContent = `Translated ${source_language} → ${target_language}`;
    if (typeof toast === 'function') toast('Translation complete');
  }

  window.saveTranslationSettings = saveTranslationSettings;
  window.initTranslationSection = initTranslationSection;
  window.translateTextGoogle = translateTextGoogle;
})();
