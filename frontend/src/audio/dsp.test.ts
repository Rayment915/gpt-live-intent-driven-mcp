import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { describe, expect, it } from 'vitest';
import { base64, float32, fromBase64, pcm16, PlaybackTimeline } from './dsp';

describe('PCM16LE and streaming playback', () => {
  it('clips and packs headerless little-endian PCM with a loss-bounded roundtrip', () => {
    const packed = pcm16(new Float32Array([-2, -0.5, 0, 0.5, 2]));
    expect([...packed]).toEqual([0, 128, 0, 192, 0, 0, 0, 64, 255, 127]);
    const unpacked = float32(fromBase64(base64(packed)));
    expect([...unpacked]).toEqual([-1, -0.5, 0, 0.5, 32767 / 32768]);
    expect(() => float32(new Uint8Array(3))).toThrow('偶数');
  });

  it('preserves server timeline gaps instead of concatenating speech chunks', () => {
    const clock = new PlaybackTimeline();
    const first = clock.schedule(1000, 0.02, 5);
    const second = clock.schedule(1020, 0.02, 5.01);
    const afterGap = clock.schedule(2020, 0.02, 5.02);
    expect(second - first).toBeCloseTo(0.02);
    expect(afterGap - second).toBeCloseTo(1);
  });

  it('shifts a late schedule forward without overlapping queued output', () => {
    const clock = new PlaybackTimeline();
    clock.schedule(0, 0.3, 1);
    expect(clock.schedule(20, 0.02, 1.01)).toBeCloseTo(1.38);
    const late = clock.schedule(100, 0.02, 3);
    expect(late).toBeGreaterThan(3);
    expect(clock.schedule(1100, 0.02, 3.01) - late).toBeCloseTo(1);
    expect(() => clock.schedule(NaN, 1, 0)).toThrow();
  });

  it('plays native untimestamped chunks in order without losing timestamped gaps later', () => {
    const clock = new PlaybackTimeline();
    expect(clock.scheduleUntimed(0.1, 1)).toBeCloseTo(1.08);
    expect(clock.scheduleUntimed(0.1, 1.01)).toBeCloseTo(1.18);
    expect(clock.schedule(1000, 0.1, 1.1)).toBeCloseTo(1.28);
    expect(clock.schedule(2000, 0.1, 1.2)).toBeCloseTo(2.18);
    expect(() => clock.scheduleUntimed(-1, 1)).toThrow();
  });
});

describe('AudioWorklet capture', () => {
  function worklet(rate: number) {
    const frames: Float32Array[] = [];
    let Processor: new () => { process: (input: Float32Array[][], output: Float32Array[][]) => boolean };
    vm.runInNewContext(readFileSync(new URL('../../public/pcm-worklet.js', import.meta.url), 'utf8'), {
      AudioWorkletProcessor: class { port = { postMessage: (value: Float32Array) => frames.push(new Float32Array(value)) }; },
      sampleRate: rate,
      registerProcessor: (_: string, value: typeof Processor) => { Processor = value; },
      Float32Array,
    });
    return { processor: new Processor!(), frames };
  }

  it('resamples a hardware rate to 24k and emits only 20 ms frames without mic monitoring', () => {
    const { processor, frames } = worklet(48000);
    const output = new Float32Array(1920).fill(1);
    processor.process([[new Float32Array(1920).fill(0.5)]], [[output]]);
    expect(frames).toHaveLength(2);
    expect(frames[0]).toHaveLength(480);
    expect(frames[0].every(value => value === 0.5)).toBe(true);
    expect(output.every(value => value === 0)).toBe(true);
  });

  it('mixes stereo and maintains fractional resampling phase across input blocks', () => {
    const { processor, frames } = worklet(44100);
    for (let i = 0; i < 10; i++) processor.process([[new Float32Array(441).fill(1), new Float32Array(441).fill(-1)]], [[new Float32Array(441)]]);
    expect(frames).toHaveLength(5);
    expect(frames.every(frame => frame.every(value => value === 0))).toBe(true);
  });
});
