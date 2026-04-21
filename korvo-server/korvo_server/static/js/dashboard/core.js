    // ─── Toast ───
    function toast(msg, type = 'success') {
      const el = document.getElementById('toast');
      el.textContent = msg;
      el.className = `toast ${type} show`;
      setTimeout(() => el.classList.remove('show'), 2500);
    }

    // ─── Scan Networks ───
    async function scanNetworks() {
      const btn = document.getElementById('scanBtn');
      const badge = document.getElementById('wifiBadge');
      btn.disabled = true; btn.textContent = 'Scanning…';
      badge.className = 'badge scanning'; badge.textContent = 'Scanning';

      try {
        const res = await fetch('/api/networks?source=mac');
        const data = await res.json();
        const list = document.getElementById('networkList');
        document.getElementById('scanResults').style.display = 'block';
        list.innerHTML = '';

        if (data.networks.length === 0) {
          list.innerHTML = `
            <li style="padding:1rem;color:#888;font-size:0.85rem">
              <div style="margin-bottom:0.75rem">⚠️ macOS hides WiFi SSIDs for privacy. To enable scanning:</div>
              <div style="background:#0d0d0d;border:1px solid #1a1a1a;border-radius:8px;padding:0.75rem;margin-bottom:0.75rem;font-size:0.8rem;line-height:1.6">
                <div style="color:#e74c3c;font-weight:600;margin-bottom:0.4rem">Enable Location Services for WiFi scan:</div>
                1. Open <strong>System Settings → Privacy & Security → Location Services</strong><br>
                2. Turn on Location Services<br>
                3. Find <strong>KorvoWiFiScanner</strong> or <strong>Terminal</strong> in the list<br>
                4. Check the box to allow location access<br>
                5. Click Scan again
              </div>
              <div style="color:#4caf50;font-weight:600;margin-bottom:0.5rem">Or just type your WiFi name below ↓</div>
              <div style="font-size:0.8rem;color:#666">Click the SSID field — saved networks appear as suggestions</div>
            </li>`;
        } else {
          list.innerHTML = '<div style="padding:0.5rem;font-size:0.75rem;color:#4caf50">Found ' + data.networks.length + ' networks nearby</div>';
          data.networks.forEach(n => {
            const strength = n.rssi > -50 ? 'strong' : n.rssi > -70 ? 'medium' : 'weak';
            const li = document.createElement('li');
            li.className = 'network-item';
            li.innerHTML = `
              <span class="signal ${strength}">${n.rssi}</span>
              <span class="ssid">${n.ssid}</span>
              <span class="security">${n.security || ''}</span>
            `;
            li.onclick = () => {
              document.getElementById('ssid').value = n.ssid;
              document.getElementById('password').value = '';
              document.getElementById('password').focus();
            };
            list.appendChild(li);
          });
        }
      } catch (e) { toast('Scan failed', 'error'); }

      btn.disabled = false; btn.textContent = 'Scan';
      loadWifi();
    }

    // ─── Save WiFi ───
    function toggleWifiPasswordVisibility() {
      const pwd = document.getElementById('password');
      const btn = document.getElementById('wifiPasswordToggleBtn');
      if (!pwd || !btn) return;
      const show = pwd.type === 'password';
      pwd.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'Hide WiFi password' : 'Show WiFi password');
    }

    async function saveWifi() {
      const ssid = document.getElementById('ssid').value.trim();
      const password = document.getElementById('password').value;
      if (!ssid) return toast('Enter an SSID', 'error');

      const res = await fetch('/api/wifi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ssid, password, set_active: true }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.detail || data.message || `Save failed (${res.status})`, 'error');
        return;
      }
      if (data.success) { toast('WiFi config saved!'); loadWifi(); }
      else toast('Save failed', 'error');
    }

    // ─── Load WiFi ───
    async function loadWifi() {
      const res = await fetch('/api/wifi');
      const data = await res.json();
      const badge = document.getElementById('wifiBadge');

      if (data.active) {
        document.getElementById('ssid').value = data.active.ssid;
        document.getElementById('password').value = '';
        document.getElementById('password').placeholder = '•••••••• (saved)';
        badge.className = 'badge connected';
        badge.textContent = data.active.ssid;
      } else {
        badge.className = 'badge disconnected';
        badge.textContent = 'Not configured';
      }

      const savedDiv = document.getElementById('savedNetworks');
      const savedList = document.getElementById('savedList');
      // Also populate the datalist dropdown
      const ssidList = document.getElementById('ssidList');
      ssidList.innerHTML = '';

      if (data.networks.length > 0) {
        savedDiv.style.display = 'block';
        savedList.innerHTML = '';
        data.networks.forEach(n => {
          // Add to datalist
          const opt = document.createElement('option');
          opt.value = n.ssid;
          ssidList.appendChild(opt);

          // Add to saved list
          const div = document.createElement('div');
          div.className = `saved-item ${n.is_active ? 'active' : ''}`;
          div.innerHTML = `
            <span class="ssid">${n.ssid}</span>
            <span class="status">${n.is_active ? '● Active' : ''}</span>
            ${n.is_active ? '' : `<button class="btn-danger" onclick="activateNetwork(${n.id})">Set Active</button>`}
            <button class="btn-danger" onclick="deleteNetwork(${n.id})">✕</button>
          `;
          savedList.appendChild(div);
        });
      } else {
        savedDiv.style.display = 'none';
      }
    }

    async function activateNetwork(id) {
      await fetch(`/api/wifi/${id}/activate`, { method: 'POST' });
      toast('Network activated'); loadWifi();
    }

    async function deleteNetwork(id) {
      await fetch(`/api/wifi/${id}`, { method: 'DELETE' });
      toast('Network removed'); loadWifi();
    }

    // ─── Settings ───
    async function loadSettings() {
      const res = await fetch('/api/settings');
      const data = await res.json();
      if (data.wake_word) document.getElementById('wakeWord').value = data.wake_word;
      if (data.server_port) document.getElementById('serverPort').value = data.server_port;
      if (data.agent_endpoint) document.getElementById('agentEndpoint').value = data.agent_endpoint;
      if (data.led_brightness) document.getElementById('settingsLedBrightness').value = data.led_brightness;
      if (data.volume) document.getElementById('volume').value = data.volume;
      if (typeof data.assemblyai_api_key === 'string') {
        const aaiEl = document.getElementById('assemblyAiApiKey');
        if (aaiEl) aaiEl.value = data.assemblyai_api_key;
        const thirdAaiEl = document.getElementById('thirdPartyAssemblyAiApiKey');
        if (thirdAaiEl) thirdAaiEl.value = data.assemblyai_api_key;
      }
      if (typeof data.openai_api_key === 'string') {
        const openaiEl = document.getElementById('thirdPartyOpenAiApiKey');
        if (openaiEl) openaiEl.value = data.openai_api_key;
      }
      if (typeof data.anthropic_api_key === 'string') {
        const anthropicEl = document.getElementById('thirdPartyAnthropicApiKey');
        if (anthropicEl) anthropicEl.value = data.anthropic_api_key;
      }
      if (typeof data.google_gemini_api_key === 'string') {
        const geminiEl = document.getElementById('thirdPartyGoogleGeminiApiKey');
        if (geminiEl) geminiEl.value = data.google_gemini_api_key;
      }
      if (typeof data.elevenlabs_api_key === 'string') {
        const elevenlabsEl = document.getElementById('thirdPartyElevenLabsApiKey');
        if (elevenlabsEl) elevenlabsEl.value = data.elevenlabs_api_key;
      }
      if (typeof data.aws_access_key_id === 'string') {
        const el = document.getElementById('thirdPartyAwsAccessKeyId');
        if (el) el.value = data.aws_access_key_id;
      }
      if (typeof data.aws_secret_access_key === 'string') {
        const el = document.getElementById('thirdPartyAwsSecretAccessKey');
        if (el) el.value = data.aws_secret_access_key;
      }
      if (typeof data.aws_region === 'string') {
        const el = document.getElementById('thirdPartyAwsRegion');
        if (el) el.value = data.aws_region;
      }
      if (typeof data.aws_session_token === 'string') {
        const el = document.getElementById('thirdPartyAwsSessionToken');
        if (el) el.value = data.aws_session_token;
      }
      const awsStatusEl = document.getElementById('thirdPartyAwsPollyStatus');
      if (awsStatusEl && typeof data.aws_polly_enabled === 'string') {
        const on = data.aws_polly_enabled === '1';
        awsStatusEl.textContent = on ? 'Enabled' : 'Disabled';
        awsStatusEl.style.color = on ? '#4caf50' : '#ff9800';
      }
      let host = (data.esp_ip || '').trim();
      const cachedEspHost = (localStorage.getItem('korvo_esp_ip') || '').trim();
      const cachedBoardHost = (localStorage.getItem('korvo_board_ip') || '').trim();
      if (!host) host = cachedEspHost;
      if (!host) host = cachedBoardHost;
      if (!host) host = (document.getElementById('espIpInput').value || 'korvo.local').trim();
      host = _normalizeBoardHost(host);
      if (host.toLowerCase().endsWith('.local') && _isIpv4Host(cachedBoardHost)) {
        host = cachedBoardHost;
      }
      document.getElementById('espIpInput').value = host;
      espIp = host;
      localStorage.setItem('korvo_esp_ip', host);
    }

    async function saveSettings() {
      const espHost = document.getElementById('espIpInput').value.trim();
      if (espHost) {
        espIp = espHost;
        localStorage.setItem('korvo_esp_ip', espHost);
      }
      const settings = {
        wake_word: document.getElementById('wakeWord').value,
        server_port: document.getElementById('serverPort').value,
        agent_endpoint: document.getElementById('agentEndpoint').value,
        led_brightness: document.getElementById('settingsLedBrightness').value,
        volume: document.getElementById('volume').value,
      };
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      toast('Settings saved!');
    }

    // ─── Serial Ports ───
    async function loadPorts() {
      const res = await fetch('/api/ports');
      const data = await res.json();
      const select = document.getElementById('serialPort');
      select.innerHTML = '';
      if (data.ports.length === 0) {
        const opt = document.createElement('option');
        opt.textContent = 'No USB serial ports found — is the board plugged in?';
        select.appendChild(opt);
      } else {
        data.ports.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p; opt.textContent = p;
          select.appendChild(opt);
        });
        const saved = (localStorage.getItem('korvo_flash_port') || '').trim();
        let pick = 0;
        if (saved) {
          const i = data.ports.indexOf(saved);
          if (i >= 0) pick = i;
        }
        select.selectedIndex = pick;
        select.onchange = function() {
          if (this.value && !this.value.includes('No USB')) localStorage.setItem('korvo_flash_port', this.value);
        };
      }
    }

    // ─── Build & Flash (SSE streaming) ───
    let bootCountdownInterval = null;

    /**
     * Single centered button shows build/flash status (replaces duplicate header badge).
     * phase: idle | building | flashing | writing | done | failed | incomplete
     */
    function setFlashActionButton(phase, html, opts) {
      const btn = document.getElementById('flashBtn');
      if (!btn) return;
      const mod = {
        idle: [],
        building: ['flash-action-btn--building'],
        flashing: ['flash-action-btn--flashing'],
        writing: ['flash-action-btn--writing'],
        done: ['flash-action-btn--done'],
        failed: ['flash-action-btn--failed'],
        incomplete: ['flash-action-btn--incomplete'],
      };
      const extra = mod[phase] || [];
      btn.className = ['btn-primary', 'flash-action-btn'].concat(extra).join(' ');
      btn.innerHTML = html;
      if (opts && typeof opts.disabled === 'boolean') {
        btn.disabled = opts.disabled;
      }
    }

    async function buildAndFlash() {
      const btn = document.getElementById('flashBtn');
      const terminal = document.getElementById('terminal');
      const output = document.getElementById('terminalOutput');
      const port = document.getElementById('serialPort').value;

      if (!port || port.includes('No USB')) return toast('No serial port selected', 'error');

      let flashSuccessDone = false;
      let flashExplicitFailed = false;

      setFlashActionButton('building', '<span class="spinner"></span> Starting…', { disabled: true });
      terminal.classList.add('active');
      output.innerHTML = '';

      function termLine(text, cls = 'line-stdout') {
        const div = document.createElement('div');
        div.className = cls;
        div.textContent = text;
        output.appendChild(div);
        output.scrollTop = output.scrollHeight;
      }

      termLine(`Flashing this port: ${port}`, 'line-system');

      function showBootModal(countdown) {
        const modal = document.getElementById('bootModal');
        const timer = document.getElementById('bootTimer');
        modal.classList.add('active');
        let remaining = countdown;
        timer.textContent = remaining;
        timer.classList.remove('urgent');

        clearInterval(bootCountdownInterval);
        bootCountdownInterval = setInterval(() => {
          remaining--;
          timer.textContent = remaining;
          if (remaining <= 3) timer.classList.add('urgent');
          if (remaining <= 0) {
            clearInterval(bootCountdownInterval);
            // Keep modal open until success or timeout
          }
        }, 1000);
      }

      function hideBootModal(success = false) {
        clearInterval(bootCountdownInterval);
        const modal = document.getElementById('bootModal');
        const content = document.getElementById('bootModalContent');
        const timer = document.getElementById('bootTimer');

        if (success) {
          content.classList.add('success');
          content.querySelector('h3').textContent = 'Board connected';
          timer.textContent = 'OK';
          setTimeout(() => {
            modal.classList.remove('active');
            content.classList.remove('success');
            content.querySelector('h3').textContent = 'Enter download mode';
          }, 2000);
        } else {
          modal.classList.remove('active');
        }
      }

      try {
        const cleanBefore = document.getElementById('flashCleanBefore').checked;
        const res = await fetch('/api/flash', {
          method: 'POST',
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify({ port, clean_before_build: cleanBefore }),
        });

        if (!res.ok) {
          const errText = await res.text();
          termLine(`HTTP ${res.status}: ${errText || res.statusText}`, 'line-error');
          toast('Build & flash refused or failed — see terminal output', 'error');
          flashExplicitFailed = true;
          setFlashActionButton('failed', 'Request failed — see log', { disabled: false });
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // Parse SSE
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';

          for (const part of parts) {
            let eventType = 'message';
            let data = '';

            for (const line of part.split('\n')) {
              if (line.startsWith('event: ')) eventType = line.slice(7).trim();
              else if (line.startsWith('data: ')) data = line.slice(6);
            }

            if (!data) continue;

            try {
              const parsed = JSON.parse(data);

              switch (eventType) {
                case 'phase': {
                  const label = typeof parsed === 'string' ? parsed : (parsed && parsed.message) || JSON.stringify(parsed);
                  setFlashActionButton('building', '<span class="spinner"></span> ' + label, { disabled: true });
                  termLine(`\n▸ ${label}`, 'line-phase');
                  break;
                }

                case 'output': {
                  const t = parsed && parsed.text != null ? String(parsed.text) : JSON.stringify(parsed);
                  termLine(t,
                    parsed.source === 'stderr' ? 'line-stderr' :
                    parsed.source === 'system' ? 'line-system' :
                    parsed.source === 'error' ? 'line-error' : 'line-stdout');
                  break;
                }

                case 'flash_start':
                  setFlashActionButton('flashing', '<span class="spinner"></span> Flashing…', { disabled: true });
                  break;

                case 'boot_mode':
                  showBootModal(parsed.countdown || 8);
                  break;

                case 'boot_success':
                  hideBootModal(true);
                  setFlashActionButton('writing', '<span class="spinner"></span> Writing…', { disabled: true });
                  break;

                case 'flash_progress':
                  break;

                case 'flash_done':
                  termLine('Board resetting...', 'line-system');
                  break;

                case 'complete':
                  if (parsed.success) {
                    flashSuccessDone = true;
                    setFlashActionButton('done', 'Done — firmware flashed', { disabled: false });
                    toast('Firmware built & flashed!');
                    setTimeout(() => {
                      setFlashActionButton('idle', 'Build & Flash to Board', { disabled: false });
                    }, 3200);
                  } else {
                    flashExplicitFailed = true;
                    setFlashActionButton('failed', 'Flash failed — see log', { disabled: false });
                    toast('Flash failed', 'error');
                    hideBootModal(false);
                  }
                  break;
              }
            } catch (e) {
              // Not parseable JSON
            }
          }
        }

        // Stream ended without a successful `complete` event (do not clobber an explicit failure)
        if (!flashSuccessDone && !flashExplicitFailed) {
          setFlashActionButton('incomplete', 'Finished (no success confirm)', { disabled: false });
        }

      } catch (e) {
        termLine(`Error: ${e.message}`, 'line-error');
        flashExplicitFailed = true;
        setFlashActionButton('failed', 'Error — see log', { disabled: false });
        toast('Flash failed', 'error');
      }
    }

    async function copyBuildTerminalOutput() {
      const output = document.getElementById('terminalOutput');
      if (!output) return;
      const text = String(output.textContent || '').trim();
      if (!text) {
        toast('No terminal output to copy', 'error');
        return;
      }
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const range = document.createRange();
          range.selectNodeContents(output);
          const sel = window.getSelection();
          if (sel) {
            sel.removeAllRanges();
            sel.addRange(range);
            document.execCommand('copy');
            sel.removeAllRanges();
          }
        }
        toast('Build output copied');
      } catch (_) {
        toast('Copy failed', 'error');
      }
    }

    // ─── LED Control ───
    let espIp = _normalizeBoardHost(localStorage.getItem('korvo_esp_ip') || localStorage.getItem('korvo_board_ip') || '');
    let ledOnline = false;
    let ledPollInterval = null;
    /** Last time GET/POST to the board LED API succeeded (avoids false "offline" when mDNS is slow). */
    let ledBoardLastOkAt = 0;

    function _ledPollTimeoutMs() {
      const h = String(espIp || '').toLowerCase();
      return h.endsWith('.local') ? 15000 : 10000;
    }

    function markLedBoardReachable() {
      ledBoardLastOkAt = Date.now();
      ledOnline = true;
      const badge = document.getElementById('ledBadge');
      if (badge) {
        badge.className = 'badge connected';
        badge.textContent = espIp;
      }
    }
    const KORVO_LED_COUNT_DEFAULT = 12;
    let korvoLedCount = KORVO_LED_COUNT_DEFAULT;
    let korvoStripPixels = Array.from({ length: KORVO_LED_COUNT_DEFAULT }, () => [0, 0, 0]);
    const LED_DEFAULT_BRIGHTNESS = 12;
    let lastLedState = { on: false, preset: 0, r: 0, g: 0, b: 0, brightness: LED_DEFAULT_BRIGHTNESS };
    /** Setting color/brightness from code must not fire change handlers (duplicate sendLed vs ESP32). */
    let ledSuppressProgrammaticColor = false;
    let ledSuppressProgrammaticBrightness = false;
    /** ESP-IDF httpd is effectively single-flight for a client; serialize GET + POST to /api/led so poll never races controls. */
    let ledApiChain = Promise.resolve();

    function enqueueLedApi(fn) {
      const p = ledApiChain.then(() => fn());
      ledApiChain = p.catch(() => {});
      return p;
    }

    function ledRgbClose(a, b, c, ar, ag, ab) {
      return Math.abs(a - ar) <= 8 && Math.abs(b - ag) <= 8 && Math.abs(c - ab) <= 8;
    }

    function syncPixelsFromState(state) {
      const n = typeof state.led_count === 'number' && state.led_count > 0 ? state.led_count : KORVO_LED_COUNT_DEFAULT;
      korvoLedCount = n;
      const preset = Number(state.preset) || 0;
      const on = !!state.on;
      if (Array.isArray(state.pixels) && state.pixels.length === n) {
        korvoStripPixels = state.pixels.map((row) => {
          if (Array.isArray(row) && row.length >= 3) {
            return [Math.round(Number(row[0])), Math.round(Number(row[1])), Math.round(Number(row[2]))];
          }
          return [0, 0, 0];
        });
      }
      /* Firmware used to leave pix[] at zero while preset 1 drew from r,g,b — avoid treating that as “all LEDs off”. */
      if (on && preset === 1) {
        const r = Number(state.r) || 0, g = Number(state.g) || 0, b = Number(state.b) || 0;
        korvoStripPixels = Array.from({ length: n }, () => [r, g, b]);
      } else if (!on || preset === 0) {
        korvoStripPixels = Array.from({ length: n }, () => [0, 0, 0]);
      } else if (on && preset >= 2 && preset <= 15 && preset !== 6) {
        const r = Number(state.r) || 0, g = Number(state.g) || 0, b = Number(state.b) || 0;
        korvoStripPixels = Array.from({ length: n }, () => [r, g, b]);
      }
    }

    function renderLedStripPreview() {
      const wrap = document.getElementById('ledRingPreview');
      if (!wrap) return;
      wrap.style.setProperty('--led-n', String(Math.max(1, korvoLedCount)));
      if (wrap.children.length !== korvoLedCount) {
        wrap.innerHTML = '';
        for (let i = 0; i < korvoLedCount; i++) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'led-ring-dot';
          btn.style.setProperty('--i', String(i));
          btn.setAttribute('aria-label', 'LED ' + i + ' — tap to toggle (off or paint from picker)');
          btn.addEventListener('click', () => {
            ledRingPixelClick(i);
          });
          wrap.appendChild(btn);
        }
      }
      for (let i = 0; i < korvoLedCount; i++) {
        const row = korvoStripPixels[i] || [0, 0, 0];
        const el = wrap.children[i];
        const lit = row[0] + row[1] + row[2] > 0;
        el.style.background = 'rgb(' + row[0] + ',' + row[1] + ',' + row[2] + ')';
        el.style.boxShadow = lit ? '0 0 8px rgba(255,255,255,0.25)' : 'none';
        el.classList.toggle('led-ring-dot--off', !lit);
      }
    }

    /* Render an initial ring immediately so dots remain visible before first board poll. */
    renderLedStripPreview();

    const LED_NAMED_SWATCHES = [
      ['ledTogRed', 255, 0, 0],
      ['ledTogGreen', 0, 255, 0],
      ['ledTogBlue', 0, 100, 255],
      ['ledTogCyan', 0, 255, 255],
      ['ledTogMagenta', 255, 0, 255],
      ['ledTogOrange', 255, 150, 0],
      ['ledTogYellow', 255, 220, 0],
      ['ledTogPink', 255, 105, 180],
      ['ledTogPurple', 128, 0, 255],
      ['ledTogLime', 180, 255, 0],
      ['ledTogTeal', 0, 128, 128],
      ['ledTogCoral', 255, 127, 80],
      ['ledTogWhite', 255, 255, 255],
      ['ledTogWarm', 255, 200, 120],
    ];

    function updateNamedColorToggleStyles() {
      for (let j = 0; j < LED_NAMED_SWATCHES.length; j++) {
        const id = LED_NAMED_SWATCHES[j][0];
        const rr = LED_NAMED_SWATCHES[j][1], gg = LED_NAMED_SWATCHES[j][2], bb = LED_NAMED_SWATCHES[j][3];
        const el = document.getElementById(id);
        if (!el) continue;
        const on = lastLedState.on && lastLedState.preset === 1 && ledRgbClose(lastLedState.r, lastLedState.g, lastLedState.b, rr, gg, bb);
        el.classList.toggle('active', on);
      }
    }

    function rgbToHexByte(v) {
      const n = Math.max(0, Math.min(255, Math.round(Number(v))));
      return n.toString(16).padStart(2, '0');
    }

    function rgbToHex(r, g, b) {
      return '#' + rgbToHexByte(r) + rgbToHexByte(g) + rgbToHexByte(b);
    }

    function setLedColorInputValue(hex) {
      const cp = document.getElementById('ledColor');
      if (!cp) return;
      ledSuppressProgrammaticColor = true;
      try {
        cp.value = hex;
        updateLedCustomPickerButton(hex);
      } finally {
        /* Defer clear so any browser that dispatches change async still sees suppress. */
        setTimeout(() => {
          ledSuppressProgrammaticColor = false;
        }, 0);
      }
    }

    function updateLedCustomPickerButton(hex) {
      const btn = document.getElementById('ledCustomPickerBtn');
      if (!btn) return;
      const h = String(hex || '').trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(h)) return;
      const r = parseInt(h.slice(1, 3), 16);
      const g = parseInt(h.slice(3, 5), 16);
      const b = parseInt(h.slice(5, 7), 16);
      // Perceived luminance for readable label color
      const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      btn.style.background = h;
      btn.style.borderColor = 'rgba(255,255,255,0.18)';
      btn.style.color = lum > 140 ? '#111' : '#f2f2f2';
      const slider = document.getElementById('ledBrightness');
      if (slider) {
        slider.style.accentColor = h;
      }
    }

    function setLedBrightnessInputValue(num) {
      const el = document.getElementById('ledBrightness');
      const val = document.getElementById('brightnessVal');
      if (!el) return;
      ledSuppressProgrammaticBrightness = true;
      try {
        el.value = String(num);
        if (val) val.textContent = String(num);
      } finally {
        setTimeout(() => {
          ledSuppressProgrammaticBrightness = false;
        }, 0);
      }
    }

    function ledApplyOptimisticSolid(r, g, b, br) {
      const rr = Math.max(0, Math.min(255, r | 0));
      const gg = Math.max(0, Math.min(255, g | 0));
      const bb = Math.max(0, Math.min(255, b | 0));
      lastLedState = { on: true, preset: 1, r: rr, g: gg, b: bb, brightness: br };
      korvoStripPixels = Array.from({ length: korvoLedCount }, () => [rr, gg, bb]);
      setLedColorInputValue(rgbToHex(rr, gg, bb));
      document.getElementById('ledPowerBtn').textContent = 'ON';
      document.getElementById('ledPowerBtn').style.background = '#1a472a';
      document.getElementById('ledPowerBtn').style.color = '#4caf50';
      renderLedStripPreview();
      updateNamedColorToggleStyles();
    }

    function ledApplyOptimisticOff() {
      const br = parseInt(document.getElementById('ledBrightness').value, 10);
      const b = Number.isFinite(br) ? br : LED_DEFAULT_BRIGHTNESS;
      lastLedState = { on: false, preset: 0, r: 0, g: 0, b: 0, brightness: b };
      korvoStripPixels = Array.from({ length: korvoLedCount }, () => [0, 0, 0]);
      document.getElementById('ledPowerBtn').textContent = 'OFF';
      document.getElementById('ledPowerBtn').style.background = '#222';
      document.getElementById('ledPowerBtn').style.color = '#666';
      renderLedStripPreview();
      updateNamedColorToggleStyles();
    }

    function setEspIp() {
      const ip = prompt('Enter ESP32 IP address:', espIp);
      if (ip) {
        espIp = ip.trim();
        localStorage.setItem('korvo_esp_ip', espIp);
        checkLedBoard();
      }
    }

    async function sendLedInternal(data) {
      if (!espIp) { setEspIp(); return null; }
      const url = 'http://' + espIp + '/api/led';
      const postTimeoutMs = _ledPollTimeoutMs() * 2;
      const postSignal =
        typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
          ? AbortSignal.timeout(postTimeoutMs)
          : undefined;
      let text = '';
      try {
        const res = await fetch(url, {
          method: 'POST',
          mode: 'cors',
          credentials: 'omit',
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
          ...(postSignal ? { signal: postSignal } : {}),
        });
        text = await res.text();
        if (!res.ok) {
          let msg = 'LED request failed (' + res.status + ')';
          try {
            const err = JSON.parse(text);
            if (err.error) msg = err.error;
          } catch (x) { /* ignore */ }
          toast(msg, 'error');
          return null;
        }
      } catch (e) {
        /* Only transport / timeout / browser blocks (e.g. mixed content) — not JSON/UI errors. */
        const name = e && e.name ? String(e.name) : '';
        const detail = e && e.message ? String(e.message) : String(e);
        console.warn('LED POST fetch failed', { url, name, detail });
        document.getElementById('ledBadge').className = 'badge disconnected';
        document.getElementById('ledBadge').textContent = 'Board offline';
        ledOnline = false;
        ledBoardLastOkAt = 0;
        if (name === 'AbortError') {
          toast('LED request timed out — try a numeric LAN IP instead of .local', 'error');
        } else if (/mixed content|insecure|https|ssl|certificate/i.test(detail)) {
          toast('LED blocked by browser (HTTPS page cannot call HTTP device — open dashboard over HTTP or use HTTPS on the board)', 'error');
        } else {
          toast('LED request failed (network)', 'error');
        }
        return null;
      }
      let state;
      try {
        state = JSON.parse(text);
      } catch (e) {
        console.error('LED POST response was not JSON', text ? text.slice(0, 400) : '(empty)', e);
        toast('LED: invalid JSON from board — check console', 'error');
        return null;
      }
      try {
        updateLedUI(state);
      } catch (e) {
        console.error('LED UI update threw', e, state);
        markLedBoardReachable();
        toast('LED board responded but the dashboard failed to refresh (see console)', 'error');
        return state;
      }
      markLedBoardReachable();
      return state;
    }

    function sendLed(data) {
      return enqueueLedApi(() => sendLedInternal(data));
    }

    function checkLedBoard() {
      if (!espIp) return;
      return enqueueLedApi(async () => {
        try {
          const res = await fetch(`http://${espIp}/api/led`, {
            cache: 'no-store',
            signal: AbortSignal.timeout(_ledPollTimeoutMs()),
          });
          if (!res.ok) {
            throw new Error('bad status ' + res.status);
          }
          const state = await res.json();
          updateLedUI(state);
          markLedBoardReachable();
        } catch (e) {
          /* Slow mDNS or one dropped poll should not override a recent successful control request. */
          if (Date.now() - ledBoardLastOkAt < 45000) {
            return;
          }
          ledOnline = false;
          document.getElementById('ledBadge').className = 'badge disconnected';
          document.getElementById('ledBadge').textContent = 'Board offline';
        }
      });
    }

    function updateLedUI(state) {
      lastLedState = {
        on: !!state.on,
        preset: Number(state.preset) || 0,
        r: Number(state.r) || 0,
        g: Number(state.g) || 0,
        b: Number(state.b) || 0,
        brightness: Number(state.brightness) || 0,
      };
      syncPixelsFromState(state);
      document.getElementById('ledPowerBtn').textContent = state.on ? 'ON' : 'OFF';
      document.getElementById('ledPowerBtn').style.background = state.on ? '#1a472a' : '#222';
      document.getElementById('ledPowerBtn').style.color = state.on ? '#4caf50' : '#666';
      setLedBrightnessInputValue(state.brightness);
      /* Preset 6 = per-pixel: do not touch the color picker (polls should not clobber it). */
      if (lastLedState.preset !== 6) {
        const hex = '#' + [state.r, state.g, state.b].map((v) => Number(v).toString(16).padStart(2, '0')).join('');
        setLedColorInputValue(hex);
      }
      renderLedStripPreview();
      updateNamedColorToggleStyles();
    }

    async function toggleLed() {
      const isOn = document.getElementById('ledPowerBtn').textContent === 'ON';
      await sendLed({ on: !isOn, preset: !isOn ? 1 : 0 });
    }

    async function ledPreset(preset) {
      const hex = document.getElementById('ledColor').value;
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      const br = parseInt(document.getElementById('ledBrightness').value, 10);
      const brightness = Number.isFinite(br) ? br : LED_DEFAULT_BRIGHTNESS;
      /* Pulse/fade use per-pixel palette when the ring has been painted; otherwise firmware fills from r,g,b. */
      if (preset === 10 || preset === 11) {
        const px = [];
        let hasPaint = false;
        for (let i = 0; i < korvoLedCount; i++) {
          const row = (korvoStripPixels[i] || [0, 0, 0]).slice(0, 3);
          px.push(row);
          if ((row[0] || 0) + (row[1] || 0) + (row[2] || 0) > 0) {
            hasPaint = true;
          }
        }
        if (hasPaint) {
          await sendLed({ on: true, preset, pixels: px, r, g, b, brightness });
          return;
        }
      }
      await sendLed({ on: preset > 0, preset, r, g, b, brightness });
    }

    function ledNamedToggle(r, g, b) {
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || LED_DEFAULT_BRIGHTNESS;
      if (lastLedState.on && lastLedState.preset === 1 && ledRgbClose(lastLedState.r, lastLedState.g, lastLedState.b, r, g, b)) {
        ledApplyOptimisticOff();
        sendLed({ on: false, preset: 0 }).catch(() => {});
      } else {
        ledApplyOptimisticSolid(r, g, b, br);
        sendLed({ on: true, preset: 1, r, g, b, brightness: br }).catch(() => {});
      }
    }

    function ledHexToRgb(hex) {
      return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
    }

    async function ledApplyPickerToIndex(idx) {
      if (typeof idx !== 'number' || idx < 0 || idx >= korvoLedCount) return;
      const hex = document.getElementById('ledColor').value;
      const rgb = ledHexToRgb(hex);
      korvoStripPixels[idx] = rgb.slice();
      renderLedStripPreview();
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || LED_DEFAULT_BRIGHTNESS;
      await sendLed({ pixel: { i: idx, r: rgb[0], g: rgb[1], b: rgb[2] }, preset: 6, on: true, brightness: br });
    }

    async function ledTurnPixelOff(idx) {
      if (typeof idx !== 'number' || idx < 0 || idx >= korvoLedCount) return;
      korvoStripPixels[idx] = [0, 0, 0];
      renderLedStripPreview();
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || LED_DEFAULT_BRIGHTNESS;
      await sendLed({ pixel: { i: idx, r: 0, g: 0, b: 0 }, preset: 6, on: true, brightness: br });
    }

    async function ledRingPixelClick(idx) {
      if (typeof idx !== 'number' || idx < 0 || idx >= korvoLedCount) return;
      const row = korvoStripPixels[idx] || [0, 0, 0];
      const isLit = (Number(row[0]) || 0) + (Number(row[1]) || 0) + (Number(row[2]) || 0) > 0;
      if (isLit) {
        await ledTurnPixelOff(idx);
        return;
      }
      await ledApplyPickerToIndex(idx);
    }

    async function ledPushFullStrip() {
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || LED_DEFAULT_BRIGHTNESS;
      const hex = document.getElementById('ledColor').value;
      const rgb = ledHexToRgb(hex);
      const px = [];
      for (let i = 0; i < korvoLedCount; i++) {
        const row = [rgb[0], rgb[1], rgb[2]];
        korvoStripPixels[i] = row;
        px.push(row);
      }
      /* Make the action visible immediately, then sync to board. */
      lastLedState = { on: true, preset: 6, r: rgb[0], g: rgb[1], b: rgb[2], brightness: br };
      renderLedStripPreview();
      await sendLed({ pixels: px, preset: 6, on: true, r: rgb[0], g: rgb[1], b: rgb[2], brightness: br });
    }

    async function ledRandomizeStrip() {
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || LED_DEFAULT_BRIGHTNESS;
      const px = [];
      for (let i = 0; i < korvoLedCount; i++) {
        const row = [
          Math.floor(Math.random() * 256),
          Math.floor(Math.random() * 256),
          Math.floor(Math.random() * 256),
        ];
        korvoStripPixels[i] = row;
        px.push(row);
      }
      const first = px[0] || [0, 0, 0];
      lastLedState = { on: true, preset: 6, r: first[0], g: first[1], b: first[2], brightness: br };
      renderLedStripPreview();
      await sendLed({ pixels: px, preset: 6, on: true, r: first[0], g: first[1], b: first[2], brightness: br });
    }

    // Event listeners for color/brightness changes
    updateLedCustomPickerButton((document.getElementById('ledColor') && document.getElementById('ledColor').value) || '#0096ff');
    document.getElementById('ledColor').addEventListener('input', (e) => {
      updateLedCustomPickerButton(e.target.value);
    });
    document.getElementById('ledColor').addEventListener('change', async (e) => {
      const hex = e.target.value;
      updateLedCustomPickerButton(hex);
      if (ledSuppressProgrammaticColor) return;
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || LED_DEFAULT_BRIGHTNESS;
      ledApplyOptimisticSolid(r, g, b, br);
      await sendLed({ on: true, preset: 1, r, g, b, brightness: br });
    });

    document.getElementById('ledBrightness').addEventListener('input', (e) => {
      document.getElementById('brightnessVal').textContent = e.target.value;
    });
    document.getElementById('ledBrightness').addEventListener('change', async (e) => {
      if (ledSuppressProgrammaticBrightness) return;
      await sendLed({ brightness: parseInt(e.target.value, 10) });
    });

    let korvoMic = null;
    let transcribeWs = null;
    let transcribeAutoMic = false;

    function _normalizeBoardHost(raw) {
      let v = String(raw || '').trim();
      if (!v) return '';
      v = v.replace(/^https?:\/\//i, '');
      const slash = v.indexOf('/');
      if (slash >= 0) v = v.slice(0, slash);
      return v.trim();
    }

    function _isIpv4Host(host) {
      const v = _normalizeBoardHost(host);
      if (!v || v.includes(':')) return false;
      const parts = v.split('.');
      if (parts.length !== 4) return false;
      for (const p of parts) {
        if (!/^\d+$/.test(p)) return false;
        const n = Number(p);
        if (!Number.isInteger(n) || n < 0 || n > 255) return false;
      }
      return true;
    }

    /** After one mDNS resolution, cache numeric STA IP so LED/audio calls avoid repeated .local delays. */
    function korvoApplyBoardHost(ip) {
      const v = _normalizeBoardHost(ip);
      if (!_isIpv4Host(v)) return false;
      espIp = v;
      const el = document.getElementById('espIpInput');
      if (el) el.value = v;
      const bi = document.getElementById('boardIp');
      if (bi) bi.value = v;
      try {
        localStorage.setItem('korvo_esp_ip', v);
        localStorage.setItem('korvo_board_ip', v);
      } catch (e) {}
      return true;
    }

    async function korvoDiscoverStaIpFromBoard() {
      if (_isIpv4Host(espIp)) return;
      let probeHost = _normalizeBoardHost(espIp);
      if (!probeHost) {
        probeHost = _normalizeBoardHost(document.getElementById('boardIp').value) || 'korvo.local';
      }
      const url = 'http://' + probeHost + '/api/network/status';
      try {
        const res = await fetch(url, { method: 'GET', signal: AbortSignal.timeout(15000) });
        if (!res.ok) return;
        const data = await res.json();
        const sta = data && data.sta_ip != null ? String(data.sta_ip).trim() : '';
        if (_isIpv4Host(sta)) {
          korvoApplyBoardHost(sta);
        }
      } catch (e) {
        /* Offline or mDNS slow — keep hostname */
      }
    }

    function _boardStreamUrl() {
      let boardIp = _normalizeBoardHost(document.getElementById('boardIp').value);
      if (!boardIp && typeof espIp !== 'undefined' && espIp) {
        boardIp = _normalizeBoardHost(espIp);
        document.getElementById('boardIp').value = boardIp;
      }
      if (!boardIp) return '';
      return 'http://' + boardIp + '/api/audio/stream';
    }

    async function toggleLiveTranscribe() {
      const btn = document.getElementById('transcribeBtn');
      const badge = document.getElementById('transcribeBadge');
      const statusEl = document.getElementById('transcribeStatus');
      const live = document.getElementById('transcriptLive');
      const roll = document.getElementById('transcriptRolling');
      if (transcribeWs) {
        transcribeWs.close();
        transcribeWs = null;
        if (transcribeAutoMic && korvoMic) {
          try { await korvoMic.stop(); } catch (e) {}
          korvoMic = null;
        }
        transcribeAutoMic = false;
        btn.innerHTML = '▶️ Start live transcript';
        badge.className = 'badge disconnected';
        badge.textContent = 'Off';
        statusEl.textContent = 'Stopped.';
        return;
      }
      const streamUrl = _boardStreamUrl();
      if (!streamUrl) {
        alert('Enter the board IP / host (same field as live mic).');
        return;
      }
      const listenWhileTranscribing = !!(document.getElementById('transcribeListenAudio') && document.getElementById('transcribeListenAudio').checked);
      if (listenWhileTranscribing && !korvoMic) {
        try {
          await toggleAudioStream();
          transcribeAutoMic = !!korvoMic;
          if (!korvoMic) {
            statusEl.textContent = 'Live audio could not start; continuing with transcript only.';
            toast('Audio playback did not start', 'error');
            transcribeAutoMic = false;
          }
        } catch (e) {
          transcribeAutoMic = false;
          statusEl.textContent = 'Could not start live audio playback; continuing with transcript only.';
          toast('Live audio playback failed', 'error');
        }
      } else {
        transcribeAutoMic = false;
      }
      const model = (document.getElementById('whisperModel').value || 'base.en').trim() || 'base.en';
      const stepSec = String(parseFloat((document.getElementById('transcribeStepSec') && document.getElementById('transcribeStepSec').value) || '1.25') || 1.25);
      const windowSec = String(parseFloat((document.getElementById('transcribeWindowSec') && document.getElementById('transcribeWindowSec').value) || '5') || 5);
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const q = new URLSearchParams({
        board_url: streamUrl,
        model: model,
        step_sec: stepSec,
        window_sec: windowSec,
      });
      const wsUrl = proto + '//' + location.host + '/ws/audio/transcribe?' + q.toString();
      statusEl.textContent = 'Connecting…';
      live.textContent = '…';
      roll.textContent = '';
      window._korvoTranscribeAccum = '';
      let wsGotReady = false;
      transcribeWs = new WebSocket(wsUrl);
      transcribeWs.onopen = () => {
        badge.className = 'badge scanning';
        badge.textContent = 'Connecting';
      };
      transcribeWs.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (_) {
          statusEl.textContent = ev.data;
          return;
        }
        if (msg.type === 'ready') {
          wsGotReady = true;
          badge.className = 'badge connected';
          badge.textContent = 'Live';
          btn.innerHTML = '⏹ Stop transcript';
          statusEl.textContent = 'Ready — model ' + msg.model + ', step ' + msg.step_sec + 's, window ' + msg.window_sec + 's';
          return;
        }
        if (msg.type === 'partial') {
          const full = (msg.text || '').trim();
          const deltaRaw = msg.delta !== undefined && msg.delta !== null ? String(msg.delta) : '';
          const delta = deltaRaw.trim();
          const liveEl = document.getElementById('transcriptLive');
          const rollEl = document.getElementById('transcriptRolling');
          if (delta) {
            window._korvoTranscribeAccum = (window._korvoTranscribeAccum ? window._korvoTranscribeAccum + ' ' : '') + delta;
          } else if (full && !window._korvoTranscribeAccum) {
            window._korvoTranscribeAccum = full;
          }
          liveEl.textContent = window._korvoTranscribeAccum || full || '…';
          if (delta) {
            const ts = msg.t_unix ? new Date(msg.t_unix * 1000).toLocaleTimeString() : new Date().toLocaleTimeString();
            const prev = rollEl.textContent.trim();
            rollEl.textContent = (prev ? prev + '\n' : '') + '[' + ts + '] ' + delta;
            rollEl.scrollTop = rollEl.scrollHeight;
          }
          const hint = delta || full;
          statusEl.textContent = hint
            ? (delta ? 'Δ ' : 'Window ') + hint.slice(0, 80) + (hint.length > 80 ? '…' : '') + ' · ' + (msg.t_unix ? new Date(msg.t_unix * 1000).toLocaleTimeString() : '')
            : '(no new words this step)';
          return;
        }
        if (msg.type === 'error') {
          statusEl.textContent = 'Error: ' + (msg.message || '');
          toast(msg.message || 'Transcribe error', 'error');
          return;
        }
        if (msg.type === 'done') {
          statusEl.textContent = 'Stream ended.';
        }
      };
      transcribeWs.onerror = () => {
        statusEl.textContent = 'WebSocket error (is the server running with Whisper installed?)';
        toast('Transcribe WebSocket error', 'error');
      };
      transcribeWs.onclose = () => {
        transcribeWs = null;
        btn.innerHTML = '▶️ Start live transcript';
        badge.className = 'badge disconnected';
        badge.textContent = 'Off';
        if (!wsGotReady && (!statusEl.textContent || statusEl.textContent === 'Connecting…')) {
          statusEl.textContent = 'Transcript connection closed before ready. Check Whisper install and board stream URL.';
        }
        if (transcribeAutoMic && korvoMic) {
          korvoMic.stop().catch(() => {});
          korvoMic = null;
        }
        transcribeAutoMic = false;
      };
    }

    async function toggleAudioStream() {
      const btn = document.getElementById('audioBtn');
      const badge = document.getElementById('audioBadge');
      const statusEl = document.getElementById('audioStreamStatus');
      if (typeof KorvoLiveMic === 'undefined') {
        alert('Missing /static/mic_stream.js — restart korvo-server.');
        return;
      }
      let boardIp = _normalizeBoardHost(document.getElementById('boardIp').value);
      if (!boardIp && espIp) {
        boardIp = _normalizeBoardHost(espIp);
        document.getElementById('boardIp').value = boardIp;
      }
      if (!boardIp) {
        alert('Enter the board LAN IP (same as LED control), or set it under LED first.');
        return;
      }
      if (korvoMic) {
        await korvoMic.stop();
        korvoMic = null;
        btn.innerHTML = '▶️ Start live mic (16 kHz mono)';
        badge.className = 'badge disconnected';
        badge.textContent = 'Not connected';
        statusEl.textContent = 'Stopped.';
        return;
      }
      const primaryUrl = `http://${boardIp}/api/audio/stream`;
      const useRelay = document.getElementById('audioUseRelay') && document.getElementById('audioUseRelay').checked;
      const playUrl = useRelay
        ? `${window.location.origin}/api/audio/relay?url=${encodeURIComponent(primaryUrl)}`
        : primaryUrl;
      statusEl.textContent = 'Connecting… ' + playUrl;
      korvoMic = new KorvoLiveMic();
      try {
        const playbackGain =
          typeof window.getBoardOutputVolumeLinear === 'function'
            ? window.getBoardOutputVolumeLinear()
            : 1;
        await korvoMic.start(playUrl, {
          playbackGain: playbackGain,
          onStatus: (s) => { statusEl.textContent = s; },
          onEnded: () => {
            korvoMic = null;
            btn.innerHTML = '▶️ Start live mic (16 kHz mono)';
            badge.className = 'badge disconnected';
            badge.textContent = 'Not connected';
          },
        });
        btn.innerHTML = '⏹ Stop live mic';
        badge.className = 'badge connected';
        badge.textContent = 'Streaming';
      } catch (e) {
        korvoMic = null;
        btn.innerHTML = '▶️ Start live mic (16 kHz mono)';
        badge.className = 'badge disconnected';
        badge.textContent = 'Start failed';
        statusEl.textContent = String(e && e.message ? e.message : e);
        toast('Mic stream failed', 'error');
      }
    }

    window.addEventListener('beforeunload', () => {
      if (korvoMic) korvoMic.stop();
      if (transcribeWs) {
        transcribeWs.close();
        transcribeWs = null;
      }
    });
    
    // Agent API interaction functions
    let agentWs = null;
    
    function sendToAgent(text) {
      // This would send text to the ESP32 agent or to a cloud LLM API
      return fetch('/api/agent/openrouter', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          transcript: text,
          model: 'qwen/qwen3.6-plus',
          apiKey: localStorage.getItem('openrouter_api_key') || ''
        })
      });
    }
    
    function setApiKey() {
      const key = prompt('Enter your OpenRouter API key:', localStorage.getItem('openrouter_api_key') || '');
      if (key) {
        localStorage.setItem('openrouter_api_key', key);
        alert('API key saved!');
      }
    }
    
    // Poll ESP32 LED state every 5s when online
    setInterval(() => { if (espIp) checkLedBoard(); }, 5000);

    document.getElementById('espIpInput').addEventListener('change', function() {
      const v = _normalizeBoardHost(this.value);
      if (!v) return;
      this.value = v;
      espIp = v;
      localStorage.setItem('korvo_esp_ip', v);
      checkLedBoard();
    });

    async function bootstrapAfterSettings() {
      await loadSettings();
      const bi = document.getElementById('boardIp');
      if (!bi.dataset.bound) {
        bi.dataset.bound = '1';
        bi.addEventListener('change', function() {
          localStorage.setItem('korvo_board_ip', this.value.trim());
        });
      }
      const savedBoard = _normalizeBoardHost(localStorage.getItem('korvo_board_ip') || '');
      if (espIp) bi.value = espIp;
      else if (savedBoard) bi.value = savedBoard;
      else bi.value = 'korvo.local';
      await korvoDiscoverStaIpFromBoard();
      if (espIp) checkLedBoard();
    }

    function korvoSwitchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach((b) => {
        const on = b.getAttribute('data-tab') === tabId;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      document.querySelectorAll('.tab-panel').forEach((p) => {
        p.classList.toggle('active', p.id === 'tab-' + tabId);
      });
      try {
        localStorage.setItem('korvo_dashboard_tab', tabId);
      } catch (e) {}
    }

    function korvoInitTabs() {
      document.querySelectorAll('.tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => korvoSwitchTab(btn.getAttribute('data-tab')));
      });
      const valid = ['wifi', 'bluetooth', 'settings', 'led', 'build', 'audio', 'transcript', 'translation', 'third-party', 'docs'];
      let t = '';
      try {
        t = (localStorage.getItem('korvo_dashboard_tab') || '').trim();
      } catch (e) {}
      if (t && valid.indexOf(t) >= 0) {
        korvoSwitchTab(t);
      }
    }

    // ─── Init ───
    window.toggleAudioStream = toggleAudioStream;
    window.toggleLiveTranscribe = toggleLiveTranscribe;
    window._boardStreamUrl = _boardStreamUrl;
    window.__korvoApplyLiveMicGain = function () {
      if (!korvoMic || typeof korvoMic.setPlaybackGain !== 'function') return;
      const g =
        typeof window.getBoardOutputVolumeLinear === 'function'
          ? window.getBoardOutputVolumeLinear()
          : 1;
      korvoMic.setPlaybackGain(g);
    };

    korvoInitTabs();
    loadWifi();
    bootstrapAfterSettings();
    loadPorts();
    if (typeof initAudioSection === 'function') initAudioSection();
    if (typeof initBluetoothSection === 'function') initBluetoothSection();
    if (typeof initTranslationSection === 'function') initTranslationSection();
    if (typeof initThirdPartySection === 'function') initThirdPartySection();
  
