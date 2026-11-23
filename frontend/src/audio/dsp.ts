export const SAMPLE_RATE = 24000;
export const FRAME_SAMPLES = 480;

export function pcm16(samples: Float32Array): Uint8Array {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  samples.forEach((value, index) => {
    const clamped = Math.min(1, Math.max(-1, value));
    view.setInt16(index * 2, Math.round(clamped * (clamped < 0 ? 32768 : 32767)), true);
  });
  return bytes;
}

export function float32(bytes: Uint8Array): Float32Array<ArrayBuffer> {
  if (bytes.length % 2 !== 0) throw new Error('PCM16LE 数据长度必须为偶数');
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return Float32Array.from({ length: bytes.length / 2 }, (_, index) => view.getInt16(index * 2, true) / 32768);
}

export function base64(bytes: Uint8Array): string {
  let text = '';
  for (let offset = 0; offset < bytes.length; offset += 4096) text += String.fromCharCode(...bytes.subarray(offset, offset + 4096));
  return btoa(text);
}

export const fromBase64 = (value: string): Uint8Array => Uint8Array.from(atob(value), char => char.charCodeAt(0));

export class PlaybackTimeline {
  private anchor?: number;
  private end = 0;

  schedule(startMs: number, duration: number, now: number): number {
    if (!Number.isFinite(startMs) || !Number.isFinite(duration) || duration < 0) throw new Error('输出音频时间戳无效');
    this.anchor ??= now + 0.08 - startMs / 1000;
    let target = this.anchor + startMs / 1000;
    if (target < now) {
      this.anchor += now + 0.04 - target;
      target = now + 0.04;
    }
    const start = Math.max(this.end, target);
    this.end = start + duration;
    return start;
  }

  scheduleUntimed(duration: number, now: number): number {
    if (!Number.isFinite(duration) || duration < 0) throw new Error('输出音频时长无效');
    const start = Math.max(this.end, now + 0.08);
    this.end = start + duration;
    return start;
  }
}
