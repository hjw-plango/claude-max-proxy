# claude-max-proxy

将 Claude Max / Pro 订阅转换为标准 Anthropic Messages API 的本地代理网关。

让任意客户端（IDE 插件、Agent 框架、自建 ChatBot、Hermes 等）通过 Claude 订阅额度调用 Anthropic API，无需额外购买 API credits。

> 本项目仅供学习与研究 Anthropic 协议、Claude Code CLI 行为使用。请合理使用，避免大流量滥用导致账号风控。

## 工作原理

代理读取本地 Claude Code CLI 保存的 OAuth token，把请求伪装为 Claude 桌面应用 Cowork 模式发出的形态，从而消耗订阅额度而非 API credits。

按客户端类型走两条独立路径：

```
                                                    ┌─ cc_client=True  ─→ 透传(只换 OAuth/UA)
请求 → /v1/messages → 鉴权 → is_cc_client(UA) ?─────┤
                                                    └─ cc_client=False ─→ 协议归一化 + Cowork 伪装
                                                                                    │
                                                                                    ↓
                                                                          api.anthropic.com
```

是否被识别为"真 Claude Code"取决于 `User-Agent` 是否含 `claude-cli/`：
- **是** → 真 CC CLI 路径：proxy 不动 body，只注入 OAuth Bearer Token + 必要的 anthropic headers
- **否** → 非 CC 路径：proxy 做完整的协议归一化 + Cowork 模式伪装（详见后文）

## 前置条件

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录（`claude` 命令可用、`~/.claude/.credentials.json` 存在）
- 有效的 Claude Max / Pro 订阅

## 快速开始

```bash
git clone https://github.com/hjw-plango/claude-max-proxy.git
cd claude-max-proxy

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 配置 API key (必需)
export PROXY_API_KEYS="alice:sk-myproxy-alice-xxxxx,bob:sk-myproxy-bob-yyyyy"

# (推荐) 抓一份 Cowork application_details 模板放在本地, 见后文 "Cowork 模板"
# 不放也能跑, 但伪装质量降低

# 启动
python3 proxy.py
```

启动后代理监听 `http://0.0.0.0:5678`。客户端把 base URL 设为 `http://<host>:5678`、API Key 设为上面配置的任意一个 token 即可使用。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_API_KEYS` | **(必填)** | 客户端鉴权 token 列表，格式 `name:key,name:key,...` |
| `PORT` | `5678` | 监听端口 |
| `BIND_HOST` | `0.0.0.0` | 监听地址。生产环境建议改为 `127.0.0.1` 或 Tailscale 段地址 |
| `DEBUG` | 空 | 设为 `1` 开启调试模式（请求 dump 到 `/tmp/proxy_*.json`） |

生成强 API key：
```bash
python3 -c 'import secrets; print("sk-myproxy-"+secrets.token_urlsafe(24))'
```

每条 `PROXY_API_KEYS` 用 `:` 分隔 user 标识（仅用于日志审计）和真实 token；多条用 `,` 分隔。

## 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | 主端点，Anthropic 原生协议，所有客户端走这里 |
| `/v1/chat/completions` | POST | **已禁用**，返回 400 + 引导消息（请改用 `/v1/messages`） |
| `/v1/models` | GET | 模型发现端点（OpenAI 风格），返回当前可用模型列表 |
| `/health` | GET | 健康检查，返回 token 剩余有效期 |

## 真 Claude Code CLI 直连（透传路径）

判定条件：请求头 `User-Agent: claude-cli/X.Y.Z (...)`。

`is_cc_client(req)` 返回 `True` 后，proxy 把 body **字节级原样**转发给 Anthropic，**不做任何改写**：

```
[ Claude Code CLI ] → newapi (透明转发) → [ proxy ]
                                           │
                                           ├─ 鉴权 PROXY_API_KEYS
                                           ├─ Authorization: Bearer <OAuth>
                                           ├─ X-Claude-Code-Session-Id (透传客户端原值)
                                           ├─ Anthropic-Beta (透传 + 加 oauth-2025-04-20)
                                           ├─ User-Agent, Stainless headers (符合 CC SDK)
                                           │
                                           └─ POST api.anthropic.com/v1/messages
                                              (body 不动: system / messages / tools / metadata / 
                                               output_config / thinking / context_management 全部保留)
