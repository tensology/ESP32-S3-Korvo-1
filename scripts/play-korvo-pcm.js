#!/usr/bin/env node
/**
 * Low-latency playback: strip 44-byte WAV header from Korvo /api/audio/stream,
 * pipe s16le mono @ 16 kHz into ffplay (avoids browser / demuxer buffering).
 *
 * Usage: node scripts/play-korvo-pcm.js [http://IP/api/audio/stream]
 * Env:    KORVO_STREAM_URL
 *
 * Requires: ffplay (brew install ffmpeg)
 */
const http = require("http");
const https = require("https");
const { spawn, execSync } = require("child_process");
const { promisify } = require("util");
const stream = require("stream");
const pipeline = promisify(stream.pipeline);

const WAV_HEADER_BYTES = 44;
const urlStr = process.argv[2] || process.env.KORVO_STREAM_URL || "http://korvo.local/api/audio/stream";

function whichFfplay() {
  try {
    return execSync("which ffplay", { encoding: "utf8" }).trim() || null;
  } catch {
    return null;
  }
}

async function main() {
  const ffplay = whichFfplay();
  if (!ffplay) {
    console.error("ffplay not found. Install ffmpeg (e.g. brew install ffmpeg).");
    process.exit(1);
  }
  const u = new URL(urlStr);
  const lib = u.protocol === "https:" ? https : http;
  const ff = spawn(
    ffplay,
    [
      "-nodisp",
      "-loglevel",
      "warning",
      "-fflags",
      "nobuffer",
      "-flags",
      "low_delay",
      "-f",
      "s16le",
      "-ar",
      "16000",
      "-ac",
      "1",
      "-i",
      "pipe:0",
    ],
    { stdio: ["pipe", "inherit", "inherit"] }
  );
  const req = lib.request(
    {
      hostname: u.hostname,
      port: u.port || (u.protocol === "https:" ? 443 : 80),
      path: u.pathname + u.search,
      method: "GET",
      headers: { Connection: "close" },
    },
    (res) => {
      if (res.statusCode !== 200) {
        console.error("HTTP", res.statusCode);
        res.resume();
        ff.kill("SIGTERM");
        process.exit(1);
      }
      let skip = 0;
      const tr = new stream.Transform({
        transform(chunk, _enc, cb) {
          let data = chunk;
          if (skip < WAV_HEADER_BYTES) {
            const need = WAV_HEADER_BYTES - skip;
            if (data.length <= need) {
              skip += data.length;
              return cb();
            }
            data = data.subarray(need);
            skip = WAV_HEADER_BYTES;
          }
          cb(null, data);
        },
      });
      pipeline(res, tr, ff.stdin).catch(() => {});
    }
  );
  req.on("error", (e) => {
    console.error(e.message);
    ff.kill("SIGTERM");
    process.exit(1);
  });
  req.end();
  ff.on("close", (code) => process.exit(code === null ? 0 : code));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
