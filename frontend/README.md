# GPT-Live Studio frontend

React 19 + TypeScript + Vite；中文优先的 `/openai/v1/live` 调试工作台，不是 Realtime UI。所有服务端通信均同源；不在浏览器接收 API 密钥，也不使用 localStorage / IndexedDB 保存对话、音频或配置。

```sh
npm install
npm run dev
npm run check
npm test
npm run build
npm run test:ui
```

开发时 `/api`（包括 WebSocket）和 `/healthz` 代理到 `127.0.0.1:8000`。生产构建在 `dist/`，静态资源使用相对路径，由 Python 后端提供。浅色/深色使用 Clawpilot 原始主题变量，可用 `?scoutTheme=dark` / `light`，切换按钮仅影响当前页面。

## 后端约定

- `GET /api/auth` 返回登录状态和 CSRF bootstrap；`POST /api/auth/login` 校验后端加盐密码哈希并轮换 Cookie；`POST /api/auth/logout` 撤销登录及当前 owner 的连接。前端不存储/硬编码密码，登录之前不会加载配置或挂载音频工作台。
- 认证开启时所有其他 API / WebSocket 都由后端强制保护。HTTP 401 会回到登录；注销/到期会释放浏览器媒体资源。云端 Cookie 为 Secure / HttpOnly / SameSite=Strict。

- `GET /api/config` 返回 `csrf`、`configured`、`endpoint`、`deployments`、完整 `defaultConfig`、`mcp`、`threshold`、`maxSessionSeconds`、`managed_tools` 和目录 `catalogs`。默认 Responses 仅包含 classify_intent；读取配置和创建会话均不发现业务 MCP。
- `POST /api/sessions` 接收 `{session, transport, sdp?}`，返回 `{id, session, sdp?}`。创建请求必须已经建立 Live 会话；浏览器不再发送 `session.start`。Studio 的会话 DTO **不接收 `audio.format`**；WebSocket 音频由后端传输层固定为无头 PCM16LE / 24 kHz / mono，WebRTC 通过 SDP 协商。
- 所有 POST 携带 `X-Studio-Csrf`。事件连接为同源 cookie WebSocket `/api/sessions/{id}/events`；只向它发送 `{type:"session.input_audio.append", audio:"<base64>"}`。服务端需在连接时缓冲 / 重放创建期间事件，避免丢失 `session.started` 等帧。
- `POST /api/sessions/{id}/commands` 接收 `{event, experimental?}`。工作台使用原生 `session.close`、`session.input_audio.mute` / `unmute` 和 `session.update`。运行中更新完整 `delegation.responses` 对象，其他启动配置锁定；tools/tool_choice 由服务端阶段管理，只能查看，其他额外 Responses 字段保留。用户更新生成参数时合并当前有效托管字段，不把启动的一个分类工具覆盖到正在执行的业务阶段。
- `POST /api/mcp/catalog/refresh` 只接受已配置服务器名称并要求 owner/Origin/CSRF。资源区的“刷新目录”按钮更新目录状态，不更改活动任务的 Schema 快照，不接受任意 URL。目录默认有效 300 秒，只有工具定义被缓存。
- `session.instructions.append` / `session.commentary.append` / `session.thinking.append` 等 append 命令不关联委派时，需要显式传 `delegation_id: null`，不能省略。命令编辑器默认模板已包含该值。
- 当前 Mock 业务自动执行，不再提供页面批准/拒绝或 approvals API。服务端仍校验意图、参数、可信会话 ID 和所需客户资料；收集字段的语音追问不属于人工审批。
- 事件为原始 JSON 对象，支持 `{type:"response.event",delegation_id,event:{type:…}}`；转写使用 `session.input_transcript.delta` / `session.output_transcript.delta`，输出音频使用 `session.output_audio.delta`。音频/转写偏移来自模型服务，`studio.span` 来自 Python 单调时钟，起点不同，不能跨时钟相减；瀑布只采用 Python span 时钟。Responses 文本独立标注，不混作 Live 语音。
- `studio.span`、`studio.tool.auto_approved`、`studio.route` 和 `studio.tools` 的属性在顶层。`studio.tool.auto_approved` 只是自动允许执行的记录，不表示 MCP 成功，也不会触发浏览器发送批准请求。`studio.tools.calls` 是当前调用快照，`probabilities` 支持意图到概率的对象或 `{intent,probability}` 数组。Decision 的 `confidence` 与 `top_probability` 分别显示。`studio.closing` 表示服务端进入关闭流程。`session.usage.updated.usage` / `session.closed.usage` 是累计快照，不求和。`session.closed` 或 `studio.disconnected.final_usage_confirmed=true` 才表示最终确认。
- WebRTC 原生 offer/answer、音频 track 与 data channel；**不监听 data channel 事件**，唯一事件源是服务端 sideband，因此没有重复事件。
- `studio.workflow` 展示分类、按需发现、更新确认、执行、恢复和排队状态，以及当前有效工具和缓存版本；服务端保留每会话串行协调，不暂停语音。`session.updated` 更新有效 Responses 快照，但不会覆盖用户的提示词编辑。
- `studio.browser_startup` 与后端 startup span 分别计时。前端只有媒体 connectionState=connected 后才标记 WebRTC 会话就绪；HTTP 请求包含后端凭据/创建/sideband 时间，不能重复相加或跨机器相减。

