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
      let host = (data.esp_ip || '').trim();
      if (!host) host = (localStorage.getItem('korvo_esp_ip') || '').trim();
      if (!host) host = (document.getElementById('espIpInput').value || 'korvo.local').trim();
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

    async function buildAndFlash() {
      const btn = document.getElementById('flashBtn');
      const badge = document.getElementById('flashBadge');
      const terminal = document.getElementById('terminal');
      const output = document.getElementById('terminalOutput');
      const port = document.getElementById('serialPort').value;

      if (!port || port.includes('No USB')) return toast('No serial port selected', 'error');

      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Working...';
      badge.style.display = 'inline-block';
      badge.className = 'badge building'; badge.textContent = 'Building';
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
          content.querySelector('h3').textContent = '✅ Board Connected!';
          timer.textContent = '✓';
          setTimeout(() => {
            modal.classList.remove('active');
            content.classList.remove('success');
            content.querySelector('h3').textContent = '🔄 Enter Download Mode';
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
          badge.className = 'badge disconnected';
          badge.textContent = 'Failed';
          btn.disabled = false;
          btn.innerHTML = '🔨 Build & Flash to Board';
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let currentEvent = '';

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
                  badge.textContent = label;
                  btn.innerHTML = `<span class="spinner"></span> ${label}`;
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
                  badge.className = 'badge flashing';
                  badge.textContent = 'Flashing';
                  btn.innerHTML = '<span class="spinner"></span> Flashing...';
                  break;

                case 'boot_mode':
                  showBootModal(parsed.countdown || 8);
                  break;

                case 'boot_success':
                  hideBootModal(true);
                  badge.textContent = 'Writing...';
                  break;

                case 'flash_progress':
                  // Don't spam terminal with every write, but update badge
                  break;

                case 'flash_done':
                  termLine('Board resetting...', 'line-system');
                  break;

                case 'complete':
                  if (parsed.success) {
                    badge.className = 'badge done'; badge.textContent = 'Done ✓';
                    btn.innerHTML = '🔨 Build & Flash to Board';
                    toast('Firmware built & flashed!');
                  } else {
                    badge.className = 'badge disconnected'; badge.textContent = 'Failed';
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

        // If we get here without a complete event
        if (!badge.classList.contains('done')) {
          badge.className = 'badge disconnected'; badge.textContent = 'Done';
          btn.innerHTML = '🔨 Build & Flash to Board';
        }

      } catch (e) {
        termLine(`Error: ${e.message}`, 'line-error');
        badge.className = 'badge disconnected'; badge.textContent = 'Error';
        toast('Flash failed', 'error');
      }

      btn.disabled = false;
      btn.innerHTML = '🔨 Build & Flash to Board';
    }

    // ─── LED Control ───
    let espIp = localStorage.getItem('korvo_esp_ip') || '';
    let ledOnline = false;
    let ledPollInterval = null;
    const KORVO_LED_COUNT_DEFAULT = 12;
    let korvoLedCount = KORVO_LED_COUNT_DEFAULT;
    let korvoStripPixels = Array.from({ length: KORVO_LED_COUNT_DEFAULT }, () => [0, 0, 0]);
    let lastLedState = { on: false, preset: 0, r: 0, g: 0, b: 0, brightness: 180 };

    function ledInitPixSelect() {
      const sel = document.getElementById('ledPixIndex');
      if (!sel || sel.dataset.ready === '1') return;
      sel.innerHTML = '';
      for (let i = 0; i < KORVO_LED_COUNT_DEFAULT; i++) {
        const o = document.createElement('option');
        o.value = String(i);
        o.textContent = 'LED ' + i;
        sel.appendChild(o);
      }
      sel.dataset.ready = '1';
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
      } else if (on && preset >= 2 && preset <= 5) {
        const r = Number(state.r) || 0, g = Number(state.g) || 0, b = Number(state.b) || 0;
        korvoStripPixels = Array.from({ length: n }, () => [r, g, b]);
      }
    }

    function renderLedStripPreview() {
      const wrap = document.getElementById('ledStripPreview');
      if (!wrap) return;
      while (wrap.children.length < korvoLedCount) {
        const d = document.createElement('div');
        d.className = 'led-strip-dot';
        wrap.appendChild(d);
      }
      while (wrap.children.length > korvoLedCount) {
        wrap.removeChild(wrap.lastChild);
      }
      for (let i = 0; i < korvoLedCount; i++) {
        const row = korvoStripPixels[i] || [0, 0, 0];
        const el = wrap.children[i];
        el.style.background = 'rgb(' + row[0] + ',' + row[1] + ',' + row[2] + ')';
        el.title = 'LED ' + i;
      }
    }

    function updateNamedColorToggleStyles() {
      const rows = [['ledTogRed', 255, 0, 0], ['ledTogGreen', 0, 255, 0], ['ledTogBlue', 0, 100, 255], ['ledTogOrange', 255, 150, 0], ['ledTogYellow', 255, 220, 0]];
      for (let j = 0; j < rows.length; j++) {
        const id = rows[j][0];
        const rr = rows[j][1], gg = rows[j][2], bb = rows[j][3];
        const el = document.getElementById(id);
        if (!el) continue;
        const on = lastLedState.on && lastLedState.preset === 1 && ledRgbClose(lastLedState.r, lastLedState.g, lastLedState.b, rr, gg, bb);
        el.classList.toggle('active', on);
      }
    }

    function setEspIp() {
      const ip = prompt('Enter ESP32 IP address:', espIp);
      if (ip) {
        espIp = ip.trim();
        localStorage.setItem('korvo_esp_ip', espIp);
        checkLedBoard();
      }
    }

    async function sendLed(data) {
      if (!espIp) { setEspIp(); return null; }
      try {
        const res = await fetch(`http://${espIp}/api/led`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        });
        const text = await res.text();
        if (!res.ok) {
          let msg = 'LED request failed (' + res.status + ')';
          try {
            const err = JSON.parse(text);
            if (err.error) msg = err.error;
          } catch (x) { /* ignore */ }
          toast(msg, 'error');
          return null;
        }
        const state = JSON.parse(text);
        updateLedUI(state);
        return state;
      } catch (e) {
        document.getElementById('ledBadge').className = 'badge disconnected';
        document.getElementById('ledBadge').textContent = 'Board offline';
        ledOnline = false;
        toast('LED request failed (network)', 'error');
        return null;
      }
    }

    async function checkLedBoard() {
      if (!espIp) return;
      try {
        const res = await fetch(`http://${espIp}/api/led`, { signal: AbortSignal.timeout(2000) });
        const state = await res.json();
        ledOnline = true;
        document.getElementById('ledBadge').className = 'badge connected';
        document.getElementById('ledBadge').textContent = espIp;
        updateLedUI(state);
      } catch (e) {
        ledOnline = false;
        document.getElementById('ledBadge').className = 'badge disconnected';
        document.getElementById('ledBadge').textContent = 'Board offline';
      }
    }

    function updateLedUI(state) {
      ledInitPixSelect();
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
      document.getElementById('ledBrightness').value = state.brightness;
      document.getElementById('brightnessVal').textContent = state.brightness;
      /* Preset 6 = per-pixel: do not touch the color picker (LED index changes & polls should not clobber it). */
      if (lastLedState.preset !== 6) {
        const hex = '#' + [state.r, state.g, state.b].map((v) => Number(v).toString(16).padStart(2, '0')).join('');
        document.getElementById('ledColor').value = hex;
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
      await sendLed({ on: preset > 0, preset, r, g, b, brightness: parseInt(document.getElementById('ledBrightness').value, 10) });
    }

    async function ledNamedToggle(r, g, b) {
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || 180;
      if (lastLedState.on && lastLedState.preset === 1 && ledRgbClose(lastLedState.r, lastLedState.g, lastLedState.b, r, g, b)) {
        await sendLed({ on: false, preset: 0 });
      } else {
        await sendLed({ on: true, preset: 1, r, g, b, brightness: br });
      }
    }

    function ledHexToRgb(hex) {
      return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
    }

    async function ledApplyPickerToIndex() {
      const idx = parseInt(document.getElementById('ledPixIndex').value, 10);
      if (isNaN(idx)) return;
      const hex = document.getElementById('ledColor').value;
      const rgb = ledHexToRgb(hex);
      korvoStripPixels[idx] = rgb.slice();
      renderLedStripPreview();
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || 180;
      await sendLed({ pixel: { i: idx, r: rgb[0], g: rgb[1], b: rgb[2] }, preset: 6, on: true, brightness: br });
    }

    async function ledTurnIndexOff() {
      const idx = parseInt(document.getElementById('ledPixIndex').value, 10);
      if (isNaN(idx)) return;
      korvoStripPixels[idx] = [0, 0, 0];
      renderLedStripPreview();
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || 180;
      await sendLed({ pixel: { i: idx, r: 0, g: 0, b: 0 }, preset: 6, on: true, brightness: br });
    }

    async function ledPushFullStrip() {
      const br = parseInt(document.getElementById('ledBrightness').value, 10) || 180;
      const px = [];
      for (let i = 0; i < KORVO_LED_COUNT_DEFAULT; i++) {
        px.push((korvoStripPixels[i] || [0, 0, 0]).slice(0, 3));
      }
      await sendLed({ pixels: px, preset: 6, on: true, brightness: br });
    }

    // Event listeners for color/brightness changes
    document.getElementById('ledColor').addEventListener('change', async (e) => {
      if (lastLedState.preset === 6) return;
      const hex = e.target.value;
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      await sendLed({ r, g, b });
    });

    document.getElementById('ledBrightness').addEventListener('input', (e) => {
      document.getElementById('brightnessVal').textContent = e.target.value;
    });
    document.getElementById('ledBrightness').addEventListener('change', async (e) => {
      await sendLed({ brightness: parseInt(e.target.value) });
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
        await korvoMic.start(playUrl, {
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
    korvoInitTabs();
    loadWifi();
    bootstrapAfterSettings();
    loadPorts();
    if (typeof initTranslationSection === 'function') initTranslationSection();
    if (typeof initThirdPartySection === 'function') initThirdPartySection();
  
