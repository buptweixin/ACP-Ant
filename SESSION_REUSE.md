# ACP 会话复用与 pi 提示词兼容

此 fork 基于 upstream main `dbf8963b0618bdedf0c7ff23dcfeb417d0edd029`，包含此前的 pi 兼容补丁。

## 启用

新安装可以直接使用本 fork 的可选 Compose 配置：

```sh
git clone https://github.com/buptweixin/ACP-Ant.git
cd ACP-Ant
docker compose -f docker-compose.yml -f docker-compose.session.yml up -d --build
```

已有部署应保留自己的数据卷、端口、反向代理和账号配置，只合并下面的环境变量并使用本 fork 重新构建镜像。

Docker Compose 的网关 service 增加：

```yaml
environment:
  AGY_PI_PROMPT_COMPAT: "1"
  AGY_SESSION_REUSE: "1"
  AGY_MAX_PROMPT_BYTES: "180000"
  AGY_SESSION_CACHE_SIZE: "64"
  AGY_SESSION_TTL: "1800"
  ACPANT_UPDATE_REPO: buptweixin/ACP-Ant
  ACPANT_UPDATE_BRANCH: main
```

重新构建镜像并启动。两个功能默认关闭，各自置 `0` 可回退。部署时使用本 fork 构建的镜像，不能使用上游镜像标签。单个网关进程管理自己的会话缓存；多 worker / 多副本需要客户端粘性路由才能稳定命中。

pi 等客户端继续发送完整历史，无需改变请求体。网关比较历史指纹，只把新增消息发给已有 ACP 会话。可选请求头 `X-ACP-Session: <每个对话唯一的ID>` 隔离不同对话；同一对话使用稳定 ID，分叉使用新 ID。`X-ACP-Session: off` 对单次请求禁用复用。不要把同一个固定 ID 当作省略历史的许可：本版始终要求客户端完整历史，不提供只有 delta 的无版本请求接口。

无此请求头时，开启复用的网关按完整历史前缀自动匹配；缓存还按鉴权密钥、API 路径、模型、effort、最终 system（含工具定义和 tool choice）及 X-ACP-Account 隔离。只有成功提交的完整回答会进入缓存。没有任何历史正文或密钥写入缓存日志，内存条目只保存历史哈希。

## 协议依据和实现

