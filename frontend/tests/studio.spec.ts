import { expect, test, type Page, type WebSocketRoute } from '@playwright/test';

const config = {
  csrf: 'test-csrf-not-a-secret', configured: true, endpoint: 'https://models.example.test',
  deployments: { live: 'live-test', responses: 'responses-test', decision: 'decision-test' },
  defaultConfig: {
    model: 'live-test', instructions: '语音助手初始提示',
    audio: { output: { voice: 'marin' } },
    delegation: { type: 'responses', responses: {
      model: 'responses-test', instructions: '业务规则', max_output_tokens: 1024,
      service_tier: 'auto', reasoning: { effort: 'low', summary: 'auto' }, text: { verbosity: 'low' },
      tools: [{ type: 'function', name: 'save_customer', parameters: { type: 'object' } }],
      tool_choice: 'auto', parallel_tool_calls: true, future_setting: { preserve: true },
    } },
  },
  mcp: [
    { name: 'Docs', url: 'https://mcp.example.test', configured: true, tools: [{ name: 'lookup_docs' }] },
    { name: 'verification', url: 'https://verification.example.test', configured: true, tools: ['save_customer'] },
  ],
  threshold: 0.8, maxSessionSeconds: 180,
};

async function setup(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/auth', route => route.fulfill({ json: {
    required: false, authenticated: true, csrf: config.csrf, username: null,
  } }));
  await page.route('**/api/config', route => route.fulfill({ json: config }));
  return errors;
}

