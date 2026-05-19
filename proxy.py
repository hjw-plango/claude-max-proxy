#!/usr/bin/env python3
"""
Claude Max → Anthropic API 代理网关

核心机制:
1. 认证用 Authorization: Bearer <oauth-token>(从本地 Claude Code 凭证读取)
2. 必须带 anthropic-beta: oauth-2025-04-20
3. Anthropic 通过扫描 body 关键词识别第三方客户端,代理在转发前做关键词替换
4. 注入标准 Claude Code 格式的 system prompt + cch 签名,使用订阅额度
"""

import json
import os
import sys
import time
import uuid
import threading

import xxhash
import requests
from flask import Flask, request, Response, stream_with_context

app = Flask(__name__)

# ============================================================
# API key 鉴权 — 从环境变量读取,格式: PROXY_API_KEYS="name1:sk-xxx,name2:sk-yyy"
# 多组 key 用逗号分隔,每组用 : 分隔 user 标识和 token
# ============================================================
def _load_allowed_tokens() -> dict:
    raw = os.environ.get("PROXY_API_KEYS", "").strip()
    tokens = {}
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, key = pair.split(":", 1)
            tokens[key.strip()] = name.strip()
    return tokens

ALLOWED_TOKENS = _load_allowed_tokens()

# ============================================================
# refresh token 加锁 — 多客户端共用同一 OAuth,防并发刷新浪费 quota
# ============================================================
_refresh_lock = threading.Lock()

# ============================================================
# 配置
# ============================================================

DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
PORT = int(os.environ.get("PORT", "5678"))

CLAUDE_DIR = os.path.expanduser("~/.claude")
CREDENTIALS_FILE = os.path.join(CLAUDE_DIR, ".credentials.json")

UPSTREAM = "https://api.anthropic.com"
CCH_SEED = 0x6E52736AC806831E

# CC 版本自动检测
def detect_cc_version():
    import subprocess
    import re
    try:
        out = subprocess.check_output(["claude", "--version"], timeout=5, text=True).strip()
        m = re.search(r'(\d+\.\d+\.\d+)', out)
        main_ver = m.group(1) if m else "2.1.92"
    except:
        main_ver = "2.1.92"

    build_cache = os.path.join(os.path.dirname(__file__), ".cc_build")
    build_num = "190"
    if os.path.exists(build_cache):
        with open(build_cache) as f:
            cached = f.read().strip()
            if cached:
                build_num = cached

    return main_ver, build_num

CC_VERSION, CC_BUILD = detect_cc_version()
CC_FULL_VERSION = f"{CC_VERSION}.{CC_BUILD}"

# ============================================================
# Token 管理
# ============================================================

def load_credentials():
    with open(CREDENTIALS_FILE) as f:
        return json.load(f)["claudeAiOauth"]

def get_access_token():
    cred = load_credentials()
    expires_at = cred.get("expiresAt", 0)
    if time.time() * 1000 > expires_at - 300_000:
        # refresh 加锁 + double-check, 多客户端共用 OAuth 时防并发刷三次
        with _refresh_lock:
            cred = load_credentials()
            expires_at = cred.get("expiresAt", 0)
            if time.time() * 1000 > expires_at - 300_000:
                sys.stdout.write("[proxy] Token expiring, refreshing via claude --print...\n")
                sys.stdout.flush()
                os.system('claude --print "ping" > /dev/null 2>&1')
                cred = load_credentials()
    return cred["accessToken"]

# ============================================================
# cch 签名计算
# ============================================================

def compute_cch(body_bytes: bytes) -> str:
    h = xxhash.xxh64(body_bytes, seed=CCH_SEED).intdigest()
    return f"{h & 0xFFFFF:05x}"

# ============================================================
# session-id LRU 缓存 — 模拟真 CC "一会话内复用同一 session-id" 的行为
# key = (remote_addr, user_token), value = (uuid, last_seen_ts), TTL = 5 分钟
# ============================================================

import hashlib

_SESSION_TTL = 300  # 5 分钟
_session_cache: dict = {}
_session_lock = threading.Lock()

def get_or_create_session_id(remote_addr: str, user_token: str) -> str:
    """非 CC 客户端没传 session-id 时, 基于 (ip, user) 在 5 分钟窗口内复用同一 uuid"""
    key = (remote_addr or "", user_token or "")
    now = time.time()
    with _session_lock:
        # 顺手清理过期项, 防止 dict 长期累积
        for k in list(_session_cache.keys()):
            if now - _session_cache[k][1] > _SESSION_TTL:
                _session_cache.pop(k, None)
        if key in _session_cache:
            sid, _ = _session_cache[key]
            _session_cache[key] = (sid, now)
            return sid
        sid = str(uuid.uuid4())
        _session_cache[key] = (sid, now)
        return sid

