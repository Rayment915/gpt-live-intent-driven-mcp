import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const url = process.env.STUDIO_URL || 'http://localhost:8000';
const credentials = { username: process.env.STUDIO_USERNAME, password: process.env.STUDIO_PASSWORD };
const outputName = process.env.STUDIO_BENCHMARK_OUTPUT || 'startup-optimized.json';
if (!/^[a-zA-Z0-9_.-]+\.json$/.test(outputName)) throw new Error('Invalid benchmark output file name');
const browser = await chromium.launch({
  channel: 'chrome', headless: true,
  args: ['--autoplay-policy=no-user-gesture-required'],
});
const results = [];

try {
  for (let index = 0; index < 2; index++) {
    const page = await browser.newPage();
    await page.goto(url, { waitUntil: 'networkidle' });
    const result = await page.evaluate(async (credentials) => {
      const authResponse = await fetch('/api/auth');
      const auth = await authResponse.json();
      if (!authResponse.ok) throw new Error('Authentication bootstrap failed');
      if (auth.required && !auth.authenticated) {
        if (!credentials.username || !credentials.password) throw new Error('Login required; provide credentials securely through the process environment');
        const logged = await fetch('/api/auth/login', { method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Studio-Csrf': auth.csrf },
          body: JSON.stringify(credentials) });
        if (!logged.ok) throw new Error(`Login failed: HTTP ${logged.status}`);
      }
      const cfg = await (await fetch('/api/config')).json();
      const audio = new AudioContext({ sampleRate: 24000 });
      await audio.resume();
      const destination = audio.createMediaStreamDestination();
      const source = audio.createBufferSource();
      source.buffer = audio.createBuffer(1, 24000, 24000);
      source.loop = true;
      source.connect(destination);
      source.start();
      const peer = new RTCPeerConnection();
      peer.createDataChannel('oai-events');
      destination.stream.getTracks().forEach(track => peer.addTrack(track, destination.stream));
      const result = { actual_microphone: false };
      let session, socket, final = false;
      const post = async (endpoint, body) => {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Studio-Csrf': cfg.csrf },
          body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(data));
        return data;
      };
      const started = performance.now();
      try {
        await peer.setLocalDescription(await peer.createOffer());
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('ICE timeout')), 10000);
          const changed = () => {
            if (peer.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); }
          };
          peer.addEventListener('icegatheringstatechange', changed);
          changed();
        });
        result.ice_ms = performance.now() - started;
        const apiStarted = performance.now();
        session = await post('/api/sessions', {
          transport: 'webrtc', session: cfg.defaultConfig, sdp: peer.localDescription.sdp,
        });
        result.create_api_ms = performance.now() - apiStarted;
        result.tools = cfg.defaultConfig.delegation.responses.tools.length;
        const spans = [];
        socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/sessions/${session.id}/events`);
        const closed = new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Close timeout')), 45000);
          socket.onmessage = ({ data }) => {
            const event = JSON.parse(data);
            if (event.type === 'studio.span') spans.push(event);
            if (event.type === 'session.closed') final = true;
            if (event.type === 'studio.disconnected') { clearTimeout(timer); resolve(); }
          };
        });
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Events timeout')), 10000);
          socket.onopen = () => { clearTimeout(timer); resolve(); };
          socket.onerror = reject;
        });
        await peer.setRemoteDescription({ type: 'answer', sdp: session.sdp });
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Media connection timeout')), 25000);
          const changed = () => {
            if (peer.connectionState === 'connected') { clearTimeout(timer); resolve(); }
            if (peer.connectionState === 'failed') { clearTimeout(timer); reject(new Error('Media failed')); }
          };
          peer.addEventListener('connectionstatechange', changed);
          changed();
        });
        result.media_ms = performance.now() - started;
        await post(`/api/sessions/${session.id}/commands`, { event: { type: 'session.close' } });
        await closed;
        result.final_usage = final;
        result.server_startup = spans.filter(span => span.kind === 'startup' && span.status === 'completed')
          .map(span => ({ name: span.name, duration_ms: span.duration_ms }));
        result.startup_discovery_spans = spans.filter(span => span.kind === 'discovery').length;
      } catch (error) {
        result.error = String(error);
      } finally {
        if (session && !final) {
          try { await post(`/api/sessions/${session.id}/commands`, { event: { type: 'session.close' } }); }
          catch (error) { result.cleanup_error = String(error); }
        }
        socket?.close();
        peer.close();
        source.stop();
        destination.stream.getTracks().forEach(track => track.stop());
        await audio.close();
        if (auth.required) {
          try { await post('/api/auth/logout', {}); }
          catch (error) { result.cleanup_error = String(error); }
        }
      }
      return result;
    }, credentials);
    results.push(result);
    console.log('STARTUP', JSON.stringify(result));
    await page.close();
  }
  fs.mkdirSync(path.join(root, 'output'), { recursive: true });
  fs.writeFileSync(path.join(root, 'output', outputName), JSON.stringify(results, null, 2));
  if (results.some(result => result.error || result.cleanup_error || !result.final_usage)) throw new Error('Startup benchmark failed');
} finally {
  await browser.close();
}
