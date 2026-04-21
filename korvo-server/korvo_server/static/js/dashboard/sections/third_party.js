(function () {
  function thirdPartyStatusEl() {
    return document.getElementById('thirdPartyStatus');
  }

  function getValue(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  function setAwsPollyStatus(text, ok) {
    const el = document.getElementById('thirdPartyAwsPollyStatus');
    if (!el) return;
    el.textContent = text;
    el.style.color = ok ? '#4caf50' : '#ff9800';
  }

  function keyInputIdForVendor(vendor) {
    if (vendor === 'assemblyai') return 'thirdPartyAssemblyAiApiKey';
    if (vendor === 'openai') return 'thirdPartyOpenAiApiKey';
    if (vendor === 'anthropic') return 'thirdPartyAnthropicApiKey';
    if (vendor === 'gemini') return 'thirdPartyGoogleGeminiApiKey';
    if (vendor === 'elevenlabs') return 'thirdPartyElevenLabsApiKey';
    return '';
  }

  async function validateVendorKey(vendor) {
    const status = thirdPartyStatusEl();
    if (vendor === 'aws_polly') return validateAwsPolly();
    const inputId = keyInputIdForVendor(vendor);
    if (!inputId) return false;
    const api_key = getValue(inputId);
    if (!api_key) {
      if (status) status.textContent = `Add ${vendor} key first.`;
      if (typeof toast === 'function') toast(`Missing ${vendor} key`, 'error');
      return false;
    }
    const res = await fetch('/api/vendors/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vendor, api_key }),
    });
    if (!res.ok) {
      if (status) status.textContent = `${vendor} key validation failed.`;
      if (typeof toast === 'function') toast(`${vendor} key invalid`, 'error');
      return false;
    }
    if (status) status.textContent = `${vendor} key validated.`;
    if (typeof toast === 'function') toast(`${vendor} key valid`);
    return true;
  }

  async function validateAwsPolly() {
    const payload = {
      aws_access_key_id: getValue('thirdPartyAwsAccessKeyId'),
      aws_secret_access_key: getValue('thirdPartyAwsSecretAccessKey'),
      aws_region: getValue('thirdPartyAwsRegion') || 'eu-west-1',
      aws_session_token: getValue('thirdPartyAwsSessionToken'),
    };
    if (!payload.aws_access_key_id || !payload.aws_secret_access_key) {
      setAwsPollyStatus('Missing AWS key/secret', false);
      if (typeof toast === 'function') toast('Add AWS key and secret first', 'error');
      return false;
    }
    const res = await fetch('/api/vendors/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        vendor: 'aws_polly',
        aws_access_key_id: payload.aws_access_key_id,
        aws_secret_access_key: payload.aws_secret_access_key,
        aws_region: payload.aws_region,
        aws_session_token: payload.aws_session_token,
      }),
    });
    if (!res.ok) {
      const msg = await res.text();
      setAwsPollyStatus('Validation failed', false);
      if (typeof toast === 'function') toast('AWS Polly validation failed', 'error');
      return false;
    }
    const data = await res.json();
    setAwsPollyStatus(`Validated (${data.region || payload.aws_region})`, true);
    if (typeof toast === 'function') toast('AWS Polly validated');
    return true;
  }

  async function saveThirdPartySettings() {
    const status = thirdPartyStatusEl();
    const awsPayload = {
      aws_access_key_id: getValue('thirdPartyAwsAccessKeyId'),
      aws_secret_access_key: getValue('thirdPartyAwsSecretAccessKey'),
      aws_region: getValue('thirdPartyAwsRegion') || 'eu-west-1',
      aws_session_token: getValue('thirdPartyAwsSessionToken'),
    };
    let awsPollyEnabled = false;
    if (awsPayload.aws_access_key_id && awsPayload.aws_secret_access_key) {
      awsPollyEnabled = await validateAwsPolly();
    } else {
      setAwsPollyStatus('Missing credentials (disabled)', false);
    }
    const payload = {
      assemblyai_api_key: getValue('thirdPartyAssemblyAiApiKey'),
      openai_api_key: getValue('thirdPartyOpenAiApiKey'),
      anthropic_api_key: getValue('thirdPartyAnthropicApiKey'),
      google_gemini_api_key: getValue('thirdPartyGoogleGeminiApiKey'),
      elevenlabs_api_key: getValue('thirdPartyElevenLabsApiKey'),
      aws_access_key_id: awsPayload.aws_access_key_id,
      aws_secret_access_key: awsPayload.aws_secret_access_key,
      aws_region: awsPayload.aws_region,
      aws_session_token: awsPayload.aws_session_token,
      aws_polly_enabled: awsPollyEnabled ? '1' : '0',
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
  window.validateVendorKey = validateVendorKey;
  window.validateAwsPolly = validateAwsPolly;
  window.initThirdPartySection = initThirdPartySection;
})();
