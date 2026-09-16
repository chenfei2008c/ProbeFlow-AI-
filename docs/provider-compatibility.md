# 供应商与音频兼容性

核验日期：2026-09-16。本文记录代码实际实现和测试边界，不代表百炼账户、模型质量或普通话语音已经通过真实验收。

## V1.1 百炼适配

四个角色使用四份独立的 `RoleConfig`：`asr`、`interview`、`tts`、`report`。实时模式缺少任一角色、密钥、模型或基础地址时直接报错；适配层不会创建 OpenAI 凭证，也不会切换到其他模型或供应商。所有付费请求只发送一次，适配层不自动重试。

| 角色 | 当前请求 | 实现的用量映射 | 状态 |
|---|---|---|---|
| interview / report | `POST {compatible_base}/chat/completions`；`qwen-plus` 类模型；`enable_thinking=false`；可选 JSON object 输出 | 输入、输出、缓存及推理 token；响应未提供可识别 usage 时保留为 `unknown` | MockTransport 验证；interview 另有 5 次虚构方案导入成功记录，访谈与报告链路仍待实测 |
| asr | 同一 OpenAI 兼容端点；本地音频以 Data URL/Base64 放入 `input_audio.data`；术语表作为 system 上下文；`language=zh` | token、音频秒数和 request ID | 使用模拟 HTTP 响应验证；尚无真实语音验收记录 |
| tts | `POST {dashscope_base}/services/aigc/multimodal-generation/generation`；`text`、`voice`、`language_type=Chinese` | 计费字符、token 和 request ID | 使用模拟 HTTP 响应及本地 WAV 下载验证；尚无真实普通话播报验收记录 |

方案导入的有限真实调用证据见 [用量及结果记录](verification/study-import-live.json)。这 5 次调用源于初次自动测试继承本机 live 配置，应用估算 0.011061 元；现已隔离常规测试模式，不把它当作完整产品或复杂文件质量验收。

百炼官方当前说明：Qwen3-ASR-Flash 支持 OpenAI 兼容同步调用，单文件不超过 5 分钟及 10 MB；请求可使用 Base64 Data URL，响应 usage 含 `seconds`。实现进一步将原始分段限制在 7.5 MB，使 Base64 请求内容保持在 10,000,000 字节以内。官方说明 Qwen3-TTS-Flash 非流式请求返回一个有效期 24 小时的音频 URL，并按 `characters` 返回用量；输入上限为 600 字符。

参考的官方资料：

