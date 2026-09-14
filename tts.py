# -*- coding: utf-8 -*-
"""
tts.py —— 文字转语音并播报到音箱(用于"命令无法处理"时的兜底提示)

链路: edge-tts(微软在线, 中文音质好) --mp3--> ffmpeg 转 WAV --paplay--> C-Media 扬声器
      合成结果按 文本+音色 做哈希缓存到 audio/tts_cache/, 之后离线也能直接播.
      网络/转换失败时退回系统 spd-say(离线, 机械音, 路由由 speech-dispatcher 决定).

用法:
    from tts import speak
    speak("暂时还处理不了")
"""
import asyncio
import hashlib
import os
import shutil
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
from voice_interact_test import playback_cmds        # 复用: PA sink 优先, 回退 ALSA plughw

CACHE_DIR = os.path.join(PROJECT_DIR, "audio", "tts_cache")
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"               # 微软中文女声


def _cache_path(text: str, voice: str) -> str:
    key = hashlib.sha1((voice + "|" + text).encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{key}.wav")


def _synth_edge(text: str, voice: str, out_wav: str) -> bool:
    """edge-tts 合成 mp3, 再用 ffmpeg 转成 44.1k 立体声 WAV(与音箱 sink 匹配)"""
    try:
        import edge_tts
    except Exception:
        return False
    tmp_mp3 = out_wav + ".mp3"
    try:
        async def _run():
            await edge_tts.Communicate(text, voice).save(tmp_mp3)
        asyncio.run(_run())
        if not os.path.exists(tmp_mp3) or os.path.getsize(tmp_mp3) < 500:
            return False
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_mp3,
                            "-ar", "44100", "-ac", "2", out_wav],
                           capture_output=True, text=True)
        return r.returncode == 0 and os.path.exists(out_wav)
    except Exception:
        return False
    finally:
        if os.path.exists(tmp_mp3):
            try:
                os.remove(tmp_mp3)
            except OSError:
                pass


def _play(wav: str) -> bool:
    for cmd in playback_cmds(wav):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True
    return False


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

    if not os.path.exists(wav):                      # 未命中缓存 -> 在线合成
        if not _synth_edge(text, voice, wav):
            if shutil.which("spd-say"):              # 离线兜底(机械音, 路由不可控)
                if not quiet:
                    print("[TTS] edge-tts 不可用, 退回 spd-say")
                subprocess.run(["spd-say", "-l", "zh", "-w", text])
                return True, "spd-say"
            if not quiet:
                print("[TTS] 合成失败: edge-tts 与 spd-say 均不可用")
            return False, "失败"

    ok = _play(wav)
    if not quiet:
        print(f"[TTS] {'播放' if ok else '播放失败'}: {text} -> {wav}")
    return ok, "edge-tts"


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "暂时还处理不了"
    print(speak(msg))