- [ACP Session Setup](https://agentclientprotocol.com/protocol/v1/session-setup)：会话持有独立上下文。
- [ACP Prompt Turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)：可以向同一 `sessionId` 发起后续 `session/prompt`。
- [Google Antigravity ACP](https://antigravity.google/docs/ide/extensions/acp#sessions)：支持加载、恢复、列出和关闭会话。

首次请求：`session/new → set_config_option → session/prompt`，成功后保留会话。后续请求只有在客户端历史严格扩展该会话的已提交输入和回答时才复用，否则新建会话。工具调用采用上游文本工具协议，客户端分配的 call ID 在下一轮以调用声明与结果绑定一起发送。不会让 agy 执行客户端工具。

缓存命中会话从空闲缓存移除，防止两个并发请求同时修改同一 session。分叉、客户端 compact、编辑历史、更改 system/工具/模型、进程重启会导致安全重建。过期/超容量条目被关闭。收到 compaction 事件后不继续缓存该会话；没有通知的上游自动压缩仍可能影响召回。本版不使用磁盘 session/load 自动恢复，因为不能仅凭进程重启推断磁盘会话与客户端历史仍然一致。

取消、超时和不确定的失败会取消并关闭会话，不重用可能已部分追加的状态。会话复用优先原账号；不可用时可以走原有账号选择逻辑，但只能用完整输入重建。如果完整输入已经超过字节保护上限，返回 `context_length_exceeded`，客户端需 compact 或改用长上下文路径。不会在缓存丢失后偷偷裁剪历史。

## 输入预算与 token

此前观测到的约 192 KB 是单次长文本路径的截断现象，不是 ACP 协议或模型总上下文的声明上限。默认发送保护为 180,000 UTF-8 字节（system + 此次发送的文本），留有余量，并非保证所有 agy 版本都有相同边界。图片继续由独立图片数量限制处理。

复用模式不裁剪历史；大历史可以通过多个较小的增量逐步积累。冷启动时单次过大的历史/工具结果仍会明确失败。无状态模式保留历史裁剪兼容行为，旧变量 `AGY_MAX_PROMPT_CHARS` 的单位修正为 UTF-8 字节。`AGY_MAX_PROMPT_BYTES=0` 可关闭最终保护，不建议在未经验证的路径使用。

`/v1/messages/count_tokens` 估计提交的完整输入，不再先删历史再估算。实际 usage 仍采用上游返回的 token 数据（没有时使用估算）；不把上传增量字节数当作模型总上下文大小，也不承诺缓存命中一定省计费 token。

## system prompt 与 429

`AGY_PI_PROMPT_COMPAT=1` 只匹配已验证的 pi 1.5.2 自动生成框架。替换默认身份说明、重复工具清单和固定文档模板，仅删除已识别的 `PI_*` 框架自省提示；保留默认工具选择/精确编辑等操作规则、所有追加/修改规则及后续项目指令。用户内容、工具名/参数和真实文件路径不做关键词清洗；减少自动生成的框架标识不等于保证无法被上游识别，也不保证消除所有 429。无法完整识别的模板保持原样，不对任意文本做关键字删除。

这是特定提示词兼容措施，不保证消除所有 429。真实 quota、rate limit、验证和认证失败仍按上游分类处理；不轮换措辞无限重试，不把额度不足伪装成功。AI Studio 的 `aistudio/` 路径不经过这些转换。

## 验证

```sh
python scripts/pi_prompt_compat_test.py
python scripts/session_reuse_test.py
python scripts/prompt_fit_test.py
python scripts/image_limit_test.py
```

还需运行原有 content、model_route、quota_flow、soft_error、token_login、studio、studio_heal 和 video_mode 离线测试。真实联调应在单独数据目录与容器进行，不复用运行中的 agy 进程或会话数据库。

检查 `/admin/status` 中 `session_cache.hits/misses/entries`，以及日志中的 `acp_session reused=True text_bytes=...`。日志不输出提示词正文。长文本验收要同时测试跨轮随机标记召回、工具循环、断开连接、缓存失效和分支；仅 HTTP 200 不算上下文保留成功。

### 2026-11-20 验证记录

- Python 3.12 / 3.14 的全部 12 组离线测试通过，包括 25 个会话/生命周期用例和 12 个 pi 提示词兼容用例。[CI 记录](https://github.com/buptweixin/ACP-Ant/actions/runs/44668621183)
- 隔离环境的早期候选镜像完成两轮真实 `gemini-4-hydrogen` 请求，每轮发送 119,800 字节；第二轮复用了 session，只追加该轮输入，上游返回 36,665 input tokens，其中 cached tokens 为 18,529。
- 第三轮长文本请求等待超时；随后的短请求也超时，未修改的原版服务短请求对照同样超时。基础 HTTPS 连通性正常，但没有据此认定具体上游故障原因。
- 完整长文本随机标记召回尚未验收，因此不声称已经验证了百万 token 上下文或消除了所有 429。功能仍默认关闭，建议先在隔离环境运行真实探针。真实请求会消耗账号额度。

### 保留操作规则的验证（2026-11-20）

后续兼容调整保留默认的 `read`/`edit`/`write` 使用规则和精确编辑约束，仅去掉已识别的框架自省提示。14 个提示词兼容测试与 25 个会话测试通过。隔离环境使用 agy 1.5.13 / gemini-4-hydrogen-low 回放四工具 pi 诊断样本：原兼容版和保留规则版均正常回答；保留规则版从 1430 增至 1545 个输入 token。模型随后正确发出 `read` 调用，在同一 ACP 会话收到模拟客户端工具结果后，正确回答结果中的测试标记。

这是转换层和 ACP 的短请求/单次工具往返验证，不代表完整 pi 客户端、所有工具、多模态或长上下文均已验证，也不保证所有 429 消失。