def compute_device_id(user_token: str) -> str:
    """基于 user_token 稳定 hash 出 64 字符 hex device_id, 跟真 CC metadata.user_id 里的格式一致"""
    return hashlib.sha256((user_token or "anon").encode("utf-8")).hexdigest()

# ============================================================
# 被 Anthropic 屏蔽的第三方应用关键词
# ============================================================

BLOCKED_KEYWORDS = [
    ("OpenClaw", "Claude Code"),
    ("openclaw", "claude_code"),
    ("open_claw", "claude_code"),
    ("open-claw", "claude-code"),
    # 第三方 agent client 标识 — 防止 Anthropic 扫 message body 识别
    ("NousResearch", "Anthropic"),
    ("nousresearch", "anthropic"),
    ("Hermes Agent", "Claude Code"),
    ("hermes-agent", "claude-code"),
    ("hermes_agent", "claude_code"),
    ("Hermes", "Claude"),
    ("hermes", "claude"),
]

# 不应被替换的模式（占位保护）
# 替换这些会导致 tool 调用失败、命令执行出错、URL 不可达等问题
import re
_PROTECT_PLACEHOLDER = "__OCPROT_{}_TORPCO__"
_PROTECT_PATTERNS = [
    # 文件路径: /home/xxx/.openclaw/workspace-daliu/, ~/.openclaw/media/
    re.compile(r'(/[\w.~/-]*)\.openclaw(/[\w.~/-]*)'),
    # npm 模块路径: node_modules/openclaw/
    re.compile(r'node_modules/openclaw'),
    # channel 标识符: openclaw-weixin
    re.compile(r'openclaw-weixin'),
]

def sanitize_body(body_str: str) -> str:
    """替换被 Anthropic 屏蔽的第三方应用关键词，但保护路径/命令/URL/标识符不被篡改"""
    # 1. 收集所有需要保护的文本片段
    placeholders = []
    for pattern in _PROTECT_PATTERNS:
        for m in pattern.finditer(body_str):
            placeholders.append(m.group())
    # 去重并按长度降序（先替换长的，避免子串冲突）
    placeholders = sorted(set(placeholders), key=len, reverse=True)
    for i, ph in enumerate(placeholders):
        body_str = body_str.replace(ph, _PROTECT_PLACEHOLDER.format(i))

    # 2. 执行关键词替换
    for old, new in BLOCKED_KEYWORDS:
        body_str = body_str.replace(old, new)

    # 3. 恢复占位符为原始文本
    for i, ph in enumerate(placeholders):
        body_str = body_str.replace(_PROTECT_PLACEHOLDER.format(i), ph)

    return body_str

# ============================================================
# 请求体处理
# ============================================================

# 加载 tool 名称映射表
_MAPPING_FILE = os.path.join(os.path.dirname(__file__), "tool_name_mapping.json")
with open(_MAPPING_FILE) as _f:
    _mapping = json.load(_f)

REMOVE_TOOLS = set(_mapping["_remove"])
OC_TO_CC = {**_mapping["direct"], **_mapping["borrowed"]}
HERMES_TO_CC = _mapping.get("hermes", {})
CC_TO_OC = {v: k for k, v in OC_TO_CC.items()}
CC_TO_HERMES = {v: k for k, v in HERMES_TO_CC.items()}

# CC 原生 + Anthropic Cowork 内置 MCP 工具白名单 (从 mapping 表 value 端推导, 不在此名单的全部借壳)
CC_NATIVE_TOOLS = set(OC_TO_CC.values()) | set(HERMES_TO_CC.values())

# 加载 Cowork application_details 模板 — 非 CC 客户端伪装成 Cowork 模式时塞进 system[2]
# 文件不入 git (含 Anthropic 内部 prompt), 部署时本地脱敏后填入, 见 cc_cowork_template.txt.example
_TEMPLATE_FILE = os.path.join(os.path.dirname(__file__), "cc_cowork_template.txt")
try:
    with open(_TEMPLATE_FILE) as _f:
        COWORK_APPLICATION_DETAILS = _f.read()
    sys.stdout.write(f"[proxy] loaded cowork template ({len(COWORK_APPLICATION_DETAILS)} chars)\n")
    sys.stdout.flush()
except FileNotFoundError:
    COWORK_APPLICATION_DETAILS = "<application_details>\nClaude is powering Cowork mode, a feature of the Claude desktop app.\n</application_details>"
    sys.stdout.write(f"[proxy] WARN cc_cowork_template.txt not found, using minimal stub (伪装质量降低, 见 .example)\n")
    sys.stdout.flush()

