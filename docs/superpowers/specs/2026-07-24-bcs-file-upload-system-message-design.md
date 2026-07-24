# BCS 文件上传系统消息 — 设计文档

- 日期：2026-07-24
- 范围：`src/bcs`（BCS — Bot Coordination Service）
- 方案：复用 `GenericNotification`（Approach C，最小改动）

## 1. 背景与目标

当 bot 或 human 将文件上传到 session 后，触发一条系统消息（system message），按 inject 方式注入给其他 bot，并推送到前端 ws。

消息文本格式（固定）：

```
{prefix} {uploaderName} 上传了一个文件 {fileName} ({fileId}，{readableSize})，下载链接：{downloadUrl}
```

- `prefix`：上传者为 bot → `Bot`；为 human → `用户`
- 示例：`Bot 测试Agent 上传了一个文件 readme.md (THISISFILEID，12 KB)，下载链接：https://bcn.alipay.com/sessions/SESSION_ID/files/THISISFILEID/content`

仅通知（awareness-only），纯文本。不引入结构化元数据字段，不新增前端组件。

## 2. 现状（代码探查结论）

- **上传流程**：HTTP 三段式（prepare → upload bytes → complete）。`complete_upload` 路由处理器 `crates/adapters/http/bcs-http/src/routes/session_files.rs:442-464` 是文件变为 `Ready` 的成功点；底层 service `complete_upload`（`crates/services/bcs-session-file/src/service.rs:356-421`）仅翻转 `Pending→Ready`，不发任何事件。Human 与 bot 走同一路径，仅 `SessionFile.owner.actor_kind` 不同。
- **系统消息机制**：`SystemMessageService::notify(group_id, event, session_id, participants)` 为门面 → `SystemMessageDispatcherImpl::dispatch` 执行：producer 产出 `Vec<SystemGroupMessage>` → 持久化到 `bcs_messages`（`sender_type=system`）→ 按 `DeliveryType` 投递给 bot（`Inject` = `chat.inject`，`deliver:false`，静默观察；`from="bcs-system-message"`）→ 向前端 ws 发布一条聚合事件（`role:"system"`，`bot_uuid:"bcs-system-message"`，session 广播）。
- **`GenericNotification` 事件**：`SystemMessageEvent::GenericNotification { group_id, message, receivers: Vec<Participant> }`。`GenericNotificationMessageProducer`（`crates/services/bcs-system-message/src/producers/generic.rs`）：`receivers` 非空 → 仅这些 bot；为空 → 全部 bot；`DeliveryType::Inject`；`recipients` 为空时返回 `vec![]`（不发任何消息）。
- **现有调用模板**：`crates/services/bcs-message-flow/src/group_flow.rs:751-768` 构造 `receivers = group.participants.filter(is_bot && bot_uuid != from_actor_id)`，然后 `let _ = system_message.notify(...)`。
- **路由层可访问**：`state.services.system_message`（`SystemMessageService`，application 层）、`state.services.registry`（`BotRegistryCoreService`；同文件 `caller_identities` 已在路由层使用，有先例）、`state.bcs_endpoint`（`HttpAppState`，来自 `BcsConfig.bcs_endpoint`）。
- `ensure_session_member` 返回 `Option<bcs_service_api::Session>`，含 `group_id` 与 `participants`。
- 无现成的 byte→可读尺寸工具，需新增。
- `urlencoding` 已是 bcs-http workspace 依赖（`crates/adapters/http/bcs-http/Cargo.toml:43`）。
- 下载路由 `/sessions/{sid}/files/{file_id}/content` 鉴权：要求调用方为 session 成员（bot bearer 或 human cookie + `human_has_session_access`）。系统消息的接收者均为 session 成员（被注入的 bot 是 session 参与者；前端 ws 客户端订阅了 session），故 `/content` 链接对所有实际接收者可达；且为持久链接，适合被持久化进消息历史。share-token 链接会在 ≤7 天过期，不适合，故不采用。

## 3. 方案选择

**Approach C：复用 `GenericNotification`**，不新增事件类型 / producer / 注册。在 `complete_upload` 成功分支预渲染消息文本并 `notify`。

