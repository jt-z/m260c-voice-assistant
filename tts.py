# -*- coding: utf-8 -*-
"""
tts.py —— 文字转语音并播报到音箱(用于"命令无法处理"时的兜底提示)

链路: edge-tts(微软在线, 中文音质好) --mp3--> ffmpeg 转 WAV --paplay--> C-Media 扬声器
      合成结果按 文本+音色 做哈希缓存到 audio/tts_cache/, 之后离线也能直接播.
      合成带超时与重试(网络抖动很常见); 仍失败则跳过本次播报(不再退 spd-say,
      因为 espeak-ng 读中文极难听且阻塞数秒).

用法:
    from tts import speak
    speak("暂时还处理不了")
"""
import asyncio
import hashlib
import os
import subprocess
import sys
import threading
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
from voice_interact_test import playback_cmds        # 复用: PA sink 优先, 回退 ALSA plughw

CACHE_DIR = os.path.join(PROJECT_DIR, "audio", "tts_cache")
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"               # 微软中文女声


def _cache_path(text: str, voice: str) -> str:
    key = hashlib.sha1((voice + "|" + text).encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{key}.wav")


SYNTH_ATTEMPTS = 3         # 合成尝试次数(edge-tts 走网络, 抖动很常见)
SYNTH_TIMEOUT = 15.0       # 单次合成的超时(秒), 超时即重试


def _ffmpeg_exe() -> str:
    """优先用系统 ffmpeg(/usr/bin/ffmpeg)。
    conda/工具链的 PATH 里可能藏着一些裁剪过的 ffmpeg 构建, 它们会报
    "Unknown input format: 'mp3'"(缺 mp3 demuxer), 所以别只依赖 PATH。"""
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return "ffmpeg"                      # 系统没有就回退 PATH


def _ffmpeg_env() -> dict:
    """去掉可能劫持 ffmpeg 动态库的环境变量(某些 conda/工具链会污染它们)"""
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(k, None)
    return env


def _looks_like_mp3(path: str) -> bool:
    """粗查 MP3 头(帧同步 0xFF Ex 或 ID3 标签), 用来尽早发现 edge-tts 写出的非音频数据"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(3)
    except OSError:
        return False
    if head[:3] == b"ID3":
        return True
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def _synth_edge(text: str, voice: str, out_wav: str) -> bool:
    """edge-tts 合成 mp3, 再用 ffmpeg 转成 44.1k 立体声 WAV(与音箱 sink 匹配)。
    网络不稳时重试 SYNTH_ATTEMPTS 次, 每次间隔递增; 全部失败返回 False。"""
    try:
        import edge_tts
    except Exception:
        return False
    tmp_mp3 = out_wav + ".mp3"
    for attempt in range(1, SYNTH_ATTEMPTS + 1):
        try:
            async def _run():
                await asyncio.wait_for(
                    edge_tts.Communicate(text, voice).save(tmp_mp3), SYNTH_TIMEOUT)
            asyncio.run(_run())
            if not os.path.exists(tmp_mp3) or os.path.getsize(tmp_mp3) < 500:
                raise RuntimeError("合成结果为空")
            if not _looks_like_mp3(tmp_mp3):
                raise RuntimeError("edge-tts 写出的不是 MP3 数据(前 3 字节异常, 大小 %d)"
                                   % os.path.getsize(tmp_mp3))
            r = subprocess.run([_ffmpeg_exe(), "-y", "-loglevel", "error",
                                "-i", tmp_mp3,
                                "-ar", "44100", "-ac", "2", out_wav],
                               capture_output=True, text=True, env=_ffmpeg_env())
            if r.returncode != 0 or not os.path.exists(out_wav):
                if os.path.exists(out_wav):        # 别把半成品 wav 留在缓存里
                    try:
                        os.remove(out_wav)
                    except OSError:
                        pass
                raise RuntimeError("ffmpeg 转码失败 rc=%s: %s"
                                   % (r.returncode,
                                      (r.stderr or "").strip()[-300:] or "(ffmpeg 无 stderr 输出)"))
            return True
        except Exception as e:
            if attempt < SYNTH_ATTEMPTS:
                print(f"[TTS] 合成第 {attempt} 次失败({type(e).__name__}: {e}), 重试…")
                time.sleep(0.8 * attempt)
            else:
                print(f"[TTS] 合成 {SYNTH_ATTEMPTS} 次均失败: {type(e).__name__}: {e}")
        finally:
            if os.path.exists(tmp_mp3):
                try:
                    os.remove(tmp_mp3)
                except OSError:
                    pass
    return False


def _play(wav: str) -> bool:
    for cmd in playback_cmds(wav):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True
    return False


# 只串行化"播放"这一步: 合成可以并行(省时间), 但两段语音绝不能同时念出来.
_play_lock = threading.Lock()


def prewarm(text: str, voice: str = DEFAULT_VOICE) -> bool:
    """只合成不播放(用于启动预热): 命中缓存则直接返回 True"""
    if not text.strip():
        return False
    os.makedirs(CACHE_DIR, exist_ok=True)
    wav = _cache_path(text, voice)
    if os.path.exists(wav):
        return True
    return _synth_edge(text, voice, wav)


def speak(text: str, voice: str = DEFAULT_VOICE, quiet: bool = False):
    """把 text 播报出来. 返回 (是否成功, 用的方式)"""
    if not text.strip():
        return False, "空文本"
    os.makedirs(CACHE_DIR, exist_ok=True)
    wav = _cache_path(text, voice)

    if not os.path.exists(wav):                      # 未命中缓存 -> 在线合成(含重试)
        if not _synth_edge(text, voice, wav):
            if not quiet:
                print(f"[TTS] 合成失败, 跳过本次播报: {text}")
            return False, "合成失败"

    with _play_lock:                                 # 播放互斥; 合成在上面已完成, 可并行
        ok = _play(wav)
    if not quiet:
        print(f"[TTS] {'播放' if ok else '播放失败'}: {text} -> {wav}")
    return ok, "edge-tts"


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "暂时还处理不了"
    print(speak(msg))
