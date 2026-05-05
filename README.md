# claude-max-proxy

将 Claude Max / Pro 订阅转换为标准 Anthropic Messages API 的本地代理网关。

让任意支持 Anthropic 协议的第三方客户端（IDE 插件、Agent 框架、自建 ChatBot 等）通过 Claude 订阅额度调用 API，无需额外购买 API credits。

> 本项目仅供学习与研究 Claude Code 协议使用。请合理使用，避免大流量滥用导致账号风控。

## 工作原理

```
第三方客户端 → http://localhost:5678/v1/messages → 请求改写 → api.anthropic.com
                                                       ├─ 注入 OAuth Bearer Token
                                                       ├─ 伪装为 Claude Code CLI 的 UA / Stainless 头
                                                       ├─ 工具名映射 (双向)
                                                       ├─ system prompt 迁移到 user message
                                                       ├─ 关键词洗白 (路径/模块名占位保护)
                                                       └─ 计算 cch 完整性签名
```

代理读取本地 Claude Code CLI 保存的 OAuth token，把请求伪装为 Claude Code CLI 发出的形态，从而消耗订阅额度而非 API credits。

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

# 启动
python3 proxy.py
```

启动后代理监听 `http://0.0.0.0:5678`，第三方客户端将 base URL 设为 `http://<host>:5678`、API Key 设为上面配置的任意一个 token 即可使用。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_API_KEYS` | **(必填)** | 客户端鉴权 token 列表，格式 `name:key,name:key,...` |
| `PORT` | `5678` | 监听端口 |
| `BIND_HOST` | `0.0.0.0` | 监听地址。生产环境建议改为 `127.0.0.1` 或 Tailscale 段地址 |
| `DEBUG` | 空 | 设为 `1` 开启调试模式（请求 dump 到 `/tmp/proxy_*.json`） |

`PROXY_API_KEYS` 格式示例：
```bash
export PROXY_API_KEYS="alice:sk-myproxy-AbCdEf123,bob:sk-myproxy-XyZ789"
```

每条以 `:` 分隔 user 标识（仅用于日志审计）和真实 token；多条用 `,` 分隔。

## 部署建议

- **不要把 `BIND_HOST=0.0.0.0` 直接暴露在公网**。建议组合：
  - `BIND_HOST=127.0.0.1` + 反向代理（nginx/caddy）+ TLS
  - `BIND_HOST=0.0.0.0` + 防火墙（ufw/iptables）只放行内网或 Tailscale 段
- API key 至少 32 位随机字符串，可用 `python3 -c 'import secrets; print("sk-myproxy-"+secrets.token_urlsafe(24))'` 生成。
- 日志 `proxy.log` 在 `DEBUG` 模式下可能包含请求体，注意权限和清理。

## 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | 主端点，Anthropic 原生协议，所有客户端走这里 |
| `/v1/chat/completions` | POST | **已禁用**，返回 400 + 引导消息（请改用 `/v1/messages`） |
| `/v1/models` | GET | 模型发现端点，返回当前可用模型列表 |
| `/health` | GET | 健康检查，返回 token 剩余有效期 |

## 工具名映射

`tool_name_mapping.json` 配置三类映射：

- `_remove`：客户端独有但 Claude Code 不存在的工具，转发前直接丢弃（如飞书全家桶、browser_*、discord 等）
- `direct`：语义直接对应的工具（如 `read` ↔ `Read`、`exec` ↔ `Bash`）
- `borrowed`：借用 Claude Code 已有工具名作为壳，原 schema 不变（如 `memory_search` → `Glob`，模型读 description 仍能正确使用）
- `hermes`：Hermes 风格客户端的专用映射表

请求方向（客户端 → Anthropic）按 `direct/borrowed/hermes` 改名；响应方向（Anthropic → 客户端）按反向表还原。

## System Prompt 处理

Anthropic 通过 `system` 字段识别第三方应用。代理把客户端原始 system prompt 迁移到第一条 user message（包裹在 `<system_instructions>` 标签内），`system` 字段重写为标准 Claude Code 格式（billing header + identity）。

## Cache Control 策略

非 Claude Code 客户端的请求，代理会**剥光客户端在 body 里加的所有 `cache_control` 字段**，由代理自行注入 2 个：

- `system.identity` 加 `cache_control: ttl=1h`
- 第一条 user message 的 prefix block 加 `cache_control: ttl=1h`

这样既匹配真实 Claude Code 的 cache 模式（cache 全在 system 端），又避免命中 Anthropic 的"单请求最多 4 个 cache_control block"上限。

真 Claude Code CLI 直连不受影响（`cc_client=True` 路径全程透传）。

## 注意事项

- Token 依赖本地 Claude Code CLI 的凭证文件（`~/.claude/.credentials.json`），请勿泄露。
- Token 过期前 5 分钟代理会自动调用 `claude --print` 触发刷新（带锁，防止并发刷三次）。
- `cc_version` 从本地 `claude --version` 自动检测，build 号可通过 `.cc_build` 文件覆盖。
- 在 [claude.ai/settings/usage](https://claude.ai/settings/usage) 关闭 Extra Usage，避免被识别为第三方时产生额外费用。
- 如果遇到 `You're out of extra usage` 报错，说明请求已被判定为第三方客户端、强制走 Extra Usage 计费 —— 检查关键词替换是否漏掉了某个客户端标识。

## License

[MIT](LICENSE)
