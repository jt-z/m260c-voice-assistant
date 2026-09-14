# -*- coding: utf-8 -*-
"""
llm.py —— 把"命令词没匹配上"的话交给 DeepSeek 大模型回答(纯问答, 不执行任何动作)

链路: 识别文本 -> DeepSeek(OpenAI 兼容 HTTP API) -> 回复文本 -> tts.speak 播报

key 来源(按优先级):
    1) 环境变量 DEEPSEEK_API_KEY
    2) 本文件同目录的 .deepseek_key(文件里只放一行 key, 已加进 .gitignore)
都没配时进入"桩模式": 不请求真模型, 只回一句固定话术, 便于先把链路跑通.

回复风格约束: 口语化、简短、无 markdown/emoji/列表 —— 因为要被直接念出来.
"""
import os
import time

import requests

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(PROJECT_DIR, ".deepseek_key")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"
TIMEOUT = (5, 60)          # (连接超时, 读取超时) 秒; 推理模型出字慢, 读取给足
ATTEMPTS = 2               # 网络抖动重试次数
# deepseek-v4-flash 是【推理模型】: 会先花大量 token 写 reasoning_content(思维链),
# 然后才产出 content。max_tokens 是两者之和 —— 设小了思维链就会把额度吃光,
# 导致 content 为空、finish_reason=length。这里必须给足余量。
MAX_TOKENS = 2048
MAX_SPEAK_CHARS = 120      # 播报长度上限, 超出截断

SYSTEM_PROMPT = (
    "你是一个中文语音助手, 你的回答会被音箱直接念出来, 用户看不到文字。"
    "要求: 口语化、简短, 控制在 60 字以内; 不要用 markdown、不要 emoji、"
    "不要分点列举、不要念出标点或符号的名称; 直接给结论, 不要客套和重复问题。"
)


def load_key() -> str:
    """按优先级取 API key: 环境变量 -> 项目内 .deepseek_key 文件; 取不到返回空串"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, encoding="utf-8") as fp:
                return fp.readline().strip()
        except OSError:
            pass
    return ""


def is_stub() -> bool:
    """没有配置 key -> 桩模式"""
    return not load_key()


def _clip(text: str) -> str:
    """压掉换行/连续空白, 并截断到播报上限"""
    text = " ".join(text.split())
    if len(text) > MAX_SPEAK_CHARS:
        text = text[:MAX_SPEAK_CHARS] + "……"
    return text


def _stub_reply(text: str) -> str:
    """桩模式回复: 一听就知道是桩, 但足以验证"识别->回答->播报"整条链路"""
    return "桩模式收到，你说的是%s" % text


def ask(text: str) -> str:
    """问 DeepSeek, 返回"要播报的回复文本"。失败抛 RuntimeError, 由调用方兜底。"""
    text = text.strip()
    if not text:
        return ""
    key = load_key()
    if not key:
        return _stub_reply(text)

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    headers = {"Authorization": "Bearer " + key,
               "Content-Type": "application/json"}
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = requests.post(API_URL, json=payload, headers=headers, timeout=TIMEOUT)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:200]))
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            reply = _clip(msg.get("content") or "")
            if not reply:
                usage = data.get("usage") or {}
                r_tok = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                raise RuntimeError(
                    "模型返回空 content(finish_reason=%s, reasoning_tokens=%s) —— "
                    "通常是思维链吃光了 max_tokens, 可调大 llm.MAX_TOKENS"
                    % (choice.get("finish_reason"), r_tok))
            return reply
        except Exception as e:
            last = e
            if attempt < ATTEMPTS:
                print(f"[大模型] 第 {attempt} 次调用失败({type(e).__name__}: {e}), 重试…")
                time.sleep(0.8 * attempt)
    raise RuntimeError("DeepSeek 调用失败: %s: %s" % (type(last).__name__, last))


if __name__ == "__main__":
    import sys
    print("桩模式" if is_stub() else "已配置 key, 模型=%s" % MODEL)
    print(ask(sys.argv[1] if len(sys.argv) > 1 else "你好"))
