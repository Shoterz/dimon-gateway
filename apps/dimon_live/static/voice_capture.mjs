const TARGET_SAMPLE_RATE = 16_000;
const FRAME_SAMPLES = 320;

export function createVoiceAudioContext(browserWindow = window) {
  const Constructor = browserWindow.AudioContext || browserWindow.webkitAudioContext;
  if (!Constructor) throw new Error("Web Audio is unavailable");
  return new Constructor({latencyHint: "interactive"});
}

export class VoiceDeadline {
  // Wrap the timer globals: browsers throw "Illegal invocation" when
  // setTimeout is called as a method of this object.
  constructor(
    schedule = (callback, delayMs) => setTimeout(callback, delayMs),
    cancel = token => clearTimeout(token),
  ) {
    this.schedule = schedule;
    this.cancel = cancel;
    this.token = null;
  }

  arm(delayMs, callback) {
    this.clear();
    this.token = this.schedule(() => {
      this.token = null;
      callback();
    }, delayMs);
  }

  clear() {
    if (this.token === null) return;
    this.cancel(this.token);
    this.token = null;
  }
}

class StatefulResampler {
  constructor(sourceSampleRate, targetSampleRate = TARGET_SAMPLE_RATE) {
    if (!Number.isFinite(sourceSampleRate) || sourceSampleRate < targetSampleRate) {
      throw new RangeError("invalid browser audio sample rate");
    }
    this.step = sourceSampleRate / targetSampleRate;
    this.filter = this.makeLowPassFilter(sourceSampleRate, targetSampleRate);
    this.history = new Float32Array(this.filter.length);
    this.historyCursor = 0;
    this.buffered = new Float32Array(0);
    this.bufferStart = 0;
    this.nextSourcePosition = 0;
  }

  push(samples) {
    if (!(samples instanceof Float32Array)) throw new TypeError("audio must be Float32Array");
    for (const sample of samples) {
      if (!Number.isFinite(sample)) throw new RangeError("audio contains invalid samples");
    }
    if (samples.length === 0) return new Float32Array(0);
    const filtered = this.filterSamples(samples);
    const combined = new Float32Array(this.buffered.length + filtered.length);
    combined.set(this.buffered);
    combined.set(filtered, this.buffered.length);
    this.buffered = combined;

    const output = [];
    while (this.nextSourcePosition + 1 < this.bufferStart + this.buffered.length) {
      const floor = Math.floor(this.nextSourcePosition);
      const leftIndex = floor - this.bufferStart;
      const fraction = this.nextSourcePosition - floor;
      const left = this.buffered[leftIndex];
      const right = this.buffered[leftIndex + 1];
      output.push(left + (right - left) * fraction);
      this.nextSourcePosition += this.step;
    }

    const discard = Math.min(
      Math.floor(this.nextSourcePosition) - this.bufferStart,
      Math.max(0, this.buffered.length - 1),
    );
    if (discard > 0) {
      this.buffered = this.buffered.slice(discard);
      this.bufferStart += discard;
    }
    return Float32Array.from(output);
  }

  makeLowPassFilter(sourceSampleRate, targetSampleRate) {
    const taps = 63;
    const center = (taps - 1) / 2;
    const cutoff = 0.45 * (targetSampleRate / sourceSampleRate);
    const coefficients = new Float64Array(taps);
    let sum = 0;
    for (let index = 0; index < taps; index += 1) {
      const offset = index - center;
      const sinc = offset === 0
        ? 2 * cutoff
        : Math.sin(2 * Math.PI * cutoff * offset) / (Math.PI * offset);
      const window = 0.42
        - 0.5 * Math.cos((2 * Math.PI * index) / (taps - 1))
        + 0.08 * Math.cos((4 * Math.PI * index) / (taps - 1));
      coefficients[index] = sinc * window;
      sum += coefficients[index];
    }
    for (let index = 0; index < taps; index += 1) coefficients[index] /= sum;
    return coefficients;
  }

  filterSamples(samples) {
    const output = new Float32Array(samples.length);
    for (let sampleIndex = 0; sampleIndex < samples.length; sampleIndex += 1) {
      this.history[this.historyCursor] = samples[sampleIndex];
      let filtered = 0;
      for (let tap = 0; tap < this.filter.length; tap += 1) {
        const historyIndex = (
          this.historyCursor - tap + this.history.length
        ) % this.history.length;
        filtered += this.filter[tap] * this.history[historyIndex];
      }
      output[sampleIndex] = filtered;
      this.historyCursor = (this.historyCursor + 1) % this.history.length;
    }
    return output;
  }
}

export class VoiceFrameResampler {
  constructor(sourceSampleRate) {
    this.resampler = new StatefulResampler(sourceSampleRate);
    this.pending = new Float32Array(0);
  }

  push(samples) {
    const converted = this.resampler.push(samples);
    const combined = new Float32Array(this.pending.length + converted.length);
    combined.set(this.pending);
    combined.set(converted, this.pending.length);
    const frames = [];
    let offset = 0;
    while (combined.length - offset >= FRAME_SAMPLES) {
      frames.push(combined.slice(offset, offset + FRAME_SAMPLES));
      offset += FRAME_SAMPLES;
    }
    this.pending = combined.slice(offset);
    return frames;
  }
}