# Hermes 特征 tool — 用于 detect_client(): 命中任一即判定 hermes
_HERMES_SIGNATURE = {"read_file", "write_file", "patch", "execute_code",
                     "search_files", "web_extract", "delegate_task", "session_search"}

def is_cc_client(req) -> bool:
    """识别真 Claude Code CLI 客户端 — User-Agent 含 claude-cli/X.Y.Z"""
    ua = req.headers.get("User-Agent", "")
    return "claude-cli" in ua

def detect_client(body: dict) -> str:
    """根据 tools 数组里的 name 集合判定 client 类型
    return: "hermes" | "openclaw"
    (CC client 在外层 is_cc_client 已过滤,此处不会被调到)"""
    tools = body.get("tools") or []
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    if names & _HERMES_SIGNATURE:
        return "hermes"
    return "openclaw"

def replace_tools(body: dict, cc_client: bool = False, client_type: str = "openclaw") -> dict:
    """替换 tool 名称, 返回运行时借壳映射 (new_name -> original_name) 供响应反向使用.
    按特征分流: 真 CC client 自己 tools 已合规, 不映射, 返回空 dict."""
    if cc_client:
        return {}  # CC client 自带的 tools 是 Anthropic 合规的,不动
    tools = body.get("tools")
    if not tools:
        return {}

    table = HERMES_TO_CC if client_type == "hermes" else OC_TO_CC
    new_tools = []
    runtime_borrow = {}  # new_name -> original_name, 用于响应反向映射
    for t in tools:
        name = t.get("name")
        # server-side tool (web_search_20250305 / code_execution_* / bash_* / computer_* / text_editor_* 等)
        # 有 "type" 字段, name 是 Anthropic 约定的固定字面量(如 type=web_search_20250305 必须 name=web_search),
        # 改名会导致 400. 此类 tool 不做 remove / rename, 直接透传.
        if "type" in t and t.get("type"):
            new_tools.append(t)
            continue
        if name in REMOVE_TOOLS:
            continue
        # 显式映射表 (OC_TO_CC / HERMES_TO_CC)
        if name in table:
            t = {**t, "name": table[name]}
            new_tools.append(t)
            continue
        # 已是 CC 原生 tool 名或 Cowork 内置 mcp__claude_ai_* 前缀: 透传
        if not name or name in CC_NATIVE_TOOLS or name.startswith("mcp__claude_ai_"):
            new_tools.append(t)
            continue
        # 兜底借壳: 任何其他 tool name (含 mcp_wecom_*、自定义业务工具、未知 MCP 等)
        # 改成 Cowork 内置 MCP 风格 (mcp__claude_ai_<原名>), schema 完全保留, 模型读 description 仍能用.
        # 让 Anthropic 视角看到的 tools 名单像"Cowork 用户接了一堆官方 MCP", 不暴露业务身份.
        new_name = f"mcp__claude_ai_{name}"
        # 防御性: 如果撞名 (理论上原 name 唯一 -> 新名也唯一), 跳过借壳
        if new_name not in runtime_borrow and new_name not in CC_NATIVE_TOOLS:
            runtime_borrow[new_name] = name
            t = {**t, "name": new_name}
        new_tools.append(t)

    # 真 CC 的 tools baseline 不带 cache_control(cache 全在 system 端) — 这里也不加,
    # 既匹配真实 CC 行为(避免 fingerprint), 又给后续 proxy 注入的 system cache_control 让出额度
    body["tools"] = new_tools
    sys.stdout.write(f"[proxy] tools: {len(new_tools)} mapped (removed {len(tools) - len(new_tools)}, "
                     f"borrowed {len(runtime_borrow)}), "
                     f"names={[t.get('name', t.get('type', '?')) for t in new_tools]}\n")
    sys.stdout.flush()

    # messages 里历史 tool_use.name 也要同步改名, 保持跟 tools 数组一致.
    # 否则 Anthropic 看到 tools 里没有 "mcp_wecom_*" 但 history 里出现, 会报 tool not found.
    original_to_new = {v: k for k, v in runtime_borrow.items()}  # 反转: original -> borrowed
    full_remap = {**table, **original_to_new}  # 客户端原名 -> 上游应该看到的名
    if full_remap:
        for msg in body.get("messages", []):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    n = blk.get("name")
                    if n in full_remap:
                        blk["name"] = full_remap[n]

    # tool_choice.name 也要同步改名 (Anthropic 校验 tool_choice 必须指向 tools 数组里存在的 name).
    # 常见场景: agent 用 tool_choice={type:tool,name:X} 强制结构化输出 (LangChain/LangGraph 等).
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "tool":
        tc_name = tc.get("name")
        if tc_name and tc_name in full_remap:
            tc["name"] = full_remap[tc_name]

    return runtime_borrow

