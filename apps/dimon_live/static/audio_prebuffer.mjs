export class RollingAudioPrebuffer {
  constructor(maxBytes) {
    if (!Number.isInteger(maxBytes) || maxBytes <= 0) throw new TypeError("maxBytes must be positive");
    this.maxBytes = maxBytes;
    this.frames = [];
    this.byteLength = 0;
  }

  push(frame) {
    if (!(frame instanceof ArrayBuffer) || frame.byteLength === 0) return;
    let copy = frame.slice(0);
    if (copy.byteLength >= this.maxBytes) {
      copy = copy.slice(copy.byteLength - this.maxBytes);
      this.frames = [copy];
      this.byteLength = copy.byteLength;
      return;
    }
    this.frames.push(copy);
    this.byteLength += copy.byteLength;
    while (this.byteLength > this.maxBytes && this.frames.length) {
      const excess = this.byteLength - this.maxBytes;
      const oldest = this.frames[0];
      if (oldest.byteLength <= excess) {
        this.frames.shift();
        this.byteLength -= oldest.byteLength;
      } else {
        this.frames[0] = oldest.slice(excess);
        this.byteLength -= excess;
      }
    }
  }

  drain() {
    const frames = this.frames;
    this.frames = [];
    this.byteLength = 0;
    return frames;
  }

  clear() {
    this.frames = [];
    this.byteLength = 0;
  }
}
