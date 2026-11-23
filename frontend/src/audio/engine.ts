import { base64, float32, FRAME_SAMPLES, fromBase64, pcm16, PlaybackTimeline, SAMPLE_RATE } from './dsp';

type SinkTarget = { setSinkId?: (id: string) => Promise<void> };

export class AudioEngine {
  private context?: AudioContext;
  private stream?: MediaStream;
  private source?: MediaStreamAudioSourceNode;
  private worklet?: AudioWorkletNode;
  private gain?: GainNode;
  private remote?: HTMLAudioElement;
  private sources = new Set<AudioBufferSourceNode>();
  private timeline = new PlaybackTimeline();
  private silenceTimer?: ReturnType<typeof setInterval>;
  private uploadGeneration = 0;
  private micGeneration = 0;
  private sink = '';
  private speakerMuted = false;
  uploading = false;

  constructor(private readonly onError: (error: unknown) => void) {}

  async initialize() {
    if (!window.AudioContext) throw new Error('浏览器不支持 Web Audio；请使用新版 Chrome / Edge');
    this.context = new AudioContext({ sampleRate: SAMPLE_RATE });
    this.gain = this.context.createGain();
    this.gain.connect(this.context.destination);
    await this.context.resume();
  }

  async microphone(deviceId: string): Promise<MediaStream> {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('无法访问麦克风；请使用 HTTPS 或 localhost 并允许麦克风权限');
    const generation = ++this.micGeneration;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1, echoCancellation: true, noiseSuppression: true },
      video: false,
    });
    if (generation !== this.micGeneration || !this.context || this.context.state === 'closed') {
      stream.getTracks().forEach(track => track.stop());
      throw new Error('麦克风请求已取消');
    }
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect();
    this.stream = stream;
    this.source = this.context.createMediaStreamSource(stream);
    if (this.worklet) this.source.connect(this.worklet);
    return stream;
  }

  async capture(send: (audio: string) => void) {
    const context = this.context;
    if (!context || !this.source) throw new Error('麦克风尚未初始化');
    await context.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-worklet.js`);
    if (this.context !== context) return;
    this.worklet = new AudioWorkletNode(context, 'studio-pcm');
    this.worklet.onprocessorerror = () => this.onError(new Error('麦克风 AudioWorklet 处理失败；请结束会话后重试'));
    this.worklet.port.onmessage = ({ data }: MessageEvent<Float32Array>) => {
      if (this.uploading) return;
      try { send(base64(pcm16(data))); }
      catch (error) { this.onError(error); }
    };
    this.source.connect(this.worklet);
    this.worklet.connect(context.destination);
  }

  fileOnlySilence(send: (audio: string) => void) {
    const silence = base64(new Uint8Array(FRAME_SAMPLES * 2));
    this.silenceTimer = setInterval(() => {
      if (this.uploading) return;
      try { send(silence); }
      catch (error) {
        clearInterval(this.silenceTimer);
        this.onError(error);
      }
    }, 20);
  }

  play(audio: string, startMs?: number, endMs?: number) {
    const context = this.context;
    if (!context || context.state === 'closed') return;
    const samples = float32(fromBase64(audio));
    if (!samples.length) return;
    const buffer = context.createBuffer(1, samples.length, SAMPLE_RATE);
    buffer.copyToChannel(samples, 0);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain!);
    const timestamp = startMs ?? (endMs === undefined ? undefined : endMs - buffer.duration * 1000);
    const when = timestamp === undefined
      ? this.timeline.scheduleUntimed(buffer.duration, context.currentTime)
      : this.timeline.schedule(timestamp, buffer.duration, context.currentTime);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    this.sources.add(source);
    source.start(when);
    if (context.state === 'suspended') this.onError(new Error('浏览器暂停了音频播放；请点击「恢复播放」'));
  }

  async remoteStream(stream: MediaStream) {
    this.remote?.pause();
    if (this.remote) this.remote.srcObject = null;
    const audio = new Audio();
    audio.autoplay = true;
    audio.setAttribute('playsinline', '');
    audio.srcObject = stream;
    audio.muted = this.speakerMuted;
    this.remote = audio;
    if (this.sink) await this.outputDevice(this.sink);
    await audio.play();
  }

  muteSpeaker(muted: boolean) {
    this.speakerMuted = muted;
    if (this.gain) this.gain.gain.value = muted ? 0 : 1;
    if (this.remote) this.remote.muted = muted;
  }

  async outputDevice(id: string) {
    const target = (this.remote ?? this.context) as SinkTarget | undefined;
    if (!target?.setSinkId) {
      if (!id) { this.sink = id; return; }
      throw new Error('此浏览器不支持输出设备切换；请在操作系统中选择扬声器');
    }
    await target.setSinkId(id);
    this.sink = id;
  }

  async resume() {
    if (!this.context) throw new Error('请先开始会话');
    await this.context.resume();
    if (this.remote) await this.remote.play();
  }

  async upload(file: File, send: (audio: string) => void, progress: (value: number) => void): Promise<boolean> {
    if (file.size > 20 * 1024 * 1024) throw new Error('音频文件不能超过 20 MB');
    if (!file.size) throw new Error('音频文件为空');
    if (this.uploading) throw new Error('已有文件正在发送，请等待或取消');
    const context = this.context;
    if (!context) throw new Error('请先开始 WebSocket 会话');
    const generation = ++this.uploadGeneration;
    this.uploading = true;
    progress(0);
    try {
      const decoded = await context.decodeAudioData(await file.arrayBuffer());
      if (generation !== this.uploadGeneration) return false;
      if (decoded.duration <= 0 || decoded.duration > 120) throw new Error('音频时长必须大于 0 秒且不超过 120 秒');
      const renderer = new OfflineAudioContext(1, Math.ceil(decoded.duration * SAMPLE_RATE), SAMPLE_RATE);
      const source = renderer.createBufferSource();
      source.buffer = decoded;
      source.connect(renderer.destination);
      source.start();
      const rendered = await renderer.startRendering();
      if (generation !== this.uploadGeneration) return false;
      const samples = rendered.getChannelData(0);
      for (let offset = 0; offset < samples.length; offset += FRAME_SAMPLES) {
        if (generation !== this.uploadGeneration) return false;
        const frame = new Float32Array(FRAME_SAMPLES);
        frame.set(samples.subarray(offset, offset + FRAME_SAMPLES));
        send(base64(pcm16(frame)));
        progress(Math.min(100, Math.round((offset + FRAME_SAMPLES) / samples.length * 100)));
        // Never catch up in bursts after a suspended browser tab.
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      return true;
    } finally {
      if (generation === this.uploadGeneration) this.uploading = false;
    }
  }

  cancelUpload() {
    this.uploadGeneration++;
    this.uploading = false;
  }

  async stop() {
    this.micGeneration++;
    this.cancelUpload();
    clearInterval(this.silenceTimer);
    if (this.worklet) {
      this.worklet.port.onmessage = null;
      this.worklet.onprocessorerror = null;
      this.worklet.disconnect();
      this.worklet.port.close();
    }
    this.source?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
    this.remote?.pause();
    if (this.remote) this.remote.srcObject = null;
    this.remote = undefined;
    for (const source of this.sources) { source.stop(); source.disconnect(); }
    this.sources.clear();
    const context = this.context;
    this.context = undefined;
    if (context && context.state !== 'closed') await context.close();
  }
}
