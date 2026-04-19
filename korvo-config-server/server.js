const express = require('express');
const Database = require('better-sqlite3');
const { spawn } = require('child_process');
const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const axios = require('axios');

const app = express();
const PORT = 3333;

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

  const configDir = path.join(__dirname, '..', 'esp-skainet', 'examples', 'wake_word_detection', 'wakenet', 'main');
  fs.writeFileSync(path.join(configDir, 'korvo_config.json'), JSON.stringify(config, null, 2));

  const lines = [
    '// Auto-generated by korvo-config-server — DO NOT EDIT',
    '#ifndef KORVO_CONFIG_H',
    '#define KORVO_CONFIG_H',
    '',
    config.wifi ? `#define KORVO_WIFI_SSID "${config.wifi.ssid}"` : '#define KORVO_WIFI_SSID ""',
    config.wifi ? `#define KORVO_WIFI_PASSWORD "${config.wifi.password}"` : '#define KORVO_WIFI_PASSWORD ""',
    '',
  ];
  for (const [key, value] of Object.entries(settings)) {
    const macro = `KORVO_CFG_${key.toUpperCase().replace(/[^A-Z0-9]/g, '_')}`;
    lines.push(`#define ${macro} "${value}"`);
  }
  lines.push('', '#endif // KORVO_CONFIG_H', '');
  fs.writeFileSync(path.join(configDir, 'korvo_config.h'), lines.join('\n'));

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

// ─── Serial Ports ───
app.get('/api/ports', (req, res) => {
  try {
    const ports = fs.readdirSync('/dev')
      .filter(f => f.startsWith('cu.usb') || f.startsWith('tty.usb'))
      .map(f => `/dev/${f}`);
    res.json({ ports });
  } catch (err) {
    res.json({ ports: [] });
  }
});

// ─── Build & Flash (streaming SSE) ───
app.post('/api/flash', (req, res) => {
  const port = req.body.port || '/dev/cu.usbserial-11210';
  const projectDir = path.join(__dirname, '..', 'esp-skainet', 'examples', 'wake_word_detection', 'wakenet');
  const setupSh = path.join(__dirname, '..', 'setup.sh');

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  function runCommand(cmd, label, onLine) {
    return new Promise((resolve, reject) => {
      send('phase', label);
      const proc = spawn('bash', ['-c', cmd], {
        cwd: projectDir,
        env: { ...process.env, IDF_PATH: '/Users/paul/esp/esp-idf' },
      });

      let buffer = '';
      proc.stdout.on('data', (chunk) => {
        buffer += chunk.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          // Strip ANSI codes for display
          const clean = line.replace(/\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07/g, '').trim();
          if (clean) {
            send('output', { text: clean, source: 'stdout' });
            if (onLine) onLine(clean);
          }
        }
      });

      let errBuffer = '';
      proc.stderr.on('data', (chunk) => {
        errBuffer += chunk.toString();
        const lines = errBuffer.split('\n');
        errBuffer = lines.pop() || '';
        for (const line of lines) {
          const clean = line.replace(/\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07/g, '').trim();
          if (clean) {
            send('output', { text: clean, source: 'stderr' });
            if (onLine) onLine(clean);
          }
        }
      });

      proc.on('close', (code) => {
        // flush remaining
        if (buffer.trim()) {
          const clean = buffer.replace(/\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07/g, '').trim();
          if (clean) send('output', { text: clean, source: 'stdout' });
        }
        if (code === 0) resolve();
        else reject(new Error(`${label} failed (exit ${code})`));
      });

      proc.on('error', (err) => reject(err));
    });
  }

  (async () => {
    try {
      // 1. Generate config
      generateConfig();
      send('output', { text: '✅ Config generated', source: 'system' });

      // 2. Build
      const buildCmd = `source "${setupSh}" 2>/dev/null && idf.py build 2>&1`;
      await runCommand(buildCmd, '🔨 Building firmware...');
      send('output', { text: '✅ Build complete', source: 'system' });

      // 3. Flash — watch for "Connecting" to trigger boot mode modal
      send('flash_start', { port });
      const flashCmd = `source "${setupSh}" 2>/dev/null && idf.py -p ${port} flash 2>&1`;

      await runCommand(flashCmd, '📡 Flashing firmware...', (line) => {
        // Detect esptool connection phase
        if (line.includes('Connecting')) {
          send('boot_mode', {
            message: 'Hold BOOT, press RESET, release BOOT',
            countdown: 8,
          });
        }
        // Detect successful chip detection
        if (line.includes('Chip is ESP32') || line.includes('Chip is ESP32-S3')) {
          send('boot_success', { message: 'Board connected — flashing...' });
        }
        // Detect write progress
        if (line.includes('Writing at')) {
          send('flash_progress', { text: line });
        }
        // Detect completion
        if (line.includes('Hard resetting')) {
          send('flash_done', { message: 'Firmware flashed — board resetting' });
        }
      });

      send('output', { text: '✅ Flash complete!', source: 'system' });
      send('complete', { success: true, message: 'Firmware built & flashed successfully!' });
    } catch (err) {
      send('output', { text: `❌ ${err.message}`, source: 'error' });
      send('complete', { success: false, message: err.message });
    }

    res.end();
  })();
});

app.listen(PORT, () => {
  console.log(`🔧 Korvo Config Server running at http://localhost:${PORT}`);
  generateConfig();
});