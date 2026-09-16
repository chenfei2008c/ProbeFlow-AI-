# V1.1 实现契约

浏览器与 FastAPI 同源。所有写请求携带 `X-ProbeFlow-Client: web` 和随机 `Idempotency-Key`；重试同一业务请求复用该键。JSON 错误：`{code,message,retryable,request_id}`。认证使用分开的 HttpOnly 管理员／受访者 Cookie，不在 localStorage 存 token。邀请原文只在创建时返回，以 `/join#token=...` 兑换后立即移除地址栏片段。

## 前端 API

- `GET /api/config` → `{mode,consent_version,providers:{asr,interview,tts,report},admin_initialized}`。
- `POST /api/admin/login {password}`；`POST /api/admin/logout {}`；`GET /api/admin/me` → `{id}`。
- `GET /api/admin/studies` → 数组；`POST /api/admin/studies` → Study；`GET /api/admin/studies/{id}` → Study；`POST /api/admin/studies/{id}/versions` → Study。研究配置：`{title,objective,participant_description,target_minutes,topics:[{id,title,research_question,priority,evidence_type,minutes}],exclusions,glossary:string[],budget_cny,confirm_transcript:true,tone}`，language 固定 zh-CN。Study：`{id,title,archived,current_version_id,version_number,version:<上述配置>,session_count,completed_count,total_cost_cny,updated_at}`。
- `POST /api/admin/studies/{id}/outline` 输入上述研究配置 → `{job_id}`；`GET /api/admin/jobs/{id}` → Job，提纲结果位于 result，人工编辑后发布。
- `POST /api/admin/studies/{id}/archive {archived}`；`DELETE /api/admin/studies/{id}`。
- `POST /api/admin/studies/{id}/invites {}` → `{id,url,expires_at}`；`POST /api/admin/sessions/{id}/recovery-invite {}` → `{url,expires_at}`，绑定原会话并撤销旧凭证。
- `GET /api/admin/studies/{id}/invites` → `{id,version_number,session_id,status,created_at,expires_at,redeemed_at,revoked_at}` 数组，不返回 token／hash；status 为 available/redeemed/revoked/expired。`POST /api/admin/studies/{id}/invites/{invite_id}/revoke {}` 仅撤销未兑换邀请，已兑换返回 INVITE_USED，凭证轮换使用恢复邀请；档案不受影响。
- `GET /api/admin/studies/{id}/sessions` → Session 数组；`GET /api/admin/sessions/{id}` → Detail；`DELETE /api/admin/sessions/{id}`。
- `POST /api/admin/sessions/{id}/reports {retry_job_id?,accept_possible_charge?:false}` → `{job_id}`；空对象新建报告，指定 retry_job_id 重试本场失败报告并复用来源快照及成功检查点。`POST /api/admin/sessions/{id}/budget {budget_cny}`；`POST /api/admin/turns/{id}/revision {text}`。
- `GET /api/admin/sessions/{id}/export?format=json|csv|markdown` → 附件。
- `GET /api/admin/usage` → `{month_spent_cny,month_reserved_cny,monthly_limit_cny,entries:[]}`；`GET /api/admin/diagnostics` → `{mode,providers,storage,ffmpeg_available,recent_errors,backup}`；`POST /api/admin/diagnostics/tts {text}` → `{job_id}`。
- `POST /api/participant/exchange {token}` → `{session_id}`；`GET /api/participant/session` → Detail。
- `POST /api/participant/consent {version,mode:"text"|"voice",processing:true,permanent:true}` → `{session_id,job_id?}`。文案包含供应商、保存规则、结束／撤回区别；两项由人主动勾选。
- `POST /api/participant/turns {input_mode:"text"|"voice",text?,mime_type?}` → `{turn_id,job_id?}`。文字回答进入 confirming；语音返回上传轮次。
- `PUT /api/participant/turns/{id}/chunks/{seq}` → 二进制 body；请求 `X-Chunk-SHA256`；返回 `{seq,sha256}`。
- `POST /api/participant/turns/{id}/finalize {chunks:[{seq,sha256}]}` → `{job_id}`。
- `POST /api/participant/turns/{id}/confirm {text}` → `{job_id}`。
- `POST /api/participant/control {action:"pause"|"resume"|"skip"|"end"|"withdraw"|"extend"|"retry"|"playback_done"|"rerecord",reason?,job_id?,accept_possible_charge?,turn_id?,played_complete?}` → 会话／任务状态。结束需用户确认；撤回需独立二次确认；播放被打断需记为未完整播放。
- `POST /api/participant/heartbeat {}` → `{active_seconds,status}`；`GET /api/participant/events?after=0` → `{events:[{seq,type,payload,created_at}],cursor}`；`GET /api/media/{id}` → 鉴权音频。