test('login protects the workspace and supports wrong-password feedback and logout', async ({ page }) => {
  let authenticated = false;
  let configReads = 0;
  await page.route('**/api/auth', route => route.fulfill({ json: {
    required: true, authenticated, csrf: config.csrf, username: authenticated ? 'admin' : null,
  } }));
  await page.route('**/api/config', route => {
    configReads++;
    return route.fulfill({ json: config });
  });
  await page.route('**/api/auth/login', async route => {
    const body = route.request().postDataJSON();
    expect(route.request().headers()['x-studio-csrf']).toBe(config.csrf);
    if (body.password !== 'only-a-test-password') {
      await route.fulfill({ status: 401, json: { error: 'Incorrect username or password' } });
    } else {
      authenticated = true;
      await route.fulfill({ json: { required: true, authenticated: true, csrf: config.csrf, username: 'admin' } });
    }
  });
  await page.route('**/api/auth/logout', async route => {
    authenticated = false;
    await route.fulfill({ json: { authenticated: false } });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toHaveCount(0);
  expect(configReads).toBe(0);
  await page.getByLabel('用户名', { exact: true }).fill('admin');
  await page.getByLabel('密码', { exact: true }).fill('incorrect');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Incorrect username or password');
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('');
  await page.getByLabel('密码', { exact: true }).fill('only-a-test-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'admin · 退出登录' }).click();
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toHaveCount(0);
});

test('a stale authentication poll cannot reopen the workspace after a 401', async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  let pollReplied = false;
  let releasePoll!: () => void;
  const held = new Promise<void>(resolve => { releasePoll = resolve; });
  await page.route('**/api/auth', async route => {
    const call = ++calls;
    if (call === 2) await held;
    await route.fulfill({ json: {
      required: true, authenticated: call <= 2, csrf: config.csrf, username: call <= 2 ? 'admin' : null,
    } });
    if (call === 2) pollReplied = true;
  });
  await page.route('**/api/config', route => route.fulfill({ json: config }));
  await page.route('**/api/sessions', route => route.fulfill({ status: 401, json: { error: 'Login required' } }));
  await page.goto('/');
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toBeVisible();
  await page.clock.fastForward(30000);
  await expect.poll(() => calls).toBe(2);
  await page.getByRole('button', { name: 'WebSocket PCM 流' }).click();
  await page.getByLabel('仅文件输入 · 不请求麦克风').check();
  await page.getByRole('button', { name: '开始会话', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible();
  await expect.poll(() => calls).toBe(3);
  releasePoll();
  await expect.poll(() => pollReplied).toBe(true);
  await page.clock.runFor(100);
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toHaveCount(0);
});

test('managed workflow starts with one tool, refreshes directories and preserves effective tools on updates', async ({ page }) => {
  const errors = await setup(page);
  const managed = {
    ...structuredClone(config), managed_tools: true,
    catalogs: [{ server: 'faq', state: 'unloaded', version: null, expires_in_seconds: 0, tools: [] }],
    mcp: [{ name: 'faq', url: 'https://faq.example.test/mcp', configured: true, tools: ['faqSearch'] }],
  };
  managed.defaultConfig.delegation.responses.tools = [{ type: 'function', name: 'classify_intent', parameters: { type: 'object' } }];
  managed.defaultConfig.delegation.responses.tool_choice = 'required';
  await page.route('**/api/config', route => route.fulfill({ json: managed }));
  let socket: WebSocketRoute | undefined;
  const commands: Record<string, unknown>[] = [];
  await page.routeWebSocket('**/api/sessions/managed/events', ws => { socket = ws; });
  await page.route('**/api/sessions', async route => {
    const body = route.request().postDataJSON();
    expect(body.session.delegation.responses.tools.map((tool: { name: string }) => tool.name)).toEqual(['classify_intent']);
    await route.fulfill({ json: { id: 'managed', session: body.session } });
  });
  await page.route('**/api/mcp/catalog/refresh', async route => {
    expect(route.request().headers()['x-studio-csrf']).toBe(managed.csrf);
    expect(route.request().postDataJSON().servers).toEqual(['faq']);
    await route.fulfill({ json: { catalogs: [{ server: 'faq', state: 'valid', version: 'v1', expires_in_seconds: 300, tools: ['faqSearch'] }] } });
  });
  await page.route('**/api/sessions/managed/commands', async route => {
    const { event } = route.request().postDataJSON();
    commands.push(event);
    if (event.type === 'session.close') socket!.send(JSON.stringify({ type: 'session.closed', usage: {} }));
    await route.fulfill({ json: { sent: true } });
  });
  await page.goto('/');
  await expect(page.getByLabel('tools · JSON', { exact: true })).toBeDisabled();
  await page.locator('.resource-detail').filter({ hasText: 'https://faq.example.test' }).locator('summary').click();
  await page.getByRole('button', { name: '刷新目录', exact: true }).click();
  await expect(page.getByText('目录 · valid / v1', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '同步 JSON', exact: true }).click();
  const editor = page.getByLabel('完整会话参数 JSON');
  const session = JSON.parse(await editor.inputValue());
  session.delegation.responses.tools.push({ type: 'function', name: 'faqSearch' });
  await editor.fill(JSON.stringify(session));
  await expect(page.getByRole('alert').filter({ hasText: '由分类工作流托管' })).toBeVisible();
  session.delegation.responses.tools.pop();
  await editor.fill(JSON.stringify(session));
  await page.getByRole('button', { name: 'WebSocket PCM 流' }).click();
  await page.getByLabel('仅文件输入 · 不请求麦克风').check();
  await page.getByRole('button', { name: '开始会话', exact: true }).click();
  await expect(page.getByText('会话进行中', { exact: true })).toBeVisible();
  const effectiveTools = [...session.delegation.responses.tools, { type: 'function', name: 'faqSearch', parameters: { type: 'object' } }];
  socket!.send(JSON.stringify({ type: 'studio.workflow', phase: 'executing', active_delegation: 'd1', queued: ['d2'], visible_tools: ['classify_intent', 'faqSearch'], tool_choice: 'auto', config_version: 2 }));
  socket!.send(JSON.stringify({ type: 'session.updated', session: { delegation: { type: 'responses', responses: { ...session.delegation.responses, tools: effectiveTools, tool_choice: 'auto' } } } }));
  await expect(page.getByText('业务工具执行阶段', { exact: true })).toBeVisible();
  await expect(page.getByText('1 项排队', { exact: true })).toBeVisible();
  session.delegation.responses.reasoning.effort = 'high';
  await editor.fill(JSON.stringify(session));
  await page.getByRole('button', { name: '更新完整 Responses 参数' }).click();
  await expect.poll(() => commands.length).toBeGreaterThan(0);
  const update = commands[0] as { session: { delegation: { responses: { tools: unknown[]; tool_choice: string } } } };
  expect(update.session.delegation.responses.tools).toEqual(effectiveTools);
  expect(update.session.delegation.responses.tool_choice).toBe('auto');
  await page.getByRole('button', { name: '结束会话', exact: true }).click();
  await expect(page.getByText('会话已结束，最终用量已确认。', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('parameter form, JSON, startup representations and mobile themes stay synchronized', async ({ page }) => {
  const errors = await setup(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toBeEnabled();
  await page.getByLabel('Live 模型部署名', { exact: true }).fill('my-live-model');
  await page.getByRole('button', { name: '同步 JSON', exact: true }).click();
  const editor = page.getByLabel('完整会话参数 JSON');
  expect(JSON.parse(await editor.inputValue()).model).toBe('my-live-model');
  const json = JSON.parse(await editor.inputValue());
  json.delegation.responses.reasoning.effort = 'high';
  json.delegation.responses.extra_parameter = { retain_me: 9 };
  await editor.fill(JSON.stringify(json, null, 2));
  await page.getByRole('button', { name: '参数表单', exact: true }).click();
  await expect(page.getByLabel('reasoning.effort', { exact: true })).toHaveValue('high');
  await page.getByLabel('委派启动模式').selectOption('omitted');
  await page.getByRole('button', { name: '同步 JSON', exact: true }).click();
  expect(JSON.parse(await editor.inputValue())).not.toHaveProperty('delegation');
  await editor.fill('{ broken');
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toBeDisabled();
  await editor.fill(JSON.stringify(config.defaultConfig, null, 2));
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '参数表单', exact: true }).click();
  await page.screenshot({ path: 'test-results/studio-desktop-light.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '切换到深色', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: '展开会话参数' }).click();
  await expect(page.getByLabel('Live 模型部署名', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '收起会话参数' }).click();
  await page.screenshot({ path: 'test-results/studio-mobile-dark.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('file-only session streams silence, displays automatic Mock tools and finalizes without duplicate event sources', async ({ page }) => {
  const errors = await setup(page);
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => { throw new Error('File-only mode must not request microphone'); };
  });
  let socket: WebSocketRoute | undefined;
  const audioFrames: { type: string; audio: string }[] = [];
  const commands: Record<string, unknown>[] = [];
  const approvals: boolean[] = [];
  await page.routeWebSocket('**/api/sessions/test-session/events', ws => {
    socket = ws;
    ws.onMessage(raw => { audioFrames.push(JSON.parse(String(raw))); });
  });
  await page.route('**/api/sessions', async route => {
    expect(route.request().headers()['x-studio-csrf']).toBe(config.csrf);
    const body = route.request().postDataJSON();
    expect(body.transport).toBe('websocket');
    expect(body.session.audio).not.toHaveProperty('format');
    await route.fulfill({ json: { id: 'test-session', session: body.session } });
  });
  await page.route('**/api/sessions/test-session/commands', async route => {
    expect(route.request().headers()['x-studio-csrf']).toBe(config.csrf);
    const { event } = route.request().postDataJSON();
    commands.push(event);
    if (event.type === 'session.input_audio.mute') socket!.send(JSON.stringify({ type: 'session.input_audio.muted' }));
    if (event.type === 'session.close') {
      socket!.send(JSON.stringify({ type: 'studio.closing' }));
      socket!.send(JSON.stringify({ type: 'session.closed', usage: { audio_seconds: 4.2 } }));
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    await route.fulfill({ json: { ok: true } });
  });
  await page.route('**/api/sessions/test-session/approvals/*', async route => {
    expect(route.request().headers()['x-studio-csrf']).toBe(config.csrf);
    approvals.push(route.request().postDataJSON().approved);
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'WebSocket PCM 流' }).click();
  await page.getByLabel('仅文件输入 · 不请求麦克风').check();
  await page.getByRole('button', { name: '开始会话', exact: true }).click();
  await expect(page.getByText('会话进行中', { exact: true })).toBeVisible();
  await expect.poll(() => audioFrames.length).toBeGreaterThan(2);
  expect(audioFrames[0].type).toBe('session.input_audio.append');
  expect(Buffer.from(audioFrames[0].audio, 'base64')).toEqual(Buffer.alloc(960));
  const wav = Buffer.alloc(44 + 4800);
  wav.write('RIFF', 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(24000, 24); wav.writeUInt32LE(48000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
  wav.write('data', 36); wav.writeUInt32LE(4800, 40);
  for (let i = 44; i < wav.length; i += 2) wav.writeInt16LE(8192, i);
  const uploadStarted = Date.now();
  await page.locator('input[type=file]').setInputFiles({ name: 'voice.wav', mimeType: 'audio/wav', buffer: wav });
  await expect(page.getByText('发送完成', { exact: true })).toBeVisible();
  expect(Date.now() - uploadStarted).toBeGreaterThanOrEqual(80);
  expect(audioFrames.some(frame => Buffer.from(frame.audio, 'base64').some(value => value !== 0))).toBe(true);
  socket!.send(JSON.stringify({ type: 'session.output_audio.delta', delta: Buffer.alloc(960).toString('base64') }));
  await expect(page.getByText(/原生输出未提供时间戳/)).toBeVisible();
  await expect(page.locator('.error-alert')).toHaveCount(0);
  socket!.send(JSON.stringify({ type: 'studio.span', id: 's1', trace_id: 't1', name: 'response.create', kind: 'responses', start_ms: 100, end_ms: 350, status: 'completed', attributes: { delegation_id: 'd1' } }));
  socket!.send(JSON.stringify({ type: 'session.input_transcript.delta', delta: 'PRIVATE_TRANSCRIPT', start_ms: 0, end_ms: 90 }));
  socket!.send(JSON.stringify({ type: 'response.event', delegation_id: 'd1', event: { type: 'response.output_item.done', item: { output: 'PRIVATE_TOOL_RESULT', type: 'function_call_output' } } }));
  socket!.send(JSON.stringify({ type: 'studio.tool.auto_approved', call_id: 'call-1', name: 'createRepair', mode: 'auto_mock' }));
  socket!.send(JSON.stringify({ type: 'studio.tools', calls: [{ callId: 'call-1', name: 'createRepair', arguments: '[REDACTED]', state: 'submitted' }] }));
  socket!.send(JSON.stringify({ type: 'studio.route', delegation_id: 'd1', intent: 'create_customer', confidence: 0.91, top_probability: 0.74, probabilities: { create_customer: 0.74, other: 0.26 }, allowed_tools: ['save_customer'] }));
  await expect(page.getByRole('region', { name: '工具执行记录' })).toBeVisible();
  await expect(page.getByText('当前 Mock 业务自动执行，仍进行意图、参数和客户资料校验；无需额外口头确认或页面点击。')).toBeVisible();
  await expect(page.getByRole('button', { name: '批准此操作' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '拒绝', exact: true })).toHaveCount(0);
  await expect(page.locator('.tool-records .badge.mock')).toHaveText('Mock 业务工具');
  expect(approvals).toEqual([]);
  await expect(page.getByText('PRIVATE_TRANSCRIPT', { exact: true })).toBeVisible();
  expect(await page.locator('.event-json').innerText()).not.toContain('PRIVATE_TRANSCRIPT');
  expect(await page.locator('.event-json').innerText()).not.toContain('PRIVATE_TOOL_RESULT');
  await page.getByRole('button', { name: /response.create.*responses.*completed/ }).click();
  await expect(page.locator('.detail-duration')).toContainText('250 ms');
  await expect(page.getByLabel('Live 模型部署名', { exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '同步 JSON', exact: true }).click();
  const editor = page.getByLabel('完整会话参数 JSON');
  const session = JSON.parse(await editor.inputValue());
  session.audio.output.voice = 'cedar';
  await editor.fill(JSON.stringify(session));
  await expect(page.getByRole('alert').filter({ hasText: '运行中仅能修改' })).toBeVisible();
  session.audio.output.voice = 'marin';
  session.delegation.responses.reasoning.effort = 'high';
  await editor.fill(JSON.stringify(session));
  await page.getByRole('button', { name: '更新完整 Responses 参数' }).click();
  await expect.poll(() => commands.some(event => event.type === 'session.update')).toBe(true);
  const update = commands.find(event => event.type === 'session.update') as { session: { delegation: { responses: Record<string, unknown> } } };
  expect(update.session.delegation.responses.future_setting).toEqual({ preserve: true });
  expect(update.session).not.toHaveProperty('audio');
  await page.getByRole('button', { name: /委派路由/ }).click();
  await expect(page.getByText('0.9100')).toBeVisible();
  await expect(page.getByText('0.7400')).toBeVisible();
  await page.getByRole('button', { name: '发送命令', exact: true }).click();
  const commandEditor = page.getByLabel('原生命令 JSON');
  const template = JSON.parse(await commandEditor.inputValue());
  expect(template.delegation_id).toBeNull();
  await commandEditor.fill(JSON.stringify({ ...template, content: 'UI protocol test context' }));
  await page.getByRole('button', { name: '发送命令', exact: true }).last().click();
  await expect.poll(() => commands.some(event => event.type === 'session.instructions.append' && event.delegation_id === null)).toBe(true);
  await page.getByRole('button', { name: /协议事件/ }).click();
  await page.evaluate(() => {
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = blob => { (window as unknown as { studioExport: Blob }).studioExport = blob as Blob; return create(blob); };
    HTMLAnchorElement.prototype.click = () => {};
  });
  await page.getByRole('button', { name: '导出脱敏追踪', exact: true }).click();
  const exported = await page.evaluate(async () => (window as unknown as { studioExport: Blob }).studioExport.text());
  expect(exported).not.toContain('PRIVATE_TRANSCRIPT');
  expect(exported).not.toContain('PRIVATE_TOOL_RESULT');
  expect(exported).not.toContain(config.csrf);
  socket!.send(JSON.stringify({ type: 'response.event', event: { broken: true } }));
  await expect(page.getByRole('alert').filter({ hasText: '嵌套 event 缺少有效 type' })).toBeVisible();
  await page.getByRole('button', { name: '关闭错误提示' }).click();
  await page.screenshot({ path: 'test-results/studio-live-trace.png', fullPage: true });
  await page.getByRole('button', { name: '输入静音', exact: true }).click();
  await expect(page.getByRole('button', { name: '取消输入静音' })).toBeVisible();
  await page.getByRole('button', { name: '结束会话', exact: true }).click();
  await expect(page.getByText('会话已结束，最终用量已确认。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '用量', exact: true }).click();
  await expect(page.getByText('4.2', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  expect(errors).toEqual([]);
});

test('native WebSocket microphone worklet produces PCM frames and stops cleanly', async ({ page }) => {
  const errors = await setup(page);
  const inputFrames: Buffer[] = [];
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/api/sessions/test-mic/events', ws => {
    socket = ws;
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw));
      expect(frame.type).toBe('session.input_audio.append');
      inputFrames.push(Buffer.from(frame.audio, 'base64'));
    });
  });
  await page.route('**/api/sessions', route => route.fulfill({ json: { id: 'test-mic', session: config.defaultConfig } }));
  await page.route('**/api/sessions/test-mic/commands', async route => {
    if (route.request().postDataJSON().event.type === 'session.close') socket!.send(JSON.stringify({ type: 'session.closed', usage: { audio_seconds: 1 } }));
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'WebSocket PCM 流' }).click();
  await page.getByRole('button', { name: '开始会话', exact: true }).click();
  await expect(page.getByText('会话进行中', { exact: true })).toBeVisible();
  await expect.poll(() => inputFrames.length).toBeGreaterThan(5);
  expect(inputFrames.every(frame => frame.length === 960)).toBe(true);
  await page.getByRole('button', { name: '扬声器静音', exact: true }).click();
  await expect(page.getByRole('button', { name: '打开扬声器', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: '恢复播放', exact: true }).click();
  await page.getByRole('button', { name: '结束会话', exact: true }).click();
  await expect(page.getByText('会话已结束，最终用量已确认。', { exact: true })).toBeVisible();
  const stoppedCount = inputFrames.length;
  await expect.poll(() => inputFrames.length).toBe(stoppedCount);
  expect(errors).toEqual([]);
});

test('backend response errors are visible rather than silently ignored', async ({ page }) => {
  await setup(page);
  await page.route('**/api/config', route => route.fulfill({ status: 502, contentType: 'text/html', body: '<html>backend unavailable</html>' }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('非 JSON');
  await expect(page.getByRole('button', { name: '开始会话', exact: true })).toBeDisabled();
});

test('native WebRTC negotiates SDP but ignores data-channel events and releases microphone tracks', async ({ page }) => {
  const errors = await setup(page);
  await page.addInitScript(() => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await original(constraints);
      (window as unknown as { studioTestStream: MediaStream }).studioTestStream = stream;
      return stream;
    };
  });
  let socket: WebSocketRoute | undefined;
  const clientFrames: string[] = [];
  await page.routeWebSocket('**/api/sessions/test-rtc/events', ws => {
    socket = ws;
    ws.onMessage(message => clientFrames.push(String(message)));
  });
  await page.route('**/api/sessions', async route => {
    const body = route.request().postDataJSON();
    expect(body.transport).toBe('webrtc');
    expect(body.sdp).toContain('m=audio');
    expect(body.session.audio).not.toHaveProperty('format');
    const answer = await page.evaluate(async (offer: string) => {
      const peer = new RTCPeerConnection();
      (window as unknown as { studioTestPeer: RTCPeerConnection }).studioTestPeer = peer;
      peer.ondatachannel = event => {
        event.channel.onopen = () => event.channel.send(JSON.stringify({
          type: 'studio.span', id: 'duplicate-datachannel', trace_id: 'rtc', kind: 'live', name: 'MUST_IGNORE', start_ms: 0, end_ms: 2, status: 'completed', attributes: {},
        }));
      };
      await peer.setRemoteDescription({ type: 'offer', sdp: offer });
      await peer.setLocalDescription(await peer.createAnswer());
      if (peer.iceGatheringState !== 'complete') await new Promise<void>(resolve => peer.addEventListener('icegatheringstatechange', () => { if (peer.iceGatheringState === 'complete') resolve(); }));
      return peer.localDescription!.sdp;
    }, body.sdp);
    await route.fulfill({ json: { id: 'test-rtc', session: body.session, sdp: answer } });
  });
  await page.route('**/api/sessions/test-rtc/commands', async route => {
    const { event } = route.request().postDataJSON();
    if (event.type === 'session.close') socket!.send(JSON.stringify({ type: 'session.closed', usage: { audio_seconds: 1 } }));
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '开始会话', exact: true }).click();
  await expect(page.getByText('会话进行中', { exact: true })).toBeVisible({ timeout: 20000 });
  socket!.send(JSON.stringify({ type: 'studio.span', id: 'sideband-only', trace_id: 'rtc', kind: 'live', name: 'SIDEBAND_ONLY', start_ms: 0, end_ms: 10, status: 'completed', attributes: {} }));
  await expect(page.getByRole('button', { name: /SIDEBAND_ONLY/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /MUST_IGNORE/ })).toHaveCount(0);
  expect(clientFrames).toEqual([]);
  await page.getByRole('button', { name: '结束会话', exact: true }).click();
  await expect(page.getByText('会话已结束，最终用量已确认。', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { studioTestStream: MediaStream }).studioTestStream.getTracks().every(track => track.readyState === 'ended'))).toBe(true);
  await page.evaluate(() => (window as unknown as { studioTestPeer: RTCPeerConnection }).studioTestPeer.close());
  expect(errors).toEqual([]);
});
