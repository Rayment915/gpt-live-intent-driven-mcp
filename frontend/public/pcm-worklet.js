// Integrate source samples into 24 kHz bins; output is always silent.
class StudioPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(480);
    this.offset = 0;
    this.weight = 0;
    this.sum = 0;
  }
  process(inputs, outputs) {
    for (const channel of outputs[0] || []) channel.fill(0);
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let sample = 0;
      for (const channel of channels) sample += channel[i] / channels.length;
      let remaining = 24000 / sampleRate;
      while (remaining > 1e-9) {
        const portion = Math.min(1 - this.weight, remaining);
        this.sum += sample * portion;
        this.weight += portion;
        remaining -= portion;
        if (this.weight >= 1 - 1e-9) {
          this.frame[this.offset++] = this.sum;
          this.sum = this.weight = 0;
          if (this.offset === 480) {
            this.port.postMessage(this.frame, [this.frame.buffer]);
            this.frame = new Float32Array(480);
            this.offset = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('studio-pcm', StudioPcm);
