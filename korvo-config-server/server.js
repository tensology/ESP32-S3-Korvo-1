const express = require('express');
const Database = require('better-sqlite3');
const { spawn } = require('child_process');
const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const axios = require('axios');

const app = express();
const PORT = 3333;

const REPO_ROOT = path.join(__dirname, '..');
const KORVO_IDF_SH = path.join(REPO_ROOT, 'scripts', 'korvo-idf.sh');

/** Full env after sourcing setup.sh — GUI-launched Node often has a tiny PATH; terminal does not. */
let flashSpawnEnvCache = null;
function getFlashSpawnEnv() {
  if (flashSpawnEnvCache) return flashSpawnEnvCache;
  const setupSh = path.join(REPO_ROOT, 'setup.sh');
  const base = { ...process.env, HOME: process.env.HOME || process.env.USERPROFILE || '' };
  if (!fs.existsSync(setupSh)) {
    const fb = { ...base, PYTHONUNBUFFERED: '1' };
    for (const k of ['IDF_PROJECT_DIR', 'EXTRA_COMPONENT_DIRS', 'IDF_COMPONENT_DIRS']) delete fb[k];
    flashSpawnEnvCache = fb;
    return flashSpawnEnvCache;
  }
  try {
    const envText = execSync(`/bin/bash -c 'set -e && source "${setupSh}" >/dev/null && env'`, {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      timeout: 120000,
      env: base,
      maxBuffer: 16 * 1024 * 1024,
    });
    const merged = { ...base };
    for (const line of envText.split('\n')) {
      const eq = line.indexOf('=');
      if (eq <= 0) continue;
      const k = line.slice(0, eq);
      const v = line.slice(eq + 1);
      if (k) merged[k] = v;
    }
    merged.PYTHONUNBUFFERED = '1';
    for (const k of ['IDF_PROJECT_DIR', 'EXTRA_COMPONENT_DIRS', 'IDF_COMPONENT_DIRS']) {
      delete merged[k];
    }
    flashSpawnEnvCache = merged;
    console.log('[korvo] flash env cached (PATH len=%d)', (merged.PATH || '').length);
    return flashSpawnEnvCache;
  } catch (e) {
    console.error('[korvo] getFlashSpawnEnv failed, using process.env:', e.message);
    const fb = { ...base, PYTHONUNBUFFERED: '1' };
    for (const k of ['IDF_PROJECT_DIR', 'EXTRA_COMPONENT_DIRS', 'IDF_COMPONENT_DIRS']) delete fb[k];
    flashSpawnEnvCache = fb;
    return flashSpawnEnvCache;
  }
}

// Import model configurations
const { ModelManager } = require('./models');
const modelManager = new ModelManager();

// DB setup
const db = new Database(path.join(__dirname, 'korvo.db'));
db.pragma('journal_mode = WAL');

db.exec(`
  CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );
  CREATE TABLE IF NOT EXISTS wifi_networks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ssid TEXT NOT NULL,
    password TEXT NOT NULL,
    is_active INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
  );
`);

// Migrate: add missing columns from older schema versions
try { db.exec('ALTER TABLE wifi_networks ADD COLUMN updated_at DATETIME'); } catch(e) {}
try { db.exec('ALTER TABLE wifi_networks ADD COLUMN is_active INTEGER DEFAULT 0'); } catch(e) {}

