/**
 * Korvo live mic: GET /api/audio/stream (WAV) via same-origin relay or direct URL.
 * Skips 44-byte header then plays s16le mono @ 16 kHz resampled to AudioContext rate
 * (same PCM standard as scripts/play_korvo_board_audio.py / stream_to_wav.py).
 */
(function (global) {
  const PCM_RATE = 16000;
  const WAV_HDR = 44;

  class KorvoLiveMic {
    constructor() {
      this._ctx = null;
      this._proc = null;
      this._gain = null;
      this._abort = null;
      this._pcmF = new Float32Array(PCM_RATE * 4);
      this._pcmLen = 0;
      this._readPos = 0;
      this._hdrRemain = WAV_HDR;
      this._pending = new Uint8Array(0);
    }

    /**
     * Browser playback loudness (0–1). Does not change the board; use dashboard POST for device volume.
     * @param {number} linear 0..1
     */
    setPlaybackGain(linear) {
      let g = Number(linear);
      if (!Number.isFinite(g)) g = 1;
      g = Math.min(1, Math.max(0, g));
      if (this._gain) {
        this._gain.gain.value = g;
      }
    }

    _appendPending(u8) {
      const n = this._pending.length + u8.length;
      const m = new Uint8Array(n);
      m.set(this._pending, 0);
      m.set(u8, this._pending.length);
      this._pending = m;
    }

    /** Drain pending bytes into mono float @16k (after WAV header). */
    _drainPendingToPcm() {
      if (this._hdrRemain > 0) {
        const take = Math.min(this._hdrRemain, this._pending.length);
        this._pending = this._pending.subarray(take);
        this._hdrRemain -= take;
        if (this._hdrRemain > 0) return;
      }
      const raw = this._pending;
      const pairs = Math.floor(raw.length / 2);
      for (let i = 0; i < pairs; i++) {
        const u = raw[i * 2] | (raw[i * 2 + 1] << 8);
        const s = (u << 16) >> 16;
        const f = s * (1 / 32768);
        if (this._pcmLen >= this._pcmF.length) {
          const drop = PCM_RATE;
          this._pcmF.copyWithin(0, drop, this._pcmLen);
          this._pcmLen -= drop;
          this._readPos = Math.max(0, this._readPos - drop);
        }
        this._pcmF[this._pcmLen++] = f;
      }
      this._pending = raw.subarray(pairs * 2);
    }

    _readInterp(outF32) {
      const sr = this._ctx.sampleRate;
      const ratio = sr / PCM_RATE;
      const needIn = outF32.length / ratio;
      if (this._readPos + needIn + 1 >= this._pcmLen) {
        outF32.fill(0);
        return;
      }
      let rp = this._readPos;
      for (let i = 0; i < outF32.length; i++) {
        const t = rp + i / ratio;
        const i0 = Math.floor(t);
        const frac = t - i0;
        const a = this._pcmF[i0];
        const b = this._pcmF[i0 + 1];
        outF32[i] = a + (b - a) * frac;
      }
      this._readPos += needIn;
      if (this._readPos > PCM_RATE) {
        const sh = Math.floor(this._readPos) - Math.floor(PCM_RATE * 0.25);
        if (sh > 0 && sh < this._pcmLen) {
          this._pcmF.copyWithin(0, sh, this._pcmLen);
          this._pcmLen -= sh;
          this._readPos -= sh;
        }
      }
    }

    /**
     * @param {string} url  relay or direct board stream
     * @param {{ onStatus?: (s: string) => void, onEnded?: (() => void), playbackGain?: number }} opts
     */
    async start(url, opts) {
      opts = opts || {};
      const onStatus = opts.onStatus || function () {};
      this._onEnded = typeof opts.onEnded === "function" ? opts.onEnded : null;
      await this.stop();
      this._hdrRemain = WAV_HDR;
      this._pending = new Uint8Array(0);
      this._pcmLen = 0;
      this._readPos = 0;
      this._abort = new AbortController();
      this._ctx = new AudioContext();
      const proc = this._ctx.createScriptProcessor(2048, 0, 1);
      this._gain = this._ctx.createGain();
      const pg =
        typeof opts.playbackGain === "number" && Number.isFinite(opts.playbackGain)
          ? Math.min(1, Math.max(0, opts.playbackGain))
          : 1;
      this._gain.gain.value = pg;
      proc.onaudioprocess = (ev) => {
        const out = ev.outputBuffer.getChannelData(0);
        this._readInterp(out);
      };
      proc.connect(this._gain);
      this._gain.connect(this._ctx.destination);
      this._proc = proc;
      if (this._ctx.state === "suspended") await this._ctx.resume();
      onStatus(`AudioContext ${this._ctx.sampleRate} Hz — connecting…`);

      let res;
      try {
        res = await fetch(url, {
          signal: this._abort.signal,
          cache: "no-store",
          headers: { Accept: "audio/wav,*/*" },
        });
      } catch (e) {
        const msg = e && e.name === "AbortError" ? "Stopped" : String(e && e.message ? e.message : e);
        await this.stop();
        throw new Error(msg);
      }
      if (!res.ok) {
        await this.stop();
        throw new Error("HTTP " + res.status);
      }
      if (!res.body) {
        await this.stop();
        throw new Error("No response body");
      }
      onStatus(`Streaming (${this._ctx.sampleRate} Hz resampled)`);

      const reader = res.body.getReader();
      void (async () => {
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            if (value && value.length) {
              this._appendPending(value);
              this._drainPendingToPcm();
            }
          }
          onStatus("Stream ended");
        } catch (e) {
          if (this._abort && this._abort.signal.aborted) onStatus("Stopped");
          else onStatus("Error: " + (e && e.message ? e.message : String(e)));
        } finally {
          const cb = this._onEnded;
          this._onEnded = null;
          await this.stop();
          if (cb) {
            try {
              cb();
            } catch (_) {}
          }
        }
      })();
    }

    async stop() {
      this._onEnded = null;
      if (this._abort) {
        try {
          this._abort.abort();
        } catch (_) {}
        this._abort = null;
      }
      if (this._proc) {
        try {
          this._proc.disconnect();
        } catch (_) {}
        this._proc.onaudioprocess = null;
        this._proc = null;
      }
      if (this._gain) {
        try {
          this._gain.disconnect();
        } catch (_) {}
        this._gain = null;
      }
      if (this._ctx) {
        try {
          await this._ctx.close();
        } catch (_) {}
        this._ctx = null;
      }
    }
  }

  global.KorvoLiveMic = KorvoLiveMic;
})(typeof window !== "undefined" ? window : globalThis);