选定理由：最小改动，符合"仅通知即可"的范围；现有 producer + dispatcher 已覆盖 Inject 投递、持久化、前端发布。

## 4. 架构与改动点

唯一改动文件：`crates/adapters/http/bcs-http/src/routes/session_files.rs`（及测试）。

### 4.1 修改 `complete_upload`

捕获 session（避免二次拉取），成功分支调用通知：

```rust
pub async fn complete_upload(
    State(state): State<HttpAppState>,
    Path((sid, file_id)): Path<(String, String)>,
    headers: HeaderMap,
    uri: Uri,
) -> Response {
    let caller = match resolve_group_chat_caller(&state, &headers, &uri).await {
        Ok(c) => c,
        Err(_) => return unauthorized(),
    };
    let sess = match ensure_session_member(&state, &sid, &caller).await {
        Some(s) => s,
        None => return forbidden_not_participant(),
    };
    match state.services.session_files.complete_upload(&sid, &file_id).await {
        Ok(f) => {
            notify_file_uploaded(&state, &sess, &sid, &caller, &f).await;
            (StatusCode::OK, Json(json!(to_dto(&f)))).into_response()
        }
        Err(e) => err_to_response(e),
    }
}
```

### 4.2 新增私有辅助函数（同模块）

**`notify_file_uploaded(state, sess, sid, caller, file)`：**

1. 由 `uploader_display_name(state, caller)` 解析上传者 `prefix` 与展示名 `name`。
2. `readable = human_readable_size(file.size)`。
3. `url = file_download_url(state, sid, &file.file_id)`。
4. 拼接消息（注意标点：括号内 `fileId` 与 `readableSize` 之间为中文逗号 `，`；括号后为中文逗号 + `下载链接` + 中文冒号 `：`，与示例一致）：
   ```rust
   let message = format!(
       "{} {} 上传了一个文件 {} ({}，{})，下载链接：{}",
       prefix, name, file.file_name, file.file_id, readable, url
   );
   ```
5. `uploader_actor_id = caller_to_actor_ref(caller).actor_id`。
6. `receivers = sess.participants.iter().filter(|p| p.is_bot() && p.bot_uuid != uploader_actor_id).cloned().collect()`。
7. **若 `receivers` 为空（session 内除上传者外无其他 bot），直接返回，不调用 `notify`。** 原因：`GenericNotificationMessageProducer` 对空 `receivers` 会回退到 `participants` 中所有 bot（含上传者），导致上传者被自我注入、前端仍被广播——与"排除上传者"语义冲突。注意与 `group_flow.rs` 调用模板的关键差异：后者用 `if !offline_bot_names.is_empty()` / `if !failed_bot_names.is_empty()` 天然保证 `notify` 仅在 `receivers` 非空时调用；本场景无条件触发，须显式 guard。
8. `event = SystemMessageEvent::GenericNotification { group_id: sess.group_id.clone(), message, receivers }`。
9. `let _ = state.services.system_message.notify(&sess.group_id, event, sid, &sess.participants).await;`（best-effort，忽略错误）。

**`uploader_display_name(state, caller) -> (&'static str, String)`（返回 prefix, name）：**

- `GroupChatCaller::Bot { bot_uuid }`：`prefix = "Bot"`；
  `name = state.services.registry.get(&bot_uuid).await.and_then(|b| b.capabilities.name.clone()).unwrap_or_else(|| bot_uuid.clone())`。
  （与 `bot_left.rs` 一致；路由层使用 `registry` 已有先例 `caller_identities`。）
- `GroupChatCaller::Human(h)`：`prefix = "用户"`；
  `name = h.nick_name.clone().unwrap_or_else(|| h.staff_no.clone())`。
  （与 `HumanJoined` 的 `bot_name = nick_name.or(staff_no)` 一致；`staff_no` 必非空。）

**`human_readable_size(bytes: u64) -> String`：** 1024 进制，单位 `B/KB/MB/GB/TB`；整除时取整数，否则保留 1 位小数。例：`12288 → "12 KB"`，`12345 → "12.1 KB"`，`500 → "500 B"`。无现成工具，新增为模块私有 fn。