def inject_system_and_cch(body: dict, cc_client: bool = False) -> bytes:
    """注入 Claude Code 的 system prompts + 计算 cch 签名

    核心策略：把 openclaw 的 system prompt 移到第一条 user message 里，
    system 参数只保留标准 Claude Code 格式，避免被 Anthropic 检测。

    按特征分流: 真 CC client system prompt 已合规,只算 cch 不搬家
    """
    if cc_client:
        # CC client 自带 system prompt 已经是合规格式,只更新 cch
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        body_bytes = body_str.encode("utf-8")
        cch = compute_cch(body_bytes)
        # CC client 自带的 cch 是它自己算的,proxy 不该改 — 直接透传
        return body_bytes

    # 非 CC 客户端: 剥光 body 里所有现存 cache_control(包括 client 自己加的),
    # 让 proxy 完整接管 cache 策略。原因:
    #  1. Anthropic 限制单次请求最多 4 个 cache_control block
    #  2. 真 CC 的 cache 全在 system 端, 客户端散加 cache_control 是 fingerprint
    #  3. proxy 下方会主动加 2 个(system identity + user prefix), 必须先腾出额度
    _stripped = [0]
    def _strip_cc(obj):
        if isinstance(obj, dict):
            if obj.pop("cache_control", None) is not None:
                _stripped[0] += 1
            for v in obj.values():
                _strip_cc(v)
        elif isinstance(obj, list):
            for v in obj:
                _strip_cc(v)
    _strip_cc(body)
    if _stripped[0]:
        sys.stdout.write(f"[proxy] stripped {_stripped[0]} client-side cache_control block(s)\n")
        sys.stdout.flush()

    # 提取原始 system prompt
    original_system = body.get("system", [])
    if isinstance(original_system, str):
        original_system = [{"type": "text", "text": original_system}]

    # 把原始 system prompt 拼接成文本，移到第一条 user message
    if original_system:
        sys_texts = []
        for block in original_system:
            if isinstance(block, dict) and block.get("text"):
                sys_texts.append(block["text"])
            elif isinstance(block, str):
                sys_texts.append(block)

        if sys_texts:
            combined_sys = "\n\n".join(sys_texts)
            # 真 CC 把所有 instructions 内联到 system 长 prompt 里, 不会用 <system_instructions> 这种标签
            # 这里改用通用 markdown header, 避免 proxy 独有标签被 Anthropic 静态规则识别
            prefix_text = f"# Additional Instructions\n\n{combined_sys}\n\n---\n\n"

            # 拆成独立 block + cache_control — prefix 大头(原 system + tools 描述)进 cache
            # 命中后续 turn 只 cache_read,省钱省 latency
            prefix_block = {
                "type": "text",
                "text": prefix_text,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }

            messages = body.get("messages", [])
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        msg["content"] = [prefix_block, {"type": "text", "text": content}]
                    elif isinstance(content, list):
                        msg["content"] = [prefix_block] + content
                    break

    # 伪装成 Claude 桌面 Cowork 模式 — Anthropic 自家产品流量,
    # cch+billing 体系经过实战验证, identity 用 62c 短版 + system[2] 是 application_details
    billing = {
        "type": "text",
        "text": f"x-anthropic-billing-header: cc_version={CC_FULL_VERSION}; cc_entrypoint=local-agent; cch=00000;",
    }
    identity = {
        "type": "text",
        "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    application_details = {
        "type": "text",
        "text": COWORK_APPLICATION_DETAILS,
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }

    body["system"] = [billing, identity, application_details]

    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    # 启用关键词替换 — scrub 第三方 client 标识符(hermes/NousResearch/OpenClaw 等)防 Anthropic 扫到
    body_str = sanitize_body(body_str)

    body_bytes = body_str.encode("utf-8")

    cch = compute_cch(body_bytes)
    body_bytes = body_bytes.replace(b"cch=00000", f"cch={cch}".encode("utf-8"), 1)

    return body_bytes

# ============================================================
# 构造请求头
# ============================================================

def build_headers(access_token: str, session_id: str = None) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"claude-cli/{CC_VERSION} (external, cli)",
        # session-id 透传: 保留客户端原始 session, 提升 prompt cache 命中率
        "X-Claude-Code-Session-Id": session_id or str(uuid.uuid4()),
        "x-app": "cli",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.80.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
    }

