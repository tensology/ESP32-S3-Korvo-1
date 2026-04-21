(function () {
  function thirdPartyStatusEl() {
    return document.getElementById('thirdPartyStatus');
  }

  function getValue(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  async function saveThirdPartySettings() {
    const status = thirdPartyStatusEl();
    const payload = {
      assemblyai_api_key: getValue('thirdPartyAssemblyAiApiKey'),
      openai_api_key: getValue('thirdPartyOpenAiApiKey'),
      anthropic_api_key: getValue('thirdPartyAnthropicApiKey'),
      google_gemini_api_key: getValue('thirdPartyGoogleGeminiApiKey'),
      elevenlabs_api_key: getValue('thirdPartyElevenLabsApiKey'),
    };
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      if (status) status.textContent = 'Failed to save API keys.';
      if (typeof toast === 'function') toast('Failed to save API keys', 'error');
      return;
    }
    if (status) status.textContent = 'API keys saved to SQLite settings.';
    if (typeof toast === 'function') toast('Third-party API keys saved');
  }

  function initThirdPartySection() {
    const status = thirdPartyStatusEl();
    if (status && !status.dataset.init) {
      status.dataset.init = '1';
      if (!status.textContent.trim()) status.textContent = 'API keys are saved to local SQLite settings.';
    }
  }

  window.saveThirdPartySettings = saveThirdPartySettings;
  window.initThirdPartySection = initThirdPartySection;
})();