**`file_download_url(state, sid, file_id) -> String`：** 镜像现有 `SessionFileServiceImpl::bcs_proxy_upload_url`：

```rust
let base = state.bcs_endpoint.clone()
    .unwrap_or_else(|| format!("http://{}:{}", state.bind, state.port));
format!("{}/sessions/{}/files/{}/content", base, urlencoding::encode(sid), file_id)
```

host 来源：`BcsConfig.bcs_endpoint`（`state.bcs_endpoint`，`crates/bootstrap/bcs/src/config.rs:358-362`，由 `http_adapter.rs:100-101` 注入 state），未配置时回退 `http://{bind}:{port}`。这与现有 file proxy / share URL 构建一致。

### 4.3 imports 增补

`session_files.rs` 当前 `use bcs_domain::{ActorKind, ActorRef, FileStatus, SessionFile};`。扩展为同时引入 `Participant` 与 `SystemMessageEvent`（二者均在 `bcs_domain` 中；`routes/sessions.rs:16` 即 `use bcs_domain::{ActorKind, SystemMessageEvent};`）：

```rust
use bcs_domain::{ActorKind, ActorRef, FileStatus, Participant, SessionFile, SystemMessageEvent};
```

`HttpAppState` 字段已确认：`bcs_endpoint: Option<String>`、`bind: String`、`port: u16`（`crates/adapters/http/bcs-http/src/state.rs:445-447`），故 `file_download_url` 中 `state.bind` / `state.port` 可用。

## 5. 数据流

```
human/bot → POST /sessions/{sid}/files/{file_id}/complete
  → resolve_group_chat_caller（鉴权）
  → ensure_session_member → sess（group_id, participants）
  → session_files.complete_upload → Ok(SessionFile f)（Pending→Ready）
  → notify_file_uploaded:
      build message（name / size / url）
      receivers = bots ∖ uploader
      （receivers 为空时直接返回，不调用 notify）
      SystemMessageService::notify(GenericNotification)
        → dispatcher:
            → GenericNotificationMessageProducer.produce
                 → [SystemGroupMessage{ recipients=receivers, message, Inject }]
            → 持久化 bcs_messages（sender_type=system）[best-effort]
            → 对每个 recipient：BotDeliveryPort::deliver
                 （chat.inject，deliver:false，from=bcs-system-message）
            → FrontendDeliveryPort::publish
                 （FrontendDeliveryTarget::Session，role=system，bot_uuid=bcs-system-message）
                 → 广播给所有订阅该 session 的前端连接
  → 200 to_dto(f)
```

## 6. 接收者、投递与边界

- **接收者**：仅 bot，排除上传者（上传者为 bot 时排除该 bot；为 human 时 `uploader_actor_id = human_<staff_no>` 不匹配任何 bot，即全部 bot）。`DeliveryType::Inject`（静默）。
- **前端 ws**：dispatcher 发布一条事件到 `FrontendDeliveryTarget::Session`，广播给所有订阅该 session 的前端连接（含上传者自身连接，与 `HumanJoined` 一致）。无 `exclude_conn_id`（当前 `notify` 不支持，需新管路，不在本次范围）。
- **持久化**：dispatcher 写一条 `system` 行至 `bcs_messages`，出现在历史 / 回放；故采用持久 `/content` 链接。
- **边界（已知）**：若 session 内除上传者外无其他 bot（`receivers` 为空），`notify_file_uploaded` 在调用 `notify` 前直接返回——既不注入 bot，也不发布前端事件。不能依赖 producer 对空 `receivers` 的处理：producer 会回退到全部 bot（含上传者），造成自我注入；故显式 guard 是正确性前提，非可选优化。多 bot 协作 session 不受影响。若需"即使无其他 bot 也通知前端"，为后续小改动（需不同机制），不在本次范围。
- **可达性**：`/content` 路由要求 session 成员；系统消息接收者均为 session 成员，故链接可达。

## 7. 错误处理