Session：`{id,study_id,status,participant_code,mode,consent_version,processing_consent,permanent_consent,active_seconds,target_seconds,budget_cny,spent_cny,reserved_cny,pause_reason,created_at,ended_at,retention:"permanent"}`。

Detail：`{session:<Session>,study:<研究配置>,turns:[Turn],reports:[Report],jobs:[Job],memory:{topics:[],unresolved:[]},mode:"mock"|"live",consent_version,providers}`。受访者 Detail 不含报告、管理员修订操作、费用或内部记忆；界面仅管理端渲染这些内容。

Turn：`{id,seq,role:"assistant"|"participant",status,input_mode,text,revision_id,revisions:[{id,text,source,created_at}],audio_asset_id?,audio_status?,action?,topic_id?,created_at,confirmed,played_complete?}`。Report：`{id,version,status,source_updated,body:<结构化报告>,markdown,citations:[],created_at}`。Job：`{id,kind,status,error_code?,error_message?,result?,created_at}`，status 为 queued/running/succeeded/failed/external_status_unknown/cancelled。

## 纯 Python 模块

### providers（独立，不导入数据库／应用配置）

`RoleConfig` dataclass: provider="bailian", base_url, model, api_key (repr=False), region="cn-beijing", voice="Cherry"；`ProviderSuite(mode, configs:dict[str,RoleConfig])`。

异步 `text(role, messages, *, json_mode=True, max_tokens=2048) -> TextResult(text,usage)`；`transcribe(path:Path, glossary:list[str]) -> ASRResult(text,segments,usage)`；`synthesize(text) -> AudioResult(content:bytes,mime_type,usage)`。

`Usage` dataclass: input_tokens/output_tokens/cached_tokens/reasoning_tokens:int=0, audio_seconds:float=0, characters:int=0, request_id:str|None=None, source="actual"|"estimated"|"unknown"。`ProviderError(code,message,external_status_unknown=False,retryable=False)` 禁止携带密钥／敏感请求。

mock 文本读取末条 user JSON 的 task：outline/decide/report/report_merge；由 `app.interview` 生成确定性结果，providers 可延迟导入 `mock_response(payload)`；mock ASR 文本固定标为模拟，mock TTS 只返回可播放提示音，严禁声称中文合成通过。

### audio（独立，不导入数据库）

`probe_audio(path)->AudioInfo(mime_type,duration_seconds,byte_size,sha256)`；`prepare_asr_segments(source:Path,output_dir:Path,max_seconds=180)->list[AudioSegment(path,start_seconds,end_seconds)]`，FFmpeg 探测、格式真实性／时长校验、静音检测，单声道16k PCM WAV，优先停顿处拆分并校验时长／大小；`assemble_chunks(paths,target)` 顺序原子写入。纯函数错误使用 `AudioError(code,message)`。

### interview（独立，不导入数据库）

`build_context(study:dict,turns:list[dict],memory:dict,active_seconds:int)->dict` 保留当前完整回答、全部禁问范围和拒绝事项，限制上下文；`decide_messages(context)->list[dict]`；`validate_decision(raw:str|dict,context:dict)->dict`，非法值抛 `DecisionError`；`fallback_decision(context)->dict`；`mock_response(payload:dict)->dict` 处理 task。