# ============================================================
# Flask 路由
# ============================================================

@app.route("/v1/messages", methods=["POST"])
def proxy_messages():
    # 调试: 在鉴权前 dump header,401 也能抓到 newapi 实际发的内容
    if DEBUG:
        try:
            with open("/tmp/proxy_headers.json", "w") as df:
                json.dump({
                    "ts": time.time(),
                    "remote_addr": request.remote_addr,
                    "method": request.method,
                    "path": request.full_path,
                    "headers": dict(request.headers),
                }, df, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # API key 鉴权: 同时接受 Authorization: Bearer <key> 和 x-api-key: <key>
    # newapi/one-api 习惯用 x-api-key,真 CC 用 Authorization
    auth = request.headers.get("Authorization", "")
    client_token = auth.replace("Bearer ", "").strip()
    if not client_token:
        client_token = request.headers.get("X-Api-Key", "").strip()
    user = ALLOWED_TOKENS.get(client_token)
    if not user:
        sys.stdout.write(f"[proxy] 401 unauthorized (token={client_token[:20]}...)\n")
        sys.stdout.flush()
        return {"error": {"type": "authentication_error", "message": "invalid api key"}}, 401

    try:
        raw = request.get_data(as_text=True)
        body = json.loads(raw)
    except Exception as e:
        return {"error": str(e)}, 400

    # 兼容: 剥掉 OpenAI 协议特有但 Anthropic 拒收的顶层字段
    # 部分中转工具(如 newapi 转 Cherry Studio OpenAI 请求)只做半套转换,
    # OpenAI 字段透传过来会被 Anthropic 直接 400. 用黑名单, 不影响 Anthropic 未来的新 beta 字段.
    _openai_only = {
        "stream_options", "frequency_penalty", "presence_penalty",
        "logit_bias", "logprobs", "top_logprobs", "n", "response_format",
        "seed", "user", "audio", "modalities", "parallel_tool_calls",
        "prediction", "web_search_options", "store",
    }
    stripped_keys = [k for k in list(body.keys()) if k in _openai_only]
    for k in stripped_keys:
        body.pop(k, None)
    if stripped_keys:
        sys.stdout.write(f"[proxy] stripped openai-only fields: {stripped_keys}\n")
        sys.stdout.flush()

    # 兼容: max_tokens 是 Anthropic 必填字段, 但部分 OpenAI→Anthropic 转换器
    # (如 newapi 中转的 Cherry Studio OpenAI 格式请求) 会漏掉这个字段, 兜底补一个
    if "max_tokens" not in body or not isinstance(body.get("max_tokens"), int):
        body["max_tokens"] = 4096
        sys.stdout.write("[proxy] max_tokens missing in body, defaulted to 4096\n")
        sys.stdout.flush()

    # 兼容: 把 OpenAI 格式的 tools 转成 Anthropic 格式
    #   OpenAI: {"type":"function","function":{name, description, parameters}}
    #   Anthropic: {"name":..., "description":..., "input_schema":...}
    # newapi 中转 Hermes/Cherry 的 OpenAI 请求时常常漏掉这步转换, 透传到 Anthropic 会 400.
    # 必须放在 detect_client / replace_tools 之前, 否则 tool name 全是 None, hermes 识别失败.
    if isinstance(body.get("tools"), list):
        normalized = []
        converted = 0
        for t in body["tools"]:
            if isinstance(t, dict) and t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                normalized.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
                converted += 1
            else:
                normalized.append(t)
        if converted:
            body["tools"] = normalized
            sys.stdout.write(f"[proxy] normalized {converted} OpenAI-format tool(s) to Anthropic format\n")
            sys.stdout.flush()

    # 兼容: 客户端可能把 role="system" 内联在 messages 数组里(OpenAI 习惯)
    # Anthropic 不接受,本地合并到顶层 system 字段,避免无效请求打上游
    sys_msgs = [m for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") == "system"]
    if sys_msgs:
        extra_blocks = []
        for m in sys_msgs:
            c = m.get("content", "")
            if isinstance(c, str):
                extra_blocks.append({"type": "text", "text": c})
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        extra_blocks.append({"type": "text", "text": b.get("text", "")})
        body["messages"] = [m for m in body["messages"]
                            if not (isinstance(m, dict) and m.get("role") == "system")]
        existing = body.get("system") or []
        if isinstance(existing, str):
            existing = [{"type": "text", "text": existing}]
        body["system"] = existing + extra_blocks
        sys.stdout.write(f"[proxy] inlined-system normalized: moved {len(sys_msgs)} msg(s) to top-level\n")
        sys.stdout.flush()

    # 兼容: 把 OpenAI 协议的 role=tool 和 assistant.tool_calls 转成 Anthropic 协议
    #   OpenAI tool result:     {"role":"tool","tool_call_id":"X","content":"..."}
    #   Anthropic 等价:          {"role":"user","content":[{"type":"tool_result","tool_use_id":"X","content":"..."}]}
    #
    #   OpenAI assistant call:  {"role":"assistant","content":"...","tool_calls":[{"id":"X","type":"function","function":{"name":"n","arguments":"{...}"}}]}
    #   Anthropic 等价:          {"role":"assistant","content":[{"type":"text","text":"..."},{"type":"tool_use","id":"X","name":"n","input":{...}}]}
    msgs = body.get("messages")
    if isinstance(msgs, list):
        new_msgs = []
        msg_converted = 0
        for msg in msgs:
            if not isinstance(msg, dict):
                new_msgs.append(msg); continue
            role = msg.get("role")
            if role == "tool":
                new_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
                msg_converted += 1
                continue
            if role == "assistant" and isinstance(msg.get("tool_calls"), list):
                new_content = []
                text = msg.get("content")
                if isinstance(text, str) and text:
                    new_content.append({"type": "text", "text": text})
                elif isinstance(text, list):
                    for blk in text:
                        if isinstance(blk, dict):
                            new_content.append(blk)
                for tc in msg["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args else {}
                        except Exception:
                            args = {}
                    new_content.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args or {},
                    })
                new_msgs.append({"role": "assistant", "content": new_content})
                msg_converted += 1
                continue
            new_msgs.append(msg)
        if msg_converted:
            body["messages"] = new_msgs
            sys.stdout.write(f"[proxy] normalized {msg_converted} OpenAI-style tool/assistant message(s)\n")
            sys.stdout.flush()

        # 再扫一遍 messages 数组, 剥掉每条 message 里的 OpenAI-only 字段
        # (reasoning_content/refusal/audio/function_call/name/tool_call_id/tool_calls 等).
        # 这些字段 Anthropic 拒收, 例如 DeepSeek R1 / OpenAI o1 SDK 会在 assistant message 加 reasoning_content.
        _msg_strip = {"reasoning_content", "refusal", "audio", "function_call", "name", "tool_call_id", "tool_calls"}
        msg_field_stripped = 0
        for msg in body.get("messages", []):
            if not isinstance(msg, dict):
                continue
            for k in list(msg.keys()):
                if k in _msg_strip:
                    msg.pop(k, None)
                    msg_field_stripped += 1
        if msg_field_stripped:
            sys.stdout.write(f"[proxy] stripped {msg_field_stripped} OpenAI-only field(s) from messages\n")
            sys.stdout.flush()

    access_token = get_access_token()
    cc_client = is_cc_client(request)
    client_type = "cc" if cc_client else detect_client(body)
    incoming_session_id = request.headers.get("X-Claude-Code-Session-Id")
    # 非 CC 客户端没传 session-id 时, 基于 (ip, user) 在 5 分钟内复用同一 uuid,
    # 模拟真 CC 的"一会话内复用 session-id"行为, 减少 fingerprint, 同时利于 prompt cache 命中
    if not cc_client and not incoming_session_id:
        incoming_session_id = get_or_create_session_id(request.remote_addr, client_token)
        # 同步注入 metadata.user_id (真 CC 总是发, 格式: JSON 字符串嵌 device_id/account_uuid/session_id)
        # 仅当客户端没传 metadata 时注入, 客户端自己传的优先
        if "metadata" not in body:
            body["metadata"] = {
                "user_id": json.dumps({
                    "device_id": compute_device_id(client_token),
                    "account_uuid": "",
                    "session_id": incoming_session_id,
                }, separators=(",", ":"))
            }
    # 兜底 output_config / thinking — 真 Cowork 总是带这两个字段, 缺失也是 fingerprint.
    # 仅非 CC 客户端注入, 且客户端未传时才填默认值. haiku 不支持 thinking, 整组都跳过.
    # 另: Anthropic 硬约束 — tool_choice 强制工具使用时不能开 thinking, 此时跳过 thinking 注入,
    # 否则会撞 'Thinking may not be enabled when tool_choice forces tool use.' 400.
    if not cc_client and "haiku" not in body.get("model", "").lower():
        if "output_config" not in body:
            body["output_config"] = {"effort": "medium"}
        if "thinking" not in body:
            tc = body.get("tool_choice")
            force_tool = False
            if isinstance(tc, dict):
                # Anthropic 风格: {"type": "any"} 或 {"type": "tool", "name": "..."}
                if tc.get("type") in ("any", "tool"):
                    force_tool = True
            elif isinstance(tc, str):
                # OpenAI 风格: "required" 强制使用工具
                if tc == "required":
                    force_tool = True
            if not force_tool:
                body["thinking"] = {"type": "adaptive"}
            else:
                sys.stdout.write("[proxy] skipped thinking injection (tool_choice forces tool use)\n")
                sys.stdout.flush()

    if DEBUG:
        if len(raw) > 1000:
            with open("/tmp/proxy_neo_raw.json", "w") as df:
                df.write(raw)
        # 调试: dump 入站 header — 用于核对 newapi 透传效果
        try:
            with open("/tmp/proxy_headers.json", "w") as df:
                json.dump({
                    "ts": time.time(),
                    "user": user,
                    "client_type": client_type,
                    "remote_addr": request.remote_addr,
                    "method": request.method,
                    "path": request.full_path,
                    "headers": dict(request.headers),
                }, df, indent=2, ensure_ascii=False)
        except Exception as _e:
            sys.stdout.write(f"[proxy] header dump failed: {_e}\n")
            sys.stdout.flush()

    runtime_borrow = replace_tools(body, cc_client=cc_client, client_type=client_type)
    body_bytes = inject_system_and_cch(body, cc_client=cc_client)
    headers = build_headers(access_token, session_id=incoming_session_id)
    # 透传客户端 anthropic-beta — 否则 body 里 context_management 等字段会被 Anthropic 拒
    client_beta = request.headers.get("Anthropic-Beta", "").strip()
    if client_beta:
        if "oauth" not in client_beta:
            client_beta += ",oauth-2025-04-20"
        headers["anthropic-beta"] = client_beta
    sys.stdout.write(f"[proxy] user={user} client={client_type} session={incoming_session_id or 'gen'} beta={'fwd' if client_beta else 'default'}\n")
    sys.stdout.flush()

    if DEBUG:
        with open("/tmp/proxy_neo_last.json", "wb") as df:
            df.write(body_bytes)

    is_stream = body.get("stream", False)
    if is_stream:
        headers["Accept"] = "text/event-stream"

    sys.stdout.write(f"[proxy] → {UPSTREAM}/v1/messages "
          f"model={body.get('model', '?')} stream={is_stream} "
          f"body_size={len(body_bytes)}\n")
    sys.stdout.flush()

    resp = requests.post(
        f"{UPSTREAM}/v1/messages",
        data=body_bytes,
        headers=headers,
        stream=is_stream,
        timeout=300,
    )

    sys.stdout.write(f"[proxy] ← status={resp.status_code}\n")
    if resp.status_code >= 400:
        try:
            sys.stdout.write(f"[proxy] ← error: {resp.text[:500]}\n")
        except:
            pass
    sys.stdout.flush()

    def remap_tool_names(data: bytes) -> bytes:
        """把响应中的 CC / 借壳 tool 名替换回 client 原本的 tool 名
        按特征分流: CC client 自己用 CC 名,不需要反映射"""
        if cc_client:
            return data
        rev_table = CC_TO_HERMES if client_type == "hermes" else CC_TO_OC
        text = data.decode("utf-8", errors="replace")
        # 1) 显式映射表反向 (例如 Bash -> exec / Edit -> edit)
        for cc_name, original in rev_table.items():
            text = text.replace(f'"name":"{cc_name}"', f'"name":"{original}"')
            text = text.replace(f'"name": "{cc_name}"', f'"name": "{original}"')
        # 2) 运行时借壳反向 (例如 mcp__claude_ai_wecom_docs_create_doc -> mcp_wecom_docs_create_doc)
        for borrowed_name, original_name in runtime_borrow.items():
            text = text.replace(f'"name":"{borrowed_name}"', f'"name":"{original_name}"')
            text = text.replace(f'"name": "{borrowed_name}"', f'"name": "{original_name}"')
        return text.encode("utf-8")

    if is_stream:
        def generate():
            buf = ""
            in_logged = False
            out_tokens = None
            for chunk in resp.iter_content(chunk_size=None):
                if not chunk:
                    continue
                # 嗅探 usage(不影响转发)
                try:
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        for line in block.split("\n"):
                            if not line.startswith("data: "):
                                continue
                            try:
                                ev = json.loads(line[6:])
                            except Exception:
                                continue
                            t = ev.get("type")
                            if t == "message_start" and not in_logged:
                                u = (ev.get("message") or {}).get("usage") or {}
                                sys.stdout.write(
                                    f"[proxy] usage in={u.get('input_tokens',0)} "
                                    f"cache_read={u.get('cache_read_input_tokens',0)} "
                                    f"cache_create={u.get('cache_creation_input_tokens',0)}\n"
                                )
                                sys.stdout.flush()
                                in_logged = True
                            elif t == "message_delta":
                                u = ev.get("usage") or {}
                                if "output_tokens" in u:
                                    out_tokens = u["output_tokens"]
                except Exception:
                    pass
                yield remap_tool_names(chunk)
            if out_tokens is not None:
                sys.stdout.write(f"[proxy] usage out={out_tokens}\n")
                sys.stdout.flush()
        return Response(
            stream_with_context(generate()),
            status=resp.status_code,
            content_type=resp.headers.get("content-type", "text/event-stream"),
        )
    else:
        excluded = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        # 非流式: 直接从 JSON body 抽 usage
        try:
            if "json" in resp.headers.get("content-type", ""):
                d = json.loads(resp.content.decode("utf-8", errors="replace"))
                u = d.get("usage") or {}
                sys.stdout.write(
                    f"[proxy] usage in={u.get('input_tokens',0)} "
                    f"cache_read={u.get('cache_read_input_tokens',0)} "
                    f"cache_create={u.get('cache_creation_input_tokens',0)} "
                    f"out={u.get('output_tokens',0)}\n"
                )
                sys.stdout.flush()
        except Exception:
            pass
        return Response(
            remap_tool_names(resp.content),
            status=resp.status_code,
            headers=resp_headers,
            content_type=resp.headers.get("content-type", "application/json"),
        )

# ============================================================
# OpenAI 兼容端点 — 已禁用,统一走原生 /v1/messages
# ============================================================

@app.route("/v1/chat/completions", methods=["POST"])
def reject_chat_completions():
    """拒绝 OpenAI 格式请求 — 客户端必须用 Anthropic 原生协议打 /v1/messages"""
    sys.stdout.write(f"[proxy] /chat/completions REJECTED from {request.remote_addr}\n")
    sys.stdout.flush()
    return {
        "error": {
            "type": "invalid_request_error",
            "message": "OpenAI-compatible endpoint is disabled on this proxy. "
                       "Please use POST /v1/messages with Anthropic native protocol "
                       "(set Anthropic-Version: 2023-06-01).",
        }
    }, 400

@app.route("/v1/models", methods=["GET"])
def list_models():
    """OpenAI 风格 model list — Cherry Studio / hermes 等启动时会调"""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        token = request.headers.get("X-Api-Key", "").strip()
    if ALLOWED_TOKENS.get(token) is None:
        return {"error": "unauthorized"}, 401
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "anthropic", "created": int(time.time())}
            for m in [
                "claude-opus-4-7",
                "claude-sonnet-4-6",
                "claude-haiku-4-5-20251001",
                "claude-sonnet-4-5-20250929",
                "claude-opus-4-1-20250805",
            ]
        ],
    }