- 通知为 best-effort（`let _ = …`），其自身失败（dispatcher / 投递错误）不影响上传响应——上传已成功。
- 解析不至于 panic：registry 未命中 → 回退 id；`staff_no` 必非空；不做字符串切片（符合 CLAUDE.md UTF-8 安全要求）。
- 失败 / 中断的上传不发通知（仅在 `Ok(f)` 分支触发）。

## 8. 测试

- **单元**：`human_readable_size`（边界值、单位、舍入）；`file_download_url`（`bcs_endpoint` 已设 / 未设 → `http://{bind}:{port}` 回退）。
- **路由 / 集成**：`complete_upload` 测试，通过录制型 `SystemMessageService` spy（或接入 in-memory dispatcher + 录制型 delivery port，镜像现有 `HumanJoined` / `BotJoined` 测试方式）断言：
  - (a) 成功 complete-upload 触发一次 `notify`，失败时不触发；
  - (b) 事件为 `GenericNotification`，文本符合格式，`receivers` 排除上传者；
  - (c) bot 上传者：排除上传者 bot，prefix `Bot`；
  - (d) human 上传者：包含全部 bot，prefix `用户`。
  - (e) bot 上传者是 session 内唯一 bot：`receivers` 为空 → 不调用 `notify`（无 inject、无前端广播），直接验证自我注入缺陷已被 guard 拦截。
- 测试文件位置镜像现有 bcs-http 路由测试。

## 9. 不在范围内

- 不新增 `SystemMessageEvent` 变体 / producer / 注册。
- 不改协议帧、不改前端（`SystemMessageItem` 已能渲染 `role:system` 纯文本；下载链接以纯文本呈现）。
- 不做结构化元数据、不做文件卡片。
- 不处理"无其他 bot 时仍通知前端"的边界（标记为后续）。
- 不改 Git hooks / CI 入口 / coverage manifest（遵循 `AGENTS.md` pre-push 合约）。

## 10. 涉及文件

- `crates/adapters/http/bcs-http/src/routes/session_files.rs`（改 `complete_upload`；新增 `notify_file_uploaded` / `uploader_display_name` / `human_readable_size` / `file_download_url`；增补 imports）。
- 测试文件（位置镜像现有 bcs-http 路由测试）。

## 11. 关键文件锚点（实现参考）

- 路由处理器与 hook 点：`crates/adapters/http/bcs-http/src/routes/session_files.rs:442-464`（`complete_upload`）、`:301-336`（`ensure_session_member`，返回 `Session`）、`:254-265`（`caller_to_actor_ref`）、`:269-285`（`caller_identities`，路由层用 `registry` 的先例）。
- `GenericNotification` 事件类型：`crates/contracts/bcs-domain/src/system_message.rs:37-43`。
- generic producer 行为：`crates/services/bcs-system-message/src/producers/generic.rs`。
- 现有调用模板：`crates/services/bcs-message-flow/src/group_flow.rs:749-782`、`:840-859`。
- `HumanJoined` 从 HTTP 层触发：`crates/adapters/http/bcs-http/src/routes/sessions.rs:1561-1569`。
- `GroupChatCaller` / `HttpGroupCaller`（含 `nick_name`/`staff_no`）：`crates/adapters/http/bcs-http/src/routes/group_messages.rs:101-110`、`:345-371`。
- `BotRegistryCoreService::get` → `RegisteredBot.capabilities.name`：`crates/service-api/bcs-service-api/src/core/registry.rs:69`。
- `Services` 容器字段：`crates/service-api/bcs-services-container/src/services.rs:100`（`system_message`）、`:34`（`registry`）、`:110`（`session_files`）。
- 现有 URL 构建器（镜像对象）：`crates/services/bcs-session-file/src/service.rs:87-94`（`bcs_proxy_upload_url`）。
- `bcs_endpoint` 配置与注入：`crates/bootstrap/bcs/src/config.rs:358-362`、`crates/adapters/http/bcs-http/src/state.rs:445`、`crates/bootstrap/bcs/src/http_adapter.rs:100-101`。
- `urlencoding` 依赖：`crates/adapters/http/bcs-http/Cargo.toml:43`。
