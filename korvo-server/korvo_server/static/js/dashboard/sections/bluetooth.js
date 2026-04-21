(function () {
  let btFinding = false;
  let btPollTimer = null;

  function btStatusEl() { return document.getElementById('btStatus'); }
  function btStatusBadgeEl() { return document.getElementById('btStatusBadge'); }
  function btDeviceCountEl() { return document.getElementById('btDeviceCount'); }
  function btListEl() { return document.getElementById('btDeviceList'); }
  function btConnectedEl() { return document.getElementById('btConnectedTarget'); }
  function btPreferredEl() { return document.getElementById('btPreferredTarget'); }
  function btFindBtn() { return document.getElementById('btFindBtn'); }
  function btAutoReconnectEl() { return document.getElementById('btAutoReconnectToggle'); }

  function boardBaseUrl() {
    const ipEl = document.getElementById('boardIp');
    const espEl = document.getElementById('espIpInput');
    let host = (ipEl && ipEl.value ? ipEl.value : '').trim();
    if (!host) host = (espEl && espEl.value ? espEl.value : '').trim();
    if (!host) {
      host = (localStorage.getItem('korvo_board_ip') || localStorage.getItem('korvo_esp_ip') || '').trim();
    }
    if (!host) return '';
    if (/^https?:\/\//i.test(host)) return host.replace(/\/+$/, '');
    return `http://${host}`;
  }

  function btSetStatus(message, kind) {
    const st = btStatusEl();
    const badge = btStatusBadgeEl();
    if (st) st.textContent = message;
    if (badge) {
      badge.textContent = kind === 'connected' ? 'Connected' : kind === 'scanning' ? 'Scanning' : 'Idle';
      badge.className = `badge ${kind === 'connected' ? 'connected' : kind === 'scanning' ? 'scanning' : 'disconnected'}`;
    }
  }

  function renderDevices(devices) {
    const el = btListEl();
    if (!el) return;
    const list = Array.isArray(devices) ? devices.slice() : [];
    list.sort((a, b) => (b.rssi || -999) - (a.rssi || -999));
    const count = btDeviceCountEl();
    if (count) count.textContent = `${list.length} device${list.length === 1 ? '' : 's'}`;
    if (!list.length) {
      el.innerHTML = '<div style="padding:0.55rem 0.6rem;font-size:0.82rem;opacity:0.75;">No devices found yet. Press Start scan.</div>';
      return;
    }
    el.innerHTML = '';
    list.forEach((d) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:0.6rem;padding:0.45rem 0.5rem;border-bottom:1px solid #1c1c1c;';
      const left = document.createElement('div');
      left.style.cssText = 'min-width:0;flex:1;';
      const addr = (d.addr || '').trim();
      const name = (d.name || '').trim() || (addr ? `Device ${addr}` : 'Unnamed device');
      left.innerHTML = `<div style="font-size:0.84rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${name}</div><div style="font-size:0.72rem;opacity:0.72;">${addr} · RSSI ${d.rssi ?? 'n/a'}</div>`;
      const btn = document.createElement('button');
      btn.className = 'btn-secondary';
      btn.textContent = 'Connect';
      btn.onclick = () => btConnectDevice(d);
      row.appendChild(left);
      row.appendChild(btn);
      el.appendChild(row);
    });
  }

  function setFindingUI(on) {
    btFinding = !!on;
    const btn = btFindBtn();
    if (btn) btn.textContent = btFinding ? 'Stop scan' : 'Start scan';
    btSetStatus(btFinding ? 'Scanning for nearby devices…' : 'Idle', btFinding ? 'scanning' : 'idle');
  }

  async function btFetchStatus() {
    const base = boardBaseUrl();
    if (!base) {
      btSetStatus('Set board IP in Settings/Audio first', 'idle');
      return;
    }
    try {
      const res = await fetch(`${base}/api/bluetooth/status`, { method: 'GET' });
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      if (!data.enabled) {
        btSetStatus('Bluetooth not enabled on board firmware', 'idle');
        renderDevices([]);
        return;
      }
      setFindingUI(!!data.finding);
      const c = btConnectedEl();
      if (c) c.value = data.connected ? `${data.connected_name || 'Unnamed'} (${data.connected_addr || ''})` : 'Not connected yet';
      const p = btPreferredEl();
      if (p) p.value = data.preferred_addr ? `${data.preferred_name || 'Unnamed'} (${data.preferred_addr})` : 'No preferred device set';
      const auto = btAutoReconnectEl();
      if (auto) auto.checked = !!data.auto_reconnect;
      if (data.connecting) {
        btSetStatus('Connecting to selected Bluetooth device…', 'scanning');
      } else if (data.connected) {
        btSetStatus(data.bt_output_route ? 'Connected (Bluetooth output active)' : 'Connected', 'connected');
      } else if (!data.finding) {
        btSetStatus('Idle', 'idle');
      }
      renderDevices(data.devices || []);
    } catch (e) {
      btSetStatus(`Bluetooth status failed: ${e.message || e}`, 'idle');
    }
  }

  async function btSetFinding(on) {
    const base = boardBaseUrl();
    if (!base) {
      btSetStatus('Set board IP in Settings/Audio first', 'idle');
      return;
    }
    try {
      const res = await fetch(`${base}/api/bluetooth/find`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ finding: !!on }),
      });
      if (!res.ok) throw new Error(`find ${res.status}`);
      setFindingUI(!!on);
      await btFetchStatus();
    } catch (e) {
      btSetStatus(`Scan toggle failed: ${e.message || e}`, 'idle');
    }
  }

  async function btConnectDevice(device) {
    const base = boardBaseUrl();
    if (!base) {
      btSetStatus('Set board IP in Settings/Audio first', 'idle');
      return;
    }
    try {
      const res = await fetch(`${base}/api/bluetooth/connect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ addr: device.addr || '', name: device.name || '' }),
      });
      if (!res.ok) throw new Error(`connect ${res.status}`);
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'connect_failed');
      await btSetPreferred(device);
      btSetStatus('Connected. This device is now preferred.', 'connected');
      await btFetchStatus();
    } catch (e) {
      btSetStatus(`Connect failed: ${e.message || e}`, 'idle');
    }
  }

  async function btToggleFinding() {
    await btSetFinding(!btFinding);
  }

  async function btSetPreferred(device) {
    const base = boardBaseUrl();
    if (!base) return;
    await fetch(`${base}/api/bluetooth/preferred`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ addr: device.addr || '', name: device.name || '' }),
    });
  }

  async function btDisconnectDevice() {
    const base = boardBaseUrl();
    if (!base) {
      btSetStatus('Set board IP in Settings/Audio first', 'idle');
      return;
    }
    try {
      const res = await fetch(`${base}/api/bluetooth/disconnect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(`disconnect ${res.status}`);
      btSetStatus('Disconnected. Output returned to onboard speakers.', 'idle');
      await btFetchStatus();
    } catch (e) {
      btSetStatus(`Disconnect failed: ${e.message || e}`, 'idle');
    }
  }

  async function btForgetPreferred() {
    const base = boardBaseUrl();
    if (!base) {
      btSetStatus('Set board IP in Settings/Audio first', 'idle');
      return;
    }
    try {
      const res = await fetch(`${base}/api/bluetooth/preferred/clear`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(`forget ${res.status}`);
      btSetStatus('Preferred device cleared.', 'idle');
      await btFetchStatus();
    } catch (e) {
      btSetStatus(`Forget preferred failed: ${e.message || e}`, 'idle');
    }
  }

  async function btSetAutoReconnect(enabled) {
    const base = boardBaseUrl();
    if (!base) {
      btSetStatus('Set board IP in Settings/Audio first', 'idle');
      return;
    }
    try {
      const res = await fetch(`${base}/api/bluetooth/auto`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !!enabled }),
      });
      if (!res.ok) throw new Error(`auto ${res.status}`);
      btSetStatus(enabled ? 'Auto reconnect enabled.' : 'Auto reconnect disabled.', 'idle');
      await btFetchStatus();
    } catch (e) {
      btSetStatus(`Auto reconnect update failed: ${e.message || e}`, 'idle');
    }
  }

  async function btRefreshDevices() {
    await btFetchStatus();
  }

  function startBtPolling() {
    if (btPollTimer) clearInterval(btPollTimer);
    btPollTimer = setInterval(() => {
      btFetchStatus();
    }, 1200);
  }

  function initBluetoothSection() {
    const auto = btAutoReconnectEl();
    if (auto && !auto.dataset.bound) {
      auto.dataset.bound = '1';
      auto.addEventListener('change', () => btSetAutoReconnect(auto.checked));
    }
    btFetchStatus();
    startBtPolling();
  }

  window.initBluetoothSection = initBluetoothSection;
  window.btToggleFinding = btToggleFinding;
  window.btRefreshDevices = btRefreshDevices;
  window.btDisconnectDevice = btDisconnectDevice;
  window.btForgetPreferred = btForgetPreferred;
})();