app.use(express.json());
app.use((req, res, next) => {
  const o = req.headers.origin;
  if (o) res.setHeader('Access-Control-Allow-Origin', o);
  else res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Accept');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});
app.use(express.static(path.join(__dirname, 'public')));

// ─── WiFi Network Scanning (macOS + ESP32) ───
app.get('/api/networks', (req, res) => {
  const networks = [];
  const seen = new Set();
  const source = req.query.source || 'all'; // 'mac', 'esp32', 'all'

  // 1. Swift CoreWLAN scanner (best on modern macOS)
  if (source === 'all' || source === 'mac') {
    try {
      // Try the app bundle first (has Location Services entitlements)
      const appBin = path.join(__dirname, 'bin', 'KorvoWiFiScanner.app', 'Contents', 'MacOS', 'wifi_scan');
      const plainBin = path.join(__dirname, 'bin', 'wifi_scan');
      const scanBin = fs.existsSync(appBin) ? appBin : (fs.existsSync(plainBin) ? plainBin : null);
      
      if (scanBin) {
        const output = execSync(`"${scanBin}"`, { encoding: 'utf8', timeout: 8000, killSignal: 'SIGKILL' });
        for (const line of output.trim().split('\n')) {
          if (!line || line.startsWith('ERROR')) continue;
          const parts = line.split('|');
          if (parts.length >= 2 && parts[0]) {
            const type = parts[0]; // CURRENT or SCAN
            const ssid = parts[1];
            const rssi = parseInt(parts[2]) || -50;
            const security = parts[3] || 'Unknown';
            if (ssid && !seen.has(ssid)) {
              seen.add(ssid);
              networks.push({ ssid, rssi, security: type === 'CURRENT' ? 'Connected' : security, source: 'mac', current: type === 'CURRENT' });
            }
          }
        }
      }
    } catch (e) { /* Swift scanner timed out or needs Location permission */ }

    // Fallback: airport utility
    if (networks.length === 0) {
      try {
        const airportPath = '/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport';
        if (fs.existsSync(airportPath)) {
          const output = execSync(
            `${airportPath} -s`,
            { encoding: 'utf8', timeout: 5000 }
          );
          const lines = output.trim().split('\n').slice(1);
          for (const line of lines) {
            const match = line.match(/^\s*(.+?)\s+([0-9a-f:]{17})\s+(-?\d+)\s+(-?\d+)\s+(\w+)\s+(.+)?/);
            if (match) {
              const ssid = match[1].trim();
              const rssi = parseInt(match[3]);
              const security = (match[6] || '').trim();
              if (ssid && !seen.has(ssid)) {
                seen.add(ssid);
                networks.push({ ssid, rssi, security, source: 'mac' });
              }
            }
          }
        }
      } catch (e) {}
    }
  }

  // 2. ESP32 WiFi scan (board runs a scan endpoint when in AP mode)
  if (source === 'all' || source === 'esp32') {
    const espHost = req.query.esp_host || '192.168.4.1';
    try {
      const espRes = execSync(`curl -s --connect-timeout 1 --max-time 2 "http://${espHost}/scan"`, {
        encoding: 'utf8', timeout: 4000
      });
      const espNetworks = JSON.parse(espRes);
      if (Array.isArray(espNetworks)) {
        for (const n of espNetworks) {
          if (n.ssid && !seen.has(n.ssid)) {
            seen.add(n.ssid);
            networks.push({ ...n, source: 'esp32' });
          }
        }
      }
    } catch (e) { /* ESP32 not in AP mode — that's fine */ }
  }

  networks.sort((a, b) => b.rssi - a.rssi);
  
  const note = networks.length === 0 
    ? 'macOS redacts SSIDs on modern versions. Enter your SSID manually, or use the ESP32 to scan (connect to its AP at 192.168.4.1)'
    : undefined;
  
  res.json({ networks, source: networks.length > 0 ? 'mac' : 'none', note });
});

// ─── WiFi Config CRUD ───
app.get('/api/wifi', (req, res) => {
  const networks = db.prepare('SELECT id, ssid, is_active, created_at FROM wifi_networks ORDER BY created_at DESC').all();
  const active = db.prepare('SELECT * FROM wifi_networks WHERE is_active = 1').get();
  res.json({ networks, active: active || null });
});

app.post('/api/wifi', (req, res) => {
  const { ssid, password, set_active } = req.body;
  if (!ssid) return res.status(400).json({ error: 'SSID required' });
  const existing = db.prepare('SELECT id FROM wifi_networks WHERE ssid = ?').get(ssid);
  if (existing) {
    db.prepare('UPDATE wifi_networks SET password = ?, updated_at = CURRENT_TIMESTAMP WHERE ssid = ?').run(password || '', ssid);
    if (set_active) {
      db.prepare('UPDATE wifi_networks SET is_active = 0').run();
      db.prepare('UPDATE wifi_networks SET is_active = 1 WHERE ssid = ?').run(ssid);
    }
    res.json({ success: true, id: existing.id, updated: true });
  } else {
    if (set_active) db.prepare('UPDATE wifi_networks SET is_active = 0').run();
    const result = db.prepare('INSERT INTO wifi_networks (ssid, password, is_active) VALUES (?, ?, ?)').run(ssid, password || '', set_active ? 1 : 0);
    res.json({ success: true, id: result.lastInsertRowid });
  }
  generateConfig();
});

app.delete('/api/wifi/:id', (req, res) => {
  db.prepare('DELETE FROM wifi_networks WHERE id = ?').run(req.params.id);
  generateConfig();
  res.json({ success: true });
});

app.post('/api/wifi/:id/activate', (req, res) => {
  db.prepare('UPDATE wifi_networks SET is_active = 0').run();
  db.prepare('UPDATE wifi_networks SET is_active = 1 WHERE id = ?').run(req.params.id);
  generateConfig();
  res.json({ success: true });
});

// ─── Generic Settings ───
app.get('/api/settings', (req, res) => {
  const rows = db.prepare('SELECT key, value, updated_at FROM settings').all();
  const settings = {};
  for (const row of rows) settings[row.key] = row.value;
  res.json(settings);
});

app.post('/api/settings', (req, res) => {
  const stmt = db.prepare('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)');
  for (const [key, value] of Object.entries(req.body)) stmt.run(key, String(value));
  generateConfig();
  res.json({ success: true });
});

// ─── Generate Config ───
function cMacroString(value) {
  return String(value ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\r?\n/g, ' ');
}

function generateConfig() {
  const activeWifi = db.prepare('SELECT ssid, password FROM wifi_networks WHERE is_active = 1').get();
  const settings = {};
  const rows = db.prepare('SELECT key, value FROM settings').all();
  for (const row of rows) settings[row.key] = row.value;

  const config = {
    wifi: activeWifi ? { ssid: activeWifi.ssid, password: activeWifi.password } : null,
    settings,
    generated_at: new Date().toISOString(),
  };

  const jsonText = JSON.stringify(config, null, 2);
  const skainetMain = path.join(REPO_ROOT, 'esp-skainet', 'examples', 'wake_word_detection', 'wakenet', 'main');
  const appMain = path.join(REPO_ROOT, 'korvo-app', 'main');

  const lines = [
    '// Auto-generated by korvo-config-server — DO NOT EDIT',
    '#ifndef KORVO_CONFIG_H',
    '#define KORVO_CONFIG_H',
    '',
    config.wifi ? `#define KORVO_WIFI_SSID "${cMacroString(config.wifi.ssid)}"` : '#define KORVO_WIFI_SSID ""',
    config.wifi ? `#define KORVO_WIFI_PASSWORD "${cMacroString(config.wifi.password)}"` : '#define KORVO_WIFI_PASSWORD ""',
    '',
  ];
  for (const [key, value] of Object.entries(settings)) {
    const macro = `KORVO_CFG_${key.toUpperCase().replace(/[^A-Z0-9]/g, '_')}`;
    lines.push(`#define ${macro} "${cMacroString(value)}"`);
  }
  lines.push('', '#endif // KORVO_CONFIG_H', '');
  const headerText = lines.join('\n');

  fs.mkdirSync(skainetMain, { recursive: true });
  fs.mkdirSync(appMain, { recursive: true });
  fs.writeFileSync(path.join(skainetMain, 'korvo_config.json'), jsonText);
  fs.writeFileSync(path.join(skainetMain, 'korvo_config.h'), headerText);
  fs.writeFileSync(path.join(appMain, 'korvo_config.json'), jsonText);
  fs.writeFileSync(path.join(appMain, 'korvo_config.h'), headerText);

  return config;
}

// ─── Agent Integration ───
app.post('/api/agent/openrouter', async (req, res) => {
  const { transcript, apiKey, model = 'qwen/qwen3.6-plus' } = req.body;
  
  if (!transcript) {
    return res.status(400).json({ error: 'Transcript required' });
  }
  
  if (!apiKey) {
    return res.status(400).json({ error: 'API key required' });
  }
  
  try {
    const modelConfig = modelManager.getModel(model);
    if (!modelConfig) {
      return res.status(400).json({ error: 'Invalid model' });
    }
    
    const payload = {
      model: model,
      messages: modelManager.formatMessages(transcript),
      max_tokens: modelConfig.maxTokens,
    };
    
    const response = await axios.post(
      modelConfig.endpoint,
      payload,
      {
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          'HTTP-Referer': modelConfig.headers['HTTP-Referer'],
          'X-Title': modelConfig.headers['X-Title']
        }
      }
    );
    
    const resultText = response.data.choices[0].message.content;
    res.json({ response: resultText, modelUsed: model });
    
  } catch (error) {
    console.error('OpenRouter API error:', error.response?.data || error.message);
    res.status(500).json({ error: 'Failed to get response from OpenRouter API', details: error.message });
  }
});

app.get('/api/models', (req, res) => {
  res.json({
    availableModels: modelManager.getModels(),
    defaultModel: modelManager.getDefaultModel()
  });
});

app.get('/api/config', (req, res) => res.json(generateConfig()));

// ─── Diagnose IDF wrapper (run from browser host or curl) ───
app.get('/api/flash/diag', (req, res) => {
  const setupSh = path.join(REPO_ROOT, 'setup.sh');
  const fe = getFlashSpawnEnv();
  const out = {
    repo_root: REPO_ROOT,
    wrapper: KORVO_IDF_SH,
    wrapper_exists: fs.existsSync(KORVO_IDF_SH),
    setup_sh: setupSh,
    setup_exists: fs.existsSync(setupSh),
    idf_path: fe.IDF_PATH || process.env.IDF_PATH || null,
    path_len_node: (process.env.PATH || '').length,
    path_len_after_setup: (fe.PATH || '').length,
    idf_py: null,
  };
  try {
    out.idf_py = execSync(`/bin/bash -c 'source "${setupSh}" >/dev/null && command -v idf.py'`, {
      encoding: 'utf8',
      timeout: 20000,
      env: fe,
    }).trim();
    res.json(out);
  } catch (e) {
    out.error = e.message || String(e);
    res.status(500).json(out);
  }
});

// ─── Serial Ports (macOS: prefer /dev/cu.* for esptool; include SLAB / wchusb, not Bluetooth) ───
app.get('/api/ports', (req, res) => {
  try {
    const skip = /^(Bluetooth|debug-console|JBL|GroundControl|AVT)/i;
    const rank = (name) => {
      const s = name.toLowerCase();
      if (s.includes('usbserial')) return 0;
      if (s.includes('wchusb')) return 1;
      if (s.includes('usbmodem')) return 1;
      if (s.includes('slab')) return 2;
      return 5;
    };
    const ports = fs
      .readdirSync('/dev')
      .filter((f) => {
        if (!f.startsWith('cu.')) return false;
        const rest = f.slice(3);
        if (skip.test(rest)) return false;
        return /usb|serial|SLAB|wch|modem|acm/i.test(rest);
      })
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
      .map((f) => `/dev/${f}`);
    res.json({ ports });
  } catch (err) {
    res.json({ ports: [], error: err.message || String(err) });
  }
});

// ─── Build & Flash (streaming SSE) ───
app.post('/api/flash', (req, res) => {
  const rawPort = (req.body && req.body.port) ? String(req.body.port).trim() : '';
  const port = rawPort.startsWith('/dev/') ? rawPort : `/dev/${rawPort}`;
  const projectDir = path.join(REPO_ROOT, 'korvo-app');
  const portOk = /^\/dev\/cu\.[^/]+$/.test(port) || /^\/dev\/tty(USB|ACM)[0-9]+$/.test(port);
  console.log('[flash] POST port=%s ok=%s bodyKeys=%s', port, portOk, req.body ? Object.keys(req.body).join(',') : 'no-body');

  if (!portOk) {
    res.status(400).json({ error: 'Select a serial port such as /dev/cu.usbserial-* (macOS) or /dev/ttyUSB0 (Linux). Avoid /dev/tty.* on macOS with esptool.' });
    return;
  }

  if (!fs.existsSync(KORVO_IDF_SH)) {
    res.status(500).json({ error: `Missing ${KORVO_IDF_SH}. Clone the full Korvo repo (scripts/korvo-idf.sh).` });
    return;
  }

  if (req.socket) req.socket.setTimeout(0);
  res.setTimeout(0);

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  if (typeof res.flushHeaders === 'function') res.flushHeaders();
  res.write(': stream-open\n\n');

  const spawnEnv = getFlashSpawnEnv();

  const send = (event, data) => {
    try {
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    } catch (e) {
      console.error('flash SSE write failed:', e.message);
    }
  };

  const ansiRe = /\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07/g;
  function runStreamed(execPath, execArgs, label, onLine) {
    return new Promise((resolve, reject) => {
      send('phase', label);
      const proc = spawn(execPath, execArgs, { cwd: projectDir, env: spawnEnv });
      const emitLine = (line, which) => {
        const clean = line.replace(ansiRe, '').trim();
        if (clean) {
          send('output', { text: clean, source: which });
          if (onLine) onLine(clean);
        }
      };
      let outBuf = '';
      proc.stdout.on('data', (chunk) => {
        outBuf += chunk.toString();
        const parts = outBuf.split('\n');
        outBuf = parts.pop() || '';
        for (const line of parts) emitLine(line, 'stdout');
      });
      let errBuf = '';
      proc.stderr.on('data', (chunk) => {
        errBuf += chunk.toString();
        const parts = errBuf.split('\n');
        errBuf = parts.pop() || '';
        for (const line of parts) emitLine(line, 'stderr');
      });
      proc.on('close', (code) => {
        if (outBuf.trim()) emitLine(outBuf, 'stdout');
        if (errBuf.trim()) emitLine(errBuf, 'stderr');
        if (code === 0) resolve();
        else reject(new Error(`${label} failed (exit ${code})`));
      });
      proc.on('error', (err) => reject(err));
    });
  }

  const runIdf = (args, label, onLine) => runStreamed('/bin/bash', [KORVO_IDF_SH, ...args], label, onLine);

  const cleanBeforeBuild = !!(req.body && (req.body.clean_before_build === true || req.body.clean_before_build === 'true' || req.body.clean === true));

  (async () => {
    try {
      send('output', { text: `[flash] ${new Date().toISOString()} port=${port} clean_before_build=${cleanBeforeBuild}`, source: 'system' });
      try {
        generateConfig();
        send('output', { text: '✅ Config generated', source: 'system' });
      } catch (e) {
        send('output', { text: `❌ generateConfig: ${e.message}`, source: 'error' });
        throw e;
      }

      if (cleanBeforeBuild) {
        const ninjaFile = path.join(projectDir, 'build', 'build.ninja');
        if (fs.existsSync(ninjaFile)) {
          await runStreamed('/bin/bash', ['-c', 'ninja -C build clean'], '🧹 ninja clean (forces full recompile)…');
          send('output', { text: '✅ Clean finished — compiling from scratch', source: 'system' });
        } else {
          send('output', { text: 'ℹ️ Skipping ninja clean (no build/ yet — first configure will create it)', source: 'system' });
        }
      }

      await runIdf(['build'], '🔨 Building firmware...');
      send('output', { text: '✅ Build complete', source: 'system' });

      send('flash_start', { port });
      send('output', { text: `→ Esptool will use ONLY this port: ${port}`, source: 'system' });
      await runIdf(['-p', port, 'flash'], '📡 Flashing firmware...', (line) => {
        if (line.includes('Connecting')) {
          send('boot_mode', { message: 'Hold BOOT, press RESET, release BOOT', countdown: 8 });
        }
        if (line.includes('Chip is ')) {
          send('boot_success', { message: 'Board connected — flashing...' });
        }
        if (line.includes('Writing at')) {
          send('flash_progress', { text: line });
        }
        if (line.includes('Hard resetting')) {
          send('flash_done', { message: 'Firmware flashed — board resetting' });
        }
      });

      await new Promise((r) => setTimeout(r, 900));
      const idfHome = spawnEnv.IDF_PATH || path.join(spawnEnv.HOME || process.env.HOME || '', 'esp', 'esp-idf');
      const espTool = path.join(idfHome, 'components', 'esptool_py', 'esptool', 'esptool.py');
      if (fs.existsSync(espTool)) {
        try {
          await runStreamed(
            '/bin/bash',
            ['-c', `python3 "${espTool}" --chip auto -p "${port}" read_mac 2>&1`],
            '🔎 Post-flash: probe chip on selected port…'
          );
        } catch (e) {
          send('output', { text: `⚠️ Post-flash probe failed: ${e.message} — wrong /dev/cu.* or port busy?`, source: 'error' });
        }
      }

      send('output', { text: '✅ Flash complete!', source: 'system' });
      send('complete', { success: true, message: 'Firmware built & flashed successfully!' });
    } catch (err) {
      console.error('/api/flash error:', err);
      send('output', { text: `❌ ${err.message}`, source: 'error' });
      send('complete', { success: false, message: err.message });
    }

    res.end();
  })();
});

app.listen(PORT, () => {
  console.log(`🔧 Korvo Config Server running at http://localhost:${PORT}`);
  try {
    getFlashSpawnEnv();
  } catch (e) {
    console.warn('[korvo] flash env warmup:', e.message);
  }
  generateConfig();
});