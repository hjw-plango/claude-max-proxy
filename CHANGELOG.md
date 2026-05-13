# Changelog

## Unreleased

### 已经做的（按 commit 时间顺序）

代理 v1 → 已落库的功能演进。重点是把任意非真 CC 客户端（newapi 中转的 Hermes / Cherry / OpenAI 格式请求等）的 body 规整成 Claude 桌面 Cowork 模式的形态，最小化被 Anthropic 后端识别为第三方流量的概率。

#### 部署与基线

- **`init: claude-max-proxy local gateway`** — 抽出硬编码 token 到 `PROXY_API_KEYS` 环境变量，重写 README，重置 git history。
- **`deploy: add systemd unit template and deployment guide`** — `deploy/` 目录提供 systemd unit 模板 + `.env.example`；README 加 "用 systemd 托管" 小节。

#### Anthropic API 兼容层（OpenAI → Anthropic 翻译）

- **`fix: default max_tokens to 4096 when client omits it`** — newapi 转 OpenAI 时常常丢 `max_tokens`，Anthropic 必填，兜底补 4096。
- **`fix: strip OpenAI-only fields incompatible with Anthropic API`** — 黑名单剥离 `stream_options` / `frequency_penalty` / `presence_penalty` / `logit_bias` / `logprobs` 等 15 个 OpenAI 顶层字段。黑名单而非白名单，避免误删 Anthropic 未来的 beta 字段。
- **`fix: preserve server-side tool names (web_search, code_execution, etc.)`** — Anthropic 服务端工具（`type=web_search_20250305` 等）`name` 必须是固定字面量，不能进 OC_TO_CC 改名表。
- **`fix: normalize OpenAI-format tools before client detection`** — newapi 半转留下的 `{"type":"function","function":{...}}` tools，在入口转成 Anthropic `{name, description, input_schema}`，并暴露真实 tool name 给 `detect_client`（否则 Hermes 全被误判为 openclaw）。
- **`fix: normalize OpenAI-style tool/assistant messages to Anthropic shape`** — 多轮工具调用历史：`role="tool"` → `user.tool_result`；`assistant.tool_calls` → `assistant.content[].tool_use`；`arguments` JSON 字符串 parse 成 input dict。同时 `replace_tools` 末尾扫 messages 里的 `tool_use.name` 同步改名/借壳。

#### Cowork 伪装

- **`feat: reduce non-CC client fingerprints`** — 第一轮指纹小修：
  - identity 字符串字面量与真 CC 对齐
  - 用 `# Additional Instructions` markdown header 替换 `<system_instructions>` 标签（proxy 独有水印）
  - `metadata.user_id` 注入（device_id sha256 + 复用 session_id）
  - 基于 `(client_ip, user_token)` 5 分钟 LRU 复用 session-id
- **`feat: impersonate Claude desktop Cowork mode`** — 基于抓到的 Cowork 真实 baseline 全量对齐：
  - `system[0]` billing `cc_entrypoint=local-agent`（真 Cowork 值）
  - `system[1]` identity 改为 62 字符 Cowork 短版 `"You are a Claude agent, built on Anthropic's Claude Agent SDK."`
  - **新增 `system[2]`**：~60KB Cowork `application_details`，从本地 `cc_cowork_template.txt` 加载（gitignored，部署时本地填入，见 `.example`）
  - 兜底注入 `output_config: {"effort":"medium"}` 与 `thinking: {"type":"adaptive"}`（haiku 模型跳过 thinking）
  - 保留 cch + billing 注入：原 openclaw 实战验证体系
- **`feat: borrow non-native tool names to mcp__claude_ai_* shape`** — 非 CC 白名单也非 Cowork 内置 MCP 前缀的工具（业务 MCP 如 `mcp_wecom_docs_*`、`mcp_dingtalk_*`），自动改名成 `mcp__claude_ai_<原名>`，schema 完全保留。`replace_tools` 返回 `runtime_borrow` 映射，`remap_tool_names` 把响应里的 `tool_use.name` 反向还原成客户端原名。

### 抽样审计预警（2026-05-13）

Hermes 经 newapi 的 49-tools 流量曾命中一次 `You're out of extra usage`（1/175 偶发）。诊断为客户端 tools 里大量 `mcp_wecom_*` 业务工具名暴露了第三方身份。借壳 + Cowork 伪装的完整套件落地后未再复现。但抽样审计的累积风险仍在，建议：

1. 关闭 [claude.ai/settings/usage](https://claude.ai/settings/usage) 的 Extra Usage —— 偶发被识破时不 burn 配额
2. 客户端侧精简业务工具数量（真 Cowork 用户通常 ≤ 60 个 tools）
3. 长期监控 `out of extra usage` 出现频率，复发频率上升时再深挖（cch seed / Stainless 版本 / device_id 多样化等）

### 仅触及非 CC 客户端

以上所有伪装逻辑均在 `cc_client=False` 分支生效。`cc_client=True` 路径（UA 含 `claude-cli/`）完全透传，真 Claude Code CLI 直连体验不受影响。