```

适用场景：Claude Code CLI 多人共用同一份 Max/Pro 订阅、各自带不同 `PROXY_API_KEYS` token 鉴权。

行为表现：body 跟客户端发的完全一致，Anthropic 视角看不出经过 proxy。

## 非 CC 客户端（Cowork 伪装路径）

判定：`is_cc_client(req)` 返回 `False`（UA 不含 `claude-cli/`）。覆盖 newapi 中转的 Hermes / Cherry Studio / OpenClaw / 任意其他客户端。

处理链路：

```
[ Hermes / Cherry / 其他 ] ─OpenAI 或 Anthropic 协议─→ [ newapi ] ─→ [ proxy ]
                                                                      │
                                                                      ▼
       ┌───────────────────────────────────────────────────────────────────────┐
       │                       proxy 入口归一化阶段                              │
       ├───────────────────────────────────────────────────────────────────────┤
       │ ① 剥 OpenAI-only 顶层字段 (stream_options, frequency_penalty, ...)     │
       │ ② max_tokens 缺失补 4096                                                │
       │ ③ OpenAI tools {type:function, function:{...}} → Anthropic 格式         │
       │ ④ messages 数组里 role="system" 内联 → 合并到顶层 system 字段           │
       │ ⑤ messages 数组里 role="tool" → user.tool_result                       │
       │ ⑥ assistant.tool_calls → assistant.content[].tool_use                  │
       └───────────────────────────────────────────────────────────────────────┘
                                                                      ▼
       ┌───────────────────────────────────────────────────────────────────────┐
       │                       detect_client + Cowork 伪装                       │
       ├───────────────────────────────────────────────────────────────────────┤
       │ detect_client(body.tools): {read_file, write_file, ...} → hermes       │
       │                            其他                            → openclaw  │
       │                                                                         │
       │ session-id LRU 复用: (client_ip, user_token) 5 分钟窗口                  │
       │ metadata.user_id 注入: {device_id: sha256(token), session_id: ...}      │
       │ 兜底 output_config / thinking (haiku 跳过 thinking)                     │
       │                                                                         │
       │ 工具映射 replace_tools:                                                  │
       │   - REMOVE_TOOLS 黑名单丢弃                                              │
       │   - HERMES_TO_CC / OC_TO_CC 显式表改名                                  │
       │   - server-side tool (type 字段) 透传                                   │
       │   - 其他非白名单 tool 借壳 mcp__claude_ai_<原名>                          │
       │ 同步改写 messages 数组里的 tool_use.name                                  │
       │                                                                         │
       │ system 重写为 Cowork 三段:                                              │
       │   [0] x-anthropic-billing-header: cc_entrypoint=local-agent; cch=...   │
       │   [1] "You are a Claude agent, built on Anthropic's Claude Agent SDK." │
       │   [2] <application_details>...60KB...                                  │
       │ 客户端原 system → 第一条 user message 前置 (markdown header)             │
       │ 关键词洗白: openclaw/hermes/NousResearch → claude_code/claude/anthropic  │
       │ cch 签名重算回填                                                         │
       └───────────────────────────────────────────────────────────────────────┘
                                                                      ▼
                            POST api.anthropic.com/v1/messages
                                                                      ▼
       ┌───────────────────────────────────────────────────────────────────────┐
       │                          响应方向 remap_tool_names                       │
       ├───────────────────────────────────────────────────────────────────────┤
       │ tool_use.name 反向还原: Bash→exec / Read→read_file / mcp__claude_ai_X→X │
       │ (流式与非流式两种响应都处理)                                              │
       └───────────────────────────────────────────────────────────────────────┘
                                                                      ▼
                                                          [ 客户端: 收到原工具名 ]
