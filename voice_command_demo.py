# -*- coding: utf-8 -*-
"""
voice_command_demo.py —— 语音命令控制：说“打开图片”即打开桌面/图片文件夹里的图片

链路: M260C 音箱唤醒(板内唤醒引擎) -> XFM 麦克风录音(边录边识别) -> Vosk 本地中文识别
      -> 匹配命令词 -> 执行对应 Python 函数(此例用 xdg-open 打开图片)

日志: 所有状态带时间戳实时输出; 中间识别结果会实时刷新([识别中] ...), 定稿后打印 [识别]
     可用 --log-file 把日志同时写入文件

依赖(Vosk 已装在 lerobot conda 环境, 用该环境的 python 运行):
    /home/kf/miniconda3/envs/lerobot/bin/python voice_command_demo.py
    模型: models/vosk-model-small-cn-0.22 (见 .gitignore 的 models 忽略项)

用法:
    <lerobot-python> voice_command_demo.py                    # 唤醒后说“打开图片”
    <lerobot-python> voice_command_demo.py --duration 5       # 录音窗口 5 秒
    <lerobot-python> voice_command_demo.py --log-file run.log # 日志同时落盘
    <lerobot-python> voice_command_demo.py --wav a.wav        # 直接识别已有录音(不连硬件)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import wave

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
from mic_serial import MicSerial, MSG_SHAKE, MSG_AIUI, SerialTimeoutError
from voice_interact_test import (ala_card_ids, find_angles, find_values,
                                 has_key, pulse_source_for_xfm)

MODEL_DIR = os.path.join(PROJECT_DIR, "models", "vosk-model-small-cn-0.22")
AUDIO_DIR = os.path.join(PROJECT_DIR, "audio")
WAV_RATE = 16000
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".svg")
# 命令词表: Vosk 中文模型词表是按“字”的, 语法需按字用空格分隔, 只在这几句里挑最像的
VOSK_GRAMMAR = '["打 开 图 片", "打 开 照 片", "看 一 下 图 片", "[unk]"]'

# 桌面优先, 无图则回退到图片文件夹
IMAGE_DIRS = [os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.join(os.path.expanduser("~"), "Pictures")]


# ---------------------------------------------------------------- 日志
_log_fp = None


def log(msg: str):
    """带时间戳的实时日志(控制台 + 可选日志文件)"""
    line = "%s %s" % (time.strftime("[%H:%M:%S]"), msg)
    print(line, flush=True)
    if _log_fp:
        _log_fp.write(line + "\n")
        _log_fp.flush()


# ---------------------------------------------------------------- 具体动作
def find_newest_image():
    """在 桌面 -> 图片文件夹 中找最新的图片文件, 返回路径或 None"""
    for d in IMAGE_DIRS:
        if not os.path.isdir(d):
            continue
        found = []
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(IMAGE_EXT) and not f.startswith("."):
                    p = os.path.join(root, f)
                    found.append((os.path.getmtime(p), p))
        if found:
            found.sort(reverse=True)
            return found[0][1]
    return None


def open_newest_image(*_ignored):
    """打开最新的图片(桌面优先, 回退图片文件夹)"""
    path = find_newest_image()
    if not path:
        log("[命令] 未找到图片: 请在 ~/Desktop 或 ~/Pictures 放一张图片")
        return
    log(f"[命令] 打开图片: {path}")
    subprocess.Popen(["xdg-open", path], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# 命令词 -> 动作; 想加功能只需在此登记一行
COMMANDS = [
    (("图片", "照片", "看图", "图像", "图"), open_newest_image),
]


def dispatch(text: str):
    """把识别文本映射到动作并执行; 返回是否命中"""
    for keywords, action in COMMANDS:
        if any(k in text for k in keywords):
            action(text)
            return True
    log(f"[命令] 未匹配到动作: {text!r}")
    return False


# ---------------------------------------------------------------- Vosk 识别
def load_recognizer():
    if not os.path.isdir(MODEL_DIR):
        log(f"[错误] 未找到 Vosk 模型: {MODEL_DIR}")
        log("       下载: https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip")
        sys.exit(1)
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    model = Model(MODEL_DIR)
    log(f"[模型] 已加载 {MODEL_DIR}")
    # 语法限制 + 16k 采样率(与录音一致)
    return KaldiRecognizer(model, WAV_RATE, VOSK_GRAMMAR)


def parse_text(js: str) -> str:
    """把 Vosk 返回的 JSON 转成干净文本(去空格与 [unk])"""
    return json.loads(js).get("text", "").replace(" ", "").replace("[unk]", "")


def recognize_wav(rec, wav_path: str):
    """识别已有 16k 单声道 WAV, 返回识别文本"""
    with wave.open(wav_path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            log("[错误] 仅支持 16bit 单声道 WAV")
            return ""
        rec.Reset()
        segments = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                seg = parse_text(rec.Result())      # 分段定稿结果
                if seg:
                    segments.append(seg)
                    log(f"[识别] 片段: {seg}")
        # FinalResult 只包含尾段, 必须与已定稿的分段拼接
        text = "".join(segments) + parse_text(rec.FinalResult())
    log(f"[识别] 最终: {text or '(无有效语音)'}")
    return text


# ---------------------------------------------------------------- 边录边识别
def build_stream_cmd(seconds: int):
    """构造 raw 音频流录音命令(便于边录边识别): 优先 PulseAudio 的 XFM 源"""
    src = pulse_source_for_xfm()
    common = ["-f", "S16_LE", "-r", str(WAV_RATE), "-c", "1",
              "-t", "raw", "-d", str(seconds), "-"]
    if src:
        return (["arecord", "-D", "default"] + common,
                dict(os.environ, PULSE_SOURCE=src), f"PulseAudio 源 {src}")
    cap = ala_card_ids("arecord")
    xfm = next((c for c, d in cap.items() if any("XFM" in x for x in d)), None)
    if xfm:
        return (["arecord", "-D", f"plughw:CARD={xfm},DEV=0"] + common,
                None, f"ALSA 卡 {xfm}")
    return (["arecord"] + common, None, "系统默认录音设备")


def stream_recognize(rec, seconds: int, wav_path: str):
    """录音的同时喂给 Vosk: 实时刷新中间结果, 结束后返回最终文本"""
    cmd, env, desc = build_stream_cmd(seconds)
    log(f"[录音] 开始 {seconds}s ({desc}), 请说命令词…")
    rec.Reset()
    segments = []
    partial_show = ""
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(WAV_RATE)
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(4000)
                if not chunk:
                    break
                wf.writeframes(chunk)                      # 原始音频留档
                if rec.AcceptWaveform(chunk):
                    seg = parse_text(rec.Result())         # 分段定稿, 实时打印
                    if seg:
                        print("\r" + " " * (len(partial_show) + 12) + "\r",
                              end="", flush=True)
                        segments.append(seg)
                        log(f"[识别] {seg}")
                        partial_show = ""
                else:                                      # 实时中间结果
                    cur = json.loads(rec.PartialResult()).get("partial", "").replace(" ", "")
                    cur = cur.replace("[unk]", "")
                    if cur and cur != partial_show:
                        partial_show = cur
                        print(f"\r[识别中] {cur} …", end="", flush=True)
        finally:
            proc.wait()
    if partial_show:                                       # 清掉中间结果行
        print("\r" + " " * (len(partial_show) + 12) + "\r", end="", flush=True)
    # FinalResult 只含尾段, 需与已定稿分段拼接
    text = "".join(segments) + parse_text(rec.FinalResult())
    log(f"[识别] 最终: {text or '(无有效语音)'}")
    return text


# ---------------------------------------------------------------- 主流程
def run_live(args, rec):
    """唤醒 -> 边录边识别 -> 执行命令"""
    try:
        mic = MicSerial(args.port).open() if args.port else None
    except OSError as e:
        log(f"[错误] 打开串口失败: {e}")
        sys.exit(1)
    if mic is None:
        port, mic = MicSerial.autodetect(timeout=1.5)
        if not port:
            log("[错误] 未找到降噪板串口")
            sys.exit(1)
        mic = mic or MicSerial(port).open()
    if not os.path.isdir(AUDIO_DIR):
        os.makedirs(AUDIO_DIR)

    log("=" * 60)
    log("语音命令演示: 唤醒后说“打开图片”")
    log(f"[串口] {mic.port} @115200")
    log(f"[识别] Vosk 本地离线识别(语法限制), 录音时长 {args.duration}s")
    log("=" * 60)

    last_ack_t = 0.0
    recent_wakes = {}
    while True:
        try:
            for typ, sid, payload in mic.read_frames(timeout=0.5):
                if typ == MSG_SHAKE:
                    now = time.time()
                    if now - last_ack_t >= 1.0:      # 握手确认节流, 防写缓冲积压
                        mic.ack_handshake(typ, sid, payload)
                        last_ack_t = now
                    continue
                if typ != MSG_AIUI:
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                angles = find_angles(obj)
                if not (has_key(obj, "ivw") or angles):
                    continue
                stamp = tuple(find_values(obj, "start_ms"))    # 固件会重发同一唤醒
                now = time.time()
                if stamp and stamp in recent_wakes and now - recent_wakes[stamp] < 10:
                    continue
                if stamp:
                    recent_wakes[stamp] = now

                angle = sorted(set(angles))[0] if angles else None
                log(f"[唤醒] 声源角度: {angle if angle is not None else '未知'}° "
                    f"→ 请说命令词(如“打开图片”)")
                wav = os.path.join(AUDIO_DIR, time.strftime("cmd_%Y%m%d_%H%M%S.wav"))
                text = stream_recognize(rec, args.duration, wav)
                if text:
                    dispatch(text)
                else:
                    log("[命令] 未识别到内容, 忽略")
        except (OSError, SerialTimeoutError) as e:
            log(f"[警告] 串口异常({e}), 3 秒后重连...")
            time.sleep(3)
            try:
                mic.close()
            except OSError:
                pass
            while True:
                try:
                    port, mic = MicSerial.autodetect(timeout=1.5)
                    mic = mic or MicSerial(port).open()
                    break
                except (OSError, SerialTimeoutError, TypeError):
                    time.sleep(3)
            last_ack_t = 0.0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M260C 语音命令演示(打开图片)")
    ap.add_argument("--port", help="降噪板串口(默认自动检测)")
    ap.add_argument("--duration", type=int, default=3, help="唤醒后录音秒数(默认3)")
    ap.add_argument("--log-file", help="同时把日志写入该文件")
    ap.add_argument("--wav", help="直接识别已有 WAV(不连硬件), 用于验证")
    a = ap.parse_args()

    if a.log_file:
        _log_fp = open(a.log_file, "a", encoding="utf-8")
    rec = load_recognizer()
    if a.wav:
        recognize_wav(rec, a.wav)
    else:
        run_live(a, rec)