## 音频与安全边界

- 麦克风需要 HTTPS / localhost、权限以及 AudioWorklet。WebSocket 采集单声道、无头 PCM16LE 24 kHz，每帧 20 ms；硬件采样率通过 worklet 重采样。WebSocket 播放按服务器 `start_ms` 调度，保留时间间隙；仅提供 `end_ms` 时减去 PCM 时长得到起点。原生主连接分片没有任何时间戳时，显式标注兼容模式并按到达顺序播放，此模式不能精确还原服务端间隙。不把反射时间误称为用户听到声音的时间。
- 文件限 20 MB / 120 秒，浏览器解码后用 OfflineAudioContext 下混 / 重采样，按 20 ms 实时节奏发送；发送期间暂停麦克风输入。仅文件模式不请求麦克风，在文件之间发送 20 ms 静音帧。后台标签页计时器可能被浏览器节流；不会以突发加速补发。
- 模型输入静音经服务端命令控制，**不是设备隐私开关**；麦克风媒体仍可到达服务端反射路径。结束会话才释放麦克风。扬声器静音仅影响本地播放。输出设备选择依赖浏览器 `setSinkId` 支持；不支持时明确报错，系统默认输出仍可用。
- 业务函数明确标为 **Mock**，即使它们经真实 MCP 连接暴露，也不会误标成真实业务数据；`classify_intent` 调用的 Decision 模型与 MCP 传输连接是真实的。客户资料保存 / 创建 / 取消在校验通过后自动执行，无需额外口头确认或页面点击；不能把“自动允许”显示成“操作成功”。
- 调试事件最多 800 帧，转写最多 3000 片段；调试缓存剔除音频、SDP、转写正文、工具参数与嵌套工具输出，原始转写仅在实时对话面板可见。导出是脱敏 JSON：进一步移除提示、URL、凭据及任意文本属性。原始屏幕上的转写仍可能含个人信息，请避免分享截图。
- 连接异常不自动重连或重建，避免重复会话；关闭会话等待最终事件至多 10 秒，超时明确标注用量未确认。关闭浏览器不保证服务端最终结算，后端应同时执行时长上限和连接清理。

`npm test` 覆盖参数往返 / 锁定、四种委派模式、追踪耗时、嵌套事件、自动允许与执行结果的区分、累计用量、脱敏、PCM、时间间隙以及真实 worklet 文件的重采样。真实模型 / 麦克风 / WebRTC 端到端需要配置好的后端与浏览器设备。

`npm run test:ui` 使用已安装的 Chrome（可用 `STUDIO_BROWSER_CHANNEL` 更改）与 Playwright 模拟 API / WebSocket；验证移动端深色、参数同步、无需点击的工具记录、脱敏导出、最终用量，以及原生 WebRTC SDP 和原生 AudioWorklet 麦克风链路。浏览器测试使用虚拟媒体设备，不访问真实麦克风。测试启动短生命周期的生产预览，完成后自动停止；需要先 `npm run build`。测试不等同真实模型服务端到端验收。

新增 managed-workflow 测试覆盖仅分类默认、原始 JSON 不可绕过、目录刷新与当前有效工具的参数更新。启动基准可执行 `node frontend/scripts/benchmark-startup.mjs`（从项目根目录），使用合成静音媒体而非实际麦克风，会创建少量付费 Live 会话并写入 `output/startup-optimized.json`。

受保护环境的基准使用安全注入的 `STUDIO_USERNAME` / `STUDIO_PASSWORD` 进程环境，不把密码写进命令、仓库或报告；`STUDIO_URL` 指定目标 HTTPS，`STUDIO_BENCHMARK_OUTPUT` 可指定独立 JSON 文件名以保留旧样本。基准完成后关闭 Live 并注销登录。

本机当前无头 Chrome 的实时 Web Audio clock 曾停在约 0.01 秒，即使 state=running，虚拟麦克风 worklet 也没有帧；同一现象在未改动的旧容器镜像上复现。该硬件/浏览器依赖用例不能算通过；其余动态工具、文件、配置和 WebRTC 浏览器路径已通过。本次没有通过放宽断言、伪造 PCM 或改动生产录音代码掩盖它。