```

### 协议归一化（兼容 OpenAI 风格）

newapi 等中转工具把 OpenAI 协议请求半转给 Anthropic 时常常漏掉细节，proxy 在入口统一兜底：

| OpenAI 协议特征 | Anthropic 要求 | proxy 处理 |
|---|---|---|
| `max_tokens` 字段缺失 | 必填 | 兜底补 `4096` |
| `stream_options` / `frequency_penalty` / `presence_penalty` / `logit_bias` / `logprobs` / `top_logprobs` / `n` / `response_format` / `seed` / `user` / `audio` / `modalities` / `parallel_tool_calls` / `prediction` / `web_search_options` / `store` 等 15 个 OpenAI 顶层字段 | 全部不接受 | 黑名单剥离（黑名单不是白名单，避免误删 Anthropic 未来的 beta 字段） |
| `tools: [{type:"function", function:{name, description, parameters}}]` | `tools: [{name, description, input_schema}]` | 入口转格式 |
| `messages: [{role:"system", content:"..."}]` 内联 | system 必须在顶层 | 提取到顶层 `system` |
| `messages: [{role:"tool", tool_call_id, content}]` | 要 user.tool_result | 转 `{role:"user", content:[{type:"tool_result", tool_use_id, content}]}` |
| `messages: [{role:"assistant", tool_calls:[...]}]` | 要 assistant.content[].tool_use | 转 `{role:"assistant", content:[{type:"text",...},{type:"tool_use", id, name, input}]}`，`arguments` JSON 字符串自动 parse 成 input dict |

### 工具名处理

`tool_name_mapping.json` 配置四类映射：

| 类别 | 用途 | 示例 |
|---|---|---|
| `_remove` | CC 不存在的工具，转发前直接丢弃 | `feishu_*`、`browser_*`、`discord_*`、`ha_*`（HomeAssistant）、`yb_*`、`rl_*` |
| `direct` | 语义直接对应 | `read↔Read`、`exec↔Bash`、`edit↔Edit`、`write↔Write`、`web_search↔WebSearch`、`web_fetch↔WebFetch` |
| `borrowed` | 借壳 CC 已有工具名，schema 不变 | `memory_search→Glob`、`memory_get→Grep`、`pdf→Skill`、`image→NotebookEdit` |
| `hermes` | Hermes 客户端专用映射 | `read_file→Read`、`write_file→Write`、`patch→Edit`、`terminal→Bash`、`execute_code→NotebookEdit`、`search_files→Grep` |

`replace_tools` 处理顺序：

1. 有 `type` 字段的 server-side tool（`type=web_search_20250305` 等）→ 透传不动（`name` 必须是 Anthropic 约定的固定字面量）
2. `name` 在 `_remove` → 删除
3. `name` 在 `direct/borrowed/hermes` 显式表 → 按表改名
4. `name` 在 CC 原生白名单或已是 `mcp__claude_ai_*` 前缀 → 透传不动
5. **兜底借壳**：其他全部改名为 `mcp__claude_ai_<原名>`，schema 完全保留

借壳的目的：避免业务 MCP 工具名（如 `mcp_wecom_docs_*`、`mcp_dingtalk_*`）泄露第三方身份。改名后 Anthropic 视角看到的 tools 名单看起来像"Cowork 用户接了一堆官方 MCP"。模型仍能正确使用工具，因为读的是 description / input_schema，不是 name。

借壳的反向映射通过运行时 `runtime_borrow` dict 记录，`remap_tool_names` 把响应里的 `tool_use.name` 还原回客户端原名（流式与非流式都处理）。

### System Prompt 处理

非 CC 客户端的请求，proxy 把客户端原 system 嵌入第一条 user message 的开头（用 `# Additional Instructions` markdown header 包装；旧版 `<system_instructions>` 标签是 proxy 独有水印，已去除）。

顶层 `system` 字段重写为 Cowork 标准三段：

```python
[
  # billing header (无 cache_control)
  {"type": "text",
   "text": "x-anthropic-billing-header: cc_version=2.1.140.190; cc_entrypoint=local-agent; cch=<动态>"},

  # Cowork identity (cache 1h)
  {"type": "text",
   "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
   "cache_control": {"type": "ephemeral", "ttl": "1h"}},

  # Cowork application_details ~60KB (cache 1h)
  {"type": "text",
   "text": COWORK_APPLICATION_DETAILS,  # 从 cc_cowork_template.txt 加载
   "cache_control": {"type": "ephemeral", "ttl": "1h"}},
]
```

### Cache Control 策略

非 CC 客户端请求进入后：

1. **剥光客户端塞的 cache_control**（Anthropic 限制单请求最多 4 个 cache_control block，客户端散加是常见超限原因）
2. proxy 主动加 2 个：`system[1] identity` 1h、`system[2] application_details` 1h
3. tools 数组**不加 cache_control**（与真 Cowork baseline 一致 —— cache 全在 system 端）

第一次请求 `cache_create ≈ 60KB`，后续同 session 请求 `cache_read ≈ 60KB`、`input=1` 真实计费 token，极大省订阅额度。

### metadata 与配套字段

非 CC 客户端注入：

```python
body["metadata"] = {
  "user_id": '{"device_id":"<sha256(user_token)>","account_uuid":"","session_id":"<复用5min的uuid>"}'
}
body["output_config"] = {"effort": "medium"}    # 客户端未传时
body["thinking"]     = {"type": "adaptive"}     # 客户端未传时, haiku 模型跳过
```

`session_id` 通过模块内 LRU 缓存基于 `(client_ip, user_token)` 5 分钟窗口复用，模拟真 CC "一会话内同一 session-id" 的行为。

## Cowork 模板（`cc_cowork_template.txt`）