- [Qwen-ASR API 参考](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)
- [非实时语音识别模型与限制](https://help.aliyun.com/zh/model-studio/asr-model)
- [Qwen-TTS 非实时 API 参考](https://help.aliyun.com/en/model-studio/qwen-tts-api)
- [语音合成模型列表](https://help.aliyun.com/zh/model-studio/tts-model)
- [OpenAI 兼容文本响应与思考参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)
- [百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)

## 合成音频下载边界

TTS 的第二次 HTTP 请求只下载供应商生成的临时音频，不携带 API 密钥。下载器执行以下限制：

- 只发出 HTTPS 请求。若百炼对精确白名单内的 OSS 主机返回 HTTP URL，则保留路径和签名查询参数并在请求前将协议升级为 HTTPS；其他 HTTP URL 一律拒绝。阿里云 OSS 官方说明 bucket domain 原生支持 HTTPS。
- 主机必须精确匹配代码内的百炼结果 OSS 白名单，不接受子域拼接或任意 `aliyuncs.com` 主机。
- 下载前解析全部地址；任一地址不是公开可路由 IP，或解析失败时拒绝请求。
- 不跟随重定向，连接和读取均有超时，响应体最多 20 MiB。
- 下载后按文件魔数确认 WAV、Ogg、FLAC、MP3、WebM 或 MP4 音频；HTML 和未知内容会被拒绝。

官方 TTS 参考页的示例响应仍展示 `http://dashscope-result-...`。阿里云 [OSS HTTPS 文档](https://help.aliyun.com/en/oss/user-guide/access-oss-by-https-protocol) 说明 bucket domain 默认支持 HTTPS，因此实现只对精确白名单内的 OSS 结果主机在发请求前升级协议，绝不发出 HTTP 下载。上线前仍需使用目标地域账户确认实际主机；新地域必须经官方域名核验后显式加入白名单。

任何请求超时、网络中断、成功响应无法读取、TTS 临时地址不安全或合成完成后下载失败，都抛出 `external_status_unknown=True`。主程序应保留预算预占并要求用户确认后再重试。明确的 HTTP 拒绝会保留已知状态；429 和 5xx 标记为可重试语义，但适配层仍不自行重试。

## 模拟模式

模拟模式不发出网络请求。文本适配器把末条 user JSON 原样交给 `app.interview.mock_response`，ASR 固定返回带“模拟转写”标记的文本，TTS 只返回一段可播放的双音提示声。提示声不是普通话合成，不可用来宣称中文语音已通过。

## 本地音频处理

`probe_audio` 先按文件魔数识别允许的容器，再强制 FFprobe 使用对应 demuxer 读取真实音轨和解码时长，不相信扩展名，并计算 SHA-256。当前识别 WebM/Matroska、Ogg、WAV、MP4/M4A、MP3、FLAC、AAC、AMR、AIFF 和 AVI 中的音频轨。播放列表和未知容器在启动 FFprobe 前被拒绝；所有 FFmpeg/FFprobe 输入只允许 `file`/`pipe` 协议并显式禁用网络、concat、crypto 和 data 协议，MOV 外部数据引用也被关闭。

MediaRecorder 生成的流式 WebM 可能没有容器或音轨 duration。遇到这种情况时，`probe_audio` 用同一套协议和 demuxer 限制做有界解码，从 FFmpeg progress 得到真实时长；解码最多运行到 600.1 秒，任何超过 10 分钟的单轮录音都会被拒绝。

`prepare_asr_segments` 使用 FFmpeg：

- 先解码最多 30 秒信号，以约 -50 dBFS 阈值拦截明显空录音，并以高过零率和稳定能量组合拦截明显的持续白噪声。
- 通过 `silencedetect` 寻找至少 0.3 秒的停顿；分段时优先选目标上限之前最近的停顿中点，没有合适停顿才硬切。
- 每段分别编码为单声道、16 kHz、16-bit PCM WAV，再次 FFprobe，确认可独立解码、时长不超过调用上限且 Base64 后不超过 10,000,000 字节。
- 上传块按调用者给定顺序写入同目录临时文件，刷新并 `fsync` 后原子替换目标。

本机自动测试使用 FFmpeg/FFprobe 9.0.1，覆盖假扩展名、非法媒体、静音、稳定白噪声、停顿切分、每段解码参数、字节边界、哈希和原子组装。该启发式只能拦截明显无声或稳定噪声，不能判断音频是否包含可理解的人声；真实设备、口音、背景噪声和普通话质量仍需按规格进行真人测试。

## 尚未实测

- 未持有百炼凭证，三类真实模型请求、账户地域、工作空间专属域名、实际计费字段和临时音频 URL 均未实测。
- 未进行普通话音色试听、带口音识别、业务术语识别、真实延迟或费用对账。
- 未在 iPhone Safari、Android Chrome 或实际 MediaRecorder 分块上验证容器组合。
- 未进行 60 分钟访谈和长回答的真实供应商联调。

## 配置变化、同意与价格

同意说明版本由 V1.1 文案版本、模拟／真实模式、四个角色的模型、地区与服务地址共同确定。受访者同意时保存当时的处理配置快照；密钥不进入快照。改变上述处理配置会使旧同意失效，后端在任何外部请求之前拒绝继续，页面要求重新主动确认。只轮换密钥不会更换说明版本。供应商 URL 不允许包含凭证、查询参数或片段。

`PROVIDER_TIMEOUT_SECONDS` 控制请求超时（默认 45 秒，范围大于 0 且不超过 120）；TTS 下载另限制为不超过 20 秒。超时仍按可能已执行或计费处理，不自动再次付费。

默认价格只是规格中的 2026-09-15 北京地区样例，适用于默认三类模型，**不是最新报价承诺**。更换模型或地区后，必须配置匹配的 `PRICE_OVERRIDES` 才能执行付费请求。访谈与报告的价格可独立配置；未知配置不会套用便宜模型的单价。

在本机 `.env` 中将 `PRICE_OVERRIDES` 写为单行 JSON。以下数字仅为虚构格式示例，不能直接用于真实计费：

```dotenv
PRICE_OVERRIDES='{"report":{"model":"your-report-model","region":"cn-beijing","version":"your-verified-price-version","source":"your-verified-price-source","input_per_million":"10","cached_per_million":"2","output_per_million":"20"}}'
```

每个角色都需要 `model`、`region`、`version`、`source`，单价采用非负有限数值字符串，单位为人民币：

| 角色 | 单价字段 |
|---|---|
| interview、report | `input_per_million`、`output_per_million`、`cached_per_million`，元／百万 token |
| asr | `asr_per_second`，元／输入音频秒 |
| tts | `tts_per_10000`，元／万计费字符；百炼汉字双字符口径 |

每个请求预占预算时冻结其模式、模型、地区和价格快照；返回、超时或会话删除后的财务结算均使用这份快照。后续修改配置不改变已经发起请求的结算口径。用量账本仍区分实际用量、估算和未知；它不是供应商最终账单。