decision 使用规格 action/topic_id/question/basis_turn_ids/coverage_update/new_evidence/unresolved_items/boundary；有效 ID 来自 context，不要求思维链。context 的 turns 每条有 id,role,text,revision_id,topic_id,action,confirmed。报告输入全部确认来源及问题上下文；`report_messages(study,turns)->list[dict]`；`report_merge_messages(study,reports)->list[dict]`；`validate_report(raw,turns)->dict`；`render_report(report,mode)->str`；`mock_report(study,turns)->dict`。模型报告结构 `{summary,findings:[{type:"statement"|"opinion"|"hypothesis",section:"role"|"event"|"statement"|"explanation"|"hypothesis"|"suggestion",text,citations:[{turn_id,revision_id,start,end,quote}]}],limitations:[],unanswered:[]}`。角色与事件只能标为受访者陈述，原因解释与建议为意见，假设独立分类。引用只能来自已确认的受访者，精确匹配指定版本及字符范围，禁止引用 AI 问题或拼接伪引文；生成报告正文使用纯文本／Markdown，不信任 HTML。

预算在主程序实施，providers 只返回真实或明确估算用量，不自行重试付费请求。全部外部超时归为状态未知，重试需用户明确确认；schema 最多修复一次并分别记账。

## 运行边界补充

- 所有 API 写请求按实际接收字节限流，最多 12 MiB；不依赖 Content-Length。
- 达到目标时长返回 TARGET_TIME_REACHED，必须主动 extend 才能开始新回答；90 分钟后不再延长。正在进行的回答仍可提交。
- 未完成的 recording/uploading 轮次阻止 end，返回 UPLOAD_INCOMPLETE；暂停后的恢复上传须先 resume。
- ASR 失败／未知时允许确认同一轮的手工文字，不触发再次识别；未知费用仍预占。
- audio_status 区分 available、missing、unplayable；已提交但校验失败的原始音频仍保存，duration_seconds 为 null。
- Markdown 导出包含报告、逐字稿和全部文本修订，并将外部文本中的 HTML／Markdown 活动内容转义。JSON 保存原文，CSV 中公式前缀转义。
- 诊断 providers.configured 表示凭证是否配置；live 的 available 为 null，不把凭证存在当作连通验证。
- 研究级 outline 任务记录 study_id，研究删除和备份恢复过滤覆盖这些任务及相关幂等响应。
- consent_version 为文案版本加处理配置摘要；模拟／真实切换、角色模型／地区／地址改变时失效。每条 Consent 和当前 Session 保留不含凭证的处理快照。
- /api/media/{id} 在回放前校验完整文件哈希；损坏返回 ARCHIVE_CORRUPTED，文字仍可访问与导出。
- PRICE_OVERRIDES 按四角色独立配置；付费请求预占与结算使用同一个不可变价格和模式快照，未知模型／地区返回 PRICE_NOT_CONFIGURED。

## 永久档案来源与补充授权

- Session 新增 `study_version_id` 和 `expires_at:null`。管理端 Detail 新增 `study_version:{id,number,prompt_version,created_at,retention}` 及按时间排列的 `consents:[{id,version,mode,processing,permanent,retention,created_at,snapshot}]`；snapshot 为当时保存的不含凭证的处理配置。旧记录缺少快照时返回原有空对象，不按当前配置推断。
- JSON 完整导出包含上述研究版本和全部同意历史，顶层 `prompt_version` 使用该场冻结研究版本的值。管理端可展开历史记录核对；受访者 Detail 不提供管理端同意历史列表。
- 当前 `Detail.mode` 仅描述运行环境。`Detail.archive_mode` 和 `Report.archive_mode` 描述档案来源，可为 mock/live/mixed/unknown；未知来源不能按当前环境推断为真实。
- 文本修订、音频资产、报告持久保存 `provenance: {mode,calls?:[{mode,provider,model,region,role}],source_modes?:[]}`。外部调用来源与检查点一起保存，重试复用检查点时不改写。后续文字修订和报告保留输入来源标识。
- Turn 返回当前文本 `provenance`、全部 revisions 的 `provenance` 以及可为空的 `audio_provenance`；Report 返回自身 `provenance`。旧数据库缺少这些字段的档案迁移为来源未确认，不补造历史信息。
- JSON 导出的 `mode` 是档案来源，`runtime_mode` 才是导出时运行环境；来源服务由各条 provenance.calls 记录，不用当前 providers 代替历史供应商。CSV 每个修订带来源标识，Markdown 含总标识及各版本标识。
- `Report.citations` 返回持久引用 `id`，并保留 turn_id、revision_id、start、end、quote；模型报告 body 的引用结构不变，模型不生成数据库 ID。
- CSV 前 9 列保持原顺序，后续列补充研究 ID／标题／版本、提示词版本、保存的模型调用、确认标记、是否当前修订、前一修订、引用索引及导出时间。引用索引为 JSON 数组，每项关联持久引用 ID、报告 ID／版本／时间／来源和精确文本范围。所有外部输入列均执行公式防护。
- Markdown 展示最新报告正文、全部报告的版本与引用索引及全部文本修订；历史模型缺失时明确“未记录具体模型”，不使用当前配置替代。新增标题、模型和引用元数据与原文一样转义。JSON 继续作为包含全部报告正文的完整导出。
- 已完成场次处理配置改变后，既有档案仍可读取、播放及导出，新处理须受访者补充同意。沿用 `/api/participant/consent`，必须保留原输入方式，两项主动勾选；成功只记录新版授权，不重开访谈、不创建下一问、不自动重试旧任务。受访者可通过原凭证或恢复邀请进入结束页查看更新说明。