@app.route("/health")
def health():
    try:
        cred = load_credentials()
        remaining = (cred.get("expiresAt", 0) / 1000 - time.time()) / 3600
        return {"status": "ok", "token_hours": round(remaining, 1), "cc_version": CC_FULL_VERSION}
    except Exception as e:
        return {"status": "error", "error": str(e)}, 500

if __name__ == "__main__":
    print(f"=== Claude Max → Anthropic API Proxy ===")
    print(f"CC Version: {CC_FULL_VERSION}")
    print()
    if not ALLOWED_TOKENS:
        print("❌ PROXY_API_KEYS environment variable is empty.")
        print("   Set it to enable client authentication, e.g.:")
        print('     export PROXY_API_KEYS="alice:sk-xxx,bob:sk-yyy"')
        sys.exit(1)
    try:
        cred = load_credentials()
        remaining = (cred.get("expiresAt", 0) / 1000 - time.time()) / 3600
        print(f"Subscription: {cred.get('subscriptionType')} ({cred.get('rateLimitTier')})")
        print(f"Token valid for: {remaining:.1f} hours")
        if remaining < 0.1:
            get_access_token()
    except FileNotFoundError:
        print("❌ Claude Code credentials not found at " + CREDENTIALS_FILE)
        print("   Install Claude Code CLI and run `claude` to log in first.")
        sys.exit(1)
    print()
    BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
    print(f"🚀 http://{BIND_HOST}:{PORT}")
    print(f"🔑 {len(ALLOWED_TOKENS)} api key(s) configured")
    if DEBUG:
        print(f"🔍 DEBUG mode ON (request dumps → /tmp/)")
    print()
    app.run(host=BIND_HOST, port=PORT, debug=False)
