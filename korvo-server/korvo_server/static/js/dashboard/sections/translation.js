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

  window.saveTranslationSettings = saveTranslationSettings;
  window.initTranslationSection = initTranslationSection;
})();