## 主题覆盖与报告来源

- 管理端 Detail 新增 `coverage:{topics:[{id,title,status,confirmed_turn_ids,unconfirmed_count,reasons}],unresolved:[{text,turn_ids}]}`；受访者响应不含此字段。status 为 not_started/awaiting_answer/partial/covered/skipped。covered 必须有对应主题的确认回答、匹配当前修订的证据和覆盖至当前回答的评估，页面注明这是模型判断；跳过／拒答独立展示。
- Turn 新增 `text_source`，描述当前文本修订来源。新报告 body 在模型结构之外由程序加入 `schema_version:2`、`background:{title,objective,study_version_id}`、`coverage`；旧报告仍可读取，未保存的快照不补造。
- 报告首次处理将研究、轮次、来源修订号及观察局限冻结到任务内部；普通响应不暴露该内部快照。重试保持相同输入并复用已完成检查点，后续修改使报告 `source_updated` 为 true。结束或跳过也更新会话修订号。
- 长报告每段提取后进行分层汇总；引用既须匹配原文，又须完整复用前一级已有引文。无效结果仅允许一次结构修复，修复仍失败返回 REPORT_INVALID，不写入正式报告。汇总材料超过当前 64,000 字符保护上限返回 REPORT_CONTEXT_TOO_LARGE，档案保留且不截断；旧任务缺少冻结来源时返回 REPORT_SOURCE_SNAPSHOT_MISSING，应创建新报告。
- 人工重试 REPORT_INVALID 时，只移除失败步骤及其修复结果的内部检查点；成功步骤与来源快照保留，既有费用账本不改写，重新请求继续独立记账。未知计费重试仍须明确确认。
- 管理端重试状态未知报告，或存在状态未知报告时另建报告，服务端必须收到 `accept_possible_charge:true`，否则返回 CHARGE_CONFIRMATION_REQUIRED。检查不依赖页面内存，刷新后仍有效。重复幂等请求返回同一任务；存在排队／执行中的报告时返回 REPORT_BUSY，不追加任务。请求事件保留是否重试及费用确认结果，原未知账本不改写。
- 管理页从持久任务列表恢复处理中报告的轮询；失败报告可在处理记录中重试，未知费用的勾选默认未选。请求进行中不允许重复生成。
- 重启恢复将中断的在途请求标为 external_status_unknown 并记录失败事件。报告失败不阻止已结束场次成为 completed；访谈中其他处理请求状态未知则暂停场次。已持久保存的响应检查点继续用于后续处理，不重复调用供应商。
- 研究局限由程序加入提前结束、未确认文本、文字输入、音频文件缺失／哈希损坏等实际观察，位于报告前部。永久保存不存在正常的“录音到期”分支。每一类发现缺乏依据时明确显示尚缺乏依据。