非 CC 客户端被伪装成 Cowork 模式时，`system[2]` 需要一段 ~60KB 的 Cowork `application_details` 内容。

这段文件**含 Anthropic 内部 prompt，不入公开仓库**（`.gitignore` 排除）。仓库里有 `cc_cowork_template.txt.example` 文件说明如何抓取与脱敏。

如果文件不存在，proxy 会用一段简短 stub 兜底（`<application_details>Claude is powering Cowork mode...</application_details>`），请求能成功但伪装质量明显下降，强烈建议补齐模板。

## 用 systemd 托管（生产部署）

`deploy/` 目录提供 systemd unit 模板：

```bash
# 1. 复制 unit 文件
sudo cp deploy/claude-max-proxy.service /etc/systemd/system/

# 2. 复制 env 模板, 填入真实 token (chmod 600)
sudo cp deploy/claude-max-proxy.env.example /etc/claude-max-proxy.env
sudo chmod 600 /etc/claude-max-proxy.env
sudo nano /etc/claude-max-proxy.env   # 把 REPLACE_ME 换成真 token

# 3. 启用 + 启动 (开机自启)
sudo systemctl daemon-reload
sudo systemctl enable --now claude-max-proxy

# 4. 验证
sudo systemctl status claude-max-proxy
curl http://127.0.0.1:5678/health
```

如果 repo 不在 `/root/claude-max-proxy`，需要修改 `deploy/claude-max-proxy.service` 里的 `WorkingDirectory=` 和 `ExecStart=` 路径。

unit 已开启的安全收紧：`NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=full` / `ProtectKernelTunables` / `ProtectKernelModules` / `ProtectControlGroups` / `RestrictSUIDSGID` / `LockPersonality`。`ProtectHome` 未开（proxy 需要写 `~/.claude/.credentials.json` 刷 token）。

常用命令：

```bash
sudo systemctl restart claude-max-proxy   # 改完 env 或代码后重启
sudo systemctl stop claude-max-proxy
sudo journalctl -u claude-max-proxy -f    # 看 service 启停事件
tail -f /root/claude-max-proxy/proxy.log  # 看请求日志
```

## 部署建议

- **不要把 `BIND_HOST=0.0.0.0` 直接暴露在公网**。建议组合：
  - `BIND_HOST=127.0.0.1` + 反向代理（nginx/caddy）+ TLS
  - `BIND_HOST=0.0.0.0` + 防火墙（ufw/iptables）只放行内网或 Tailscale 段
- API key 至少 32 位随机字符串
- 日志 `proxy.log` 在 `DEBUG` 模式下可能包含完整请求体，注意权限和清理

## 注意事项与已知风险

- Token 依赖本地 Claude Code CLI 的凭证文件（`~/.claude/.credentials.json`），请勿泄露
- Token 过期前 5 分钟代理会自动调用 `claude --print "ping"` 触发刷新（带锁，防止并发刷三次）
- `cc_version` 从本地 `claude --version` 自动检测，build 号可通过 `.cc_build` 文件覆盖
- 在 [claude.ai/settings/usage](https://claude.ai/settings/usage) **关闭 Extra Usage**：偶发被识别为第三方时不 burn 配额（请求会直接 403 而非走计费）
- 如果遇到 `You're out of extra usage` 报错：请求已被 Anthropic 后端判定为第三方流量、强制走 Extra Usage 计费。检查：
  - 客户端 tools 数量是否过多（真 Cowork 用户通常 ≤ 60 个 tools）
  - 业务 MCP 工具是否走了借壳（看日志 `tools: X mapped, borrowed N`）
  - `cc_cowork_template.txt` 是否齐全（缺失会大幅降低伪装质量）
- 流量来源 IP 集中度本身也是 fingerprint，proxy 无法解决。同一台机器 proxy 转发大量请求看起来不像普通 Cowork 单用户行为，长期可能被抽样审计

## 调试

`DEBUG=1` 模式下 proxy 会把以下文件 dump 到 `/tmp/`（service 私有 namespace 下，宿主机的访问路径是 `/proc/<pid>/root/tmp/`）：

| 文件 | 内容 |
|------|------|
| `proxy_neo_raw.json` | 客户端发来的原始 body |
| `proxy_neo_last.json` | proxy 改写后即将发给 Anthropic 的 body |
| `proxy_headers.json` | 入站请求头（用于核对 newapi 透传效果） |

排查时开 DEBUG → 复现一次 → 查看 dump 对比改写前后差异。**用完务必关闭 DEBUG**，避免长期写盘并暴露真实业务内容。

## License

[MIT](LICENSE)
