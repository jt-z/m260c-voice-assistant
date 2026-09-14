# -*- coding: utf-8 -*-
"""
voice_command_demo.py —— 语音命令控制：
    说“打开图片”打开 / “关闭图片”关闭 图片；说“给我倒杯咖啡”后台拉起机械臂推理脚本

链路: M260C 音箱唤醒(板内唤醒引擎) -> XFM 麦克风录音(边录边识别) -> SenseVoice 中文识别
      -> 匹配命令词 -> 执行对应 Python 函数(此例用 xdg-open 打开、按路径关闭图片)

咖啡任务: 说“给我倒杯咖啡”会新开一个 gnome-terminal 窗口跑推理脚本, 日志在窗口里实时可见;
      窗口里先按回车开始, 运行中 n=提前结束并正常收尾, q=停止, Ctrl+C=中断。

大模型兜底(默认开): 命令词都没匹配上时, 把原话交给 DeepSeek 回答并播报(reply 仅播报,
      不执行任何动作)。key 取 DEEPSEEK_API_KEY 环境变量或本目录 .deepseek_key 文件;
      都没配则为"桩模式"(回一句固定话术), 便于先跑通链路。用 --no-llm 可关闭。

日志: 所有状态带时间戳实时输出, 常用状态提示:
      [状态] 监听中 / 已唤醒 / 识别中(带剩余秒数, 原地刷新) / 识别结果 / 已执行命令
      [诊断] 每 30s 汇总收帧情况(握手帧/设备事件/唤醒/忽略), 用于判断"喊了没反应"
      可用 --log-file 把日志同时写入文件

并发: 串口读取在独立线程(持续 ACK 握手 + 收事件), 业务线程只负责录音/识别/执行,
      因此录音那几秒不再是"盲区"; 若录音期间又听到唤醒, 会明确提示并忽略.

依赖(Vosk 已装在 lerobot conda 环境, 用该环境的 python 运行):
    /home/kf/miniconda3/envs/lerobot/bin/python voice_command_demo.py
    模型: models/vosk-model-small-cn-0.22 (见 .gitignore 的 models 忽略项)

当前板内唤醒词: 「你好宽宽」(ni2 hao3 kuan1 kuan1);
改唤醒词: python3 voice_interact_test.py --set-wakeword "..." (改完需拔插音箱)

用法:
    <lerobot-python> voice_command_demo.py                    # 默认 SenseVoice(实时文本+定稿)
    <lerobot-python> voice_command_demo.py --ui               # 实时 HUD 界面(PySide6)
    <lerobot-python> voice_command_demo.py --asr vosk         # 退回仅 Vosk(轻量, 逐段流式)
    <lerobot-python> voice_command_demo.py --fallback-say "没听懂"   # 自定义兜底播报语(留空关闭)
    <lerobot-python> voice_command_demo.py --no-llm           # 关闭大模型兜底(只播兜底语)
    <lerobot-python> voice_command_demo.py --duration 5       # 录音窗口 5 秒
    <lerobot-python> voice_command_demo.py --log-file run.log # 日志同时落盘
    <lerobot-python> voice_command_demo.py --coffee-script /path/xxx.sh   # 换咖啡任务脚本
    <lerobot-python> voice_command_demo.py --wav a.wav        # 直接识别已有录音(不连硬件)
    python3 bench_asr.py                                      # Vosk vs SenseVoice 基准对比
    python3 sound_radar_hud.py 30                             # 只预览 HUD(模拟数据, 不连硬件)

语音命令: 「打开图片」/「关闭图片」/「给我倒杯咖啡」(跑机械臂推理脚本, 说「停止」可中断)
识别: 默认 SenseVoice 一个引擎负责"界面/终端实时文本 + 最终定稿"(非流式, 用滚动重解码近似实时).
咖啡任务: 后台线程执行(不阻塞语音); 输出实时进日志; 完成/失败/超时/被停止 均 TTS 播报;
          执行期间只接受「停止」类指令; 脚本的 "按 ENTER" 确认会自动回车.
"""
import argparse
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
from mic_serial import (MicSerial, MSG_SHAKE, MSG_AIUI,
                        SerialTimeoutError, pretty_payload)
from audio_capture import AudioCapture
from voice_interact_test import (ala_card_ids, find_angles, find_values,
                                 has_key, pulse_source_for_xfm)

MODEL_DIR = os.path.join(PROJECT_DIR, "models", "vosk-model-small-cn-0.22")
AUDIO_DIR = os.path.join(PROJECT_DIR, "audio")
WAV_RATE = 16000
# 当前板内唤醒词(2026-09 通过 voice_interact_test.py --set-wakeword 改为「你好宽宽」;
# 出厂默认为「小微小微」= xiao3 wei1 xiao3 wei1)
WAKE_WORD_TEXT = "你好宽宽"
WAKE_WORD_PINYIN = "ni2 hao3 kuan1 kuan1"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".svg")
# 命令词表: Vosk 中文模型词表是按“字”的, 语法需按字用空格分隔, 只在这几句里挑最像的
VOSK_GRAMMAR = ('["打 开 图 片", "打 开 照 片", "看 一 下 图 片", '
                '"关 闭 图 片", "关 掉 图 片", "[unk]"]')

# 桌面优先, 无图则回退到图片文件夹
IMAGE_DIRS = [os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.join(os.path.expanduser("~"), "Pictures")]


# ---------------------------------------------------------------- 日志
_log_fp = None
_last_opened = None       # 最近打开的图片路径, 供“关闭图片”定位窗口进程
_ui = None                # HUD 界面实例(--ui 时启用), 由 worker 线程经其队列更新
_sv = None                # SenseVoice 最终解码引擎(--asr hybrid 时启用)
_capture = None           # 常开录音(环形预滚缓冲), 避免命令第一个字被切掉
PRE_ROLL_SEC = 1.2        # 预滚时长: 唤醒瞬间回溯这段时间的音频
_stop = threading.Event()  # 界面关闭/退出信号
_wake_q = queue.Queue()    # 串口线程 -> 业务线程: (角度, beam, score)
_busy = threading.Event()  # 正在录音/识别/执行, 期间新唤醒明确提示并忽略
_tts_speak = None         # 懒加载的 TTS 播报函数(tts.speak)
LLM_ACK_TEXT = "我想想"    # 交给大模型前的等待提示(启动时预热, 尽量秒出声)

# ---- 咖啡任务(机械臂推理脚本) ----
# 在一个新的 gnome-terminal 窗口里跑脚本: 日志直接在窗口里实时可见, 也能在窗口里按键提前终止
# (运行中 n=提前结束并正常收尾, q=停止, Ctrl+C=中断, 与手动跑脚本时完全一致)。
# 本程序不干预脚本怎么结束; 窗口关闭后即可再次发起。
COFFEE_SCRIPT = "/home/kf/LX/pai0/run_inference_b601_make_coffee_ACT_50k.sh"
COFFEE_TERM_TITLE = "咖啡任务(ACT 推理)"
_coffee = {"proc": None}                  # gnome-terminal 进程(存活=窗口还开着), 防重复启动
_coffee_lock = threading.Lock()
_stats = {"shake": 0, "events": 0, "wakes": 0, "wake_busy": 0, "wake_dup": 0, "other": 0}


def log(msg: str):
    """带时间戳的实时日志(控制台 + 可选日志文件 + 可选界面)"""
    line = "%s %s" % (time.strftime("[%H:%M:%S]"), msg)
    print(line, flush=True)
    if _log_fp:
        _log_fp.write(line + "\n")
        _log_fp.flush()
    if _ui:
        _ui.log(line)


def log_state(msg: str, kind: str = "info"):
    """交互状态提示: 监听中 / 已唤醒 / 识别中 / 识别结果 / 已执行 …"""
    log("[状态] " + msg)
    if _ui:
        _ui.state(msg, kind)


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
    global _last_opened
    path = find_newest_image()
    if not path:
        log("[命令] 未找到图片: 请在 ~/Desktop 或 ~/Pictures 放一张图片")
        return
    log(f"[命令] 打开图片: {path}")
    _last_opened = path
    subprocess.Popen(["xdg-open", path], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def close_image(*_ignored):
    """关闭本程序打开的那张图片窗口(按文件路径匹配查看器进程, 只关这一张)"""
    global _last_opened
    if not _last_opened:
        log("[命令] 还没有打开过图片, 没有可关闭的窗口")
        return
    path = _last_opened
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fp:
                cmdline = fp.read().decode("utf-8", "replace")
        except OSError:
            continue
        if path in cmdline:                      # 该进程正在查看这张图
            pids.append(int(pid))
    if not pids:
        log(f"[命令] 未找到图片窗口(可能已手动关闭): {path}")
        _last_opened = None
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    log(f"[命令] 关闭图片: {path} (结束进程 {pids})")
    _last_opened = None


def make_coffee(*_ignored):
    """语音“给我倒杯咖啡/做咖啡” -> 在新的终端窗口里跑机械臂推理脚本"""
    with _coffee_lock:
        p = _coffee["proc"]
        if p is not None and p.poll() is None:
            log_state("咖啡终端窗口还开着，忽略本次指令（先关掉那个窗口再发起）", "warn")
            tts_speak_async("正在制作中，请稍候")
            return
    threading.Thread(target=_coffee_launch, daemon=True, name="coffee").start()


def _coffee_term_cmd(script: str) -> str:
    """终端里要执行的 shell 命令: 跑脚本 -> 报告退出码 -> 停在提示上等回车关窗"""
    return ("bash {s}; rc=$?; echo; "
            "echo \"[咖啡] 脚本已结束，退出码 $rc\"; "
            "read -r -p \"[咖啡] 按回车关闭本窗口 \" _").format(s=shlex.quote(script))


def _coffee_launch():
    """把脚本放进一个新终端窗口跑: 日志实时可见, 窗口里可直接按键提前终止"""
    script = COFFEE_SCRIPT
    if not os.path.exists(script):
        log(f"[咖啡] 脚本不存在: {script}")
        tts_speak("找不到咖啡脚本")
        return
    term = shutil.which("gnome-terminal")
    if not term:
        log("[咖啡] 未找到 gnome-terminal, 无法在终端窗口里显示")
        tts_speak("找不到终端程序")
        return
    if not ensure_x_display():            # 弹终端窗口需要能连上 X(DISPLAY/XAUTHORITY)
        log("[咖啡] 当前环境无法打开终端窗口, 咖啡任务未启动")
        tts_speak("无法打开终端窗口")
        return
    log_state(f"咖啡任务：在新终端窗口里启动 {os.path.basename(script)}", "awake")
    log("[咖啡] 提示：机械臂即将动作，请确保周边安全")
    log("[咖啡] 窗口里：先按回车开始推理；运行中 n=提前结束并正常收尾, q=停止, Ctrl+C=中断")
    log("[咖啡] 脚本结束后窗口停在提示上, 按回车关闭; 本程序不会去动脚本的结束")
    tts_speak_async("好的，开始为你制作咖啡")     # 后台播报, 不拖慢窗口启动

    try:
        proc = subprocess.Popen(
            [term, "--title=%s" % COFFEE_TERM_TITLE, "--wait",
             "--", "bash", "-c", _coffee_term_cmd(script)],
            cwd=os.path.dirname(script), start_new_session=True)
    except Exception as e:
        log(f"[咖啡] 启动失败: {type(e).__name__}: {e}")
        tts_speak("咖啡任务启动失败")
        return

    with _coffee_lock:
        _coffee["proc"] = proc
    log(f"[咖啡] 终端已打开（gnome-terminal PID={proc.pid}）")
    rc = proc.wait()
    with _coffee_lock:
        _coffee["proc"] = None
    log(f"[咖啡] 终端窗口已关闭（gnome-terminal 退出码 {rc}）")


# 命令词 -> 动作; 顺序即优先级(“关闭”类必须先于“图片”, 否则含“图”会被打开命令截走)
COMMANDS = [
    (("咖啡",), make_coffee),                     # 给我倒杯咖啡 -> 新终端窗口跑机械臂推理脚本
    (("关闭", "关掉", "闭", "收起"), close_image),
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


def tts_speak(text: str):
    """懒加载 TTS 并播报(首次在线合成后缓存, 之后离线可播)。
    并发保护在 tts.speak 内部(只锁播放), 所以这里不需要再加锁。"""
    global _tts_speak
    if _tts_speak is None:
        from tts import speak as _speak
        _tts_speak = _speak
    return _tts_speak(text)


def tts_speak_async(text: str):
    """后台播报, 不阻塞调用方(合成/播放慢时也不拖住启动流程); 失败只记日志"""
    def _run():
        try:
            tts_speak(text)
        except Exception as e:
            log(f"[警告] 提示播报失败: {type(e).__name__}: {e}")
    threading.Thread(target=_run, daemon=True).start()


def ask_llm_and_speak(text: str) -> bool:
    """命令词没匹配上 -> 交给 DeepSeek 回答并播报。返回是否成功播报"""
    try:
        from llm import ask, is_stub
    except Exception as e:
        log(f"[大模型] 模块不可用: {type(e).__name__}: {e}")
        return False
    if is_stub():
        log("[大模型] 未配置 key, 桩模式(只回固定话术) —— "
            "设置 DEEPSEEK_API_KEY 或写 .deepseek_key 即可接真模型")
    log_state(f"命令词没匹配上，交给 DeepSeek：{text}", "recognize")
    tts_speak_async(LLM_ACK_TEXT)             # 先给个反馈, 与请求并行
    t0 = time.time()
    try:
        reply = ask(text)
    except Exception as e:
        log(f"[大模型] 调用失败: {e}")
        return False
    log_state(f"DeepSeek 回复（{time.time() - t0:.1f}s）：{reply}", "result")
    try:
        tts_speak(reply)
    except Exception as e:
        log(f"[警告] 回复播报失败: {type(e).__name__}: {e}")
        return False
    return True


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
def build_stream_cmd(seconds=None):
    """构造 raw 音频流录音命令(便于边录边识别): 优先 PulseAudio 的 XFM 源.
    seconds=None 表示常开采集(不加 -d), 供 AudioCapture 预滚缓冲使用"""
    src = pulse_source_for_xfm()
    common = ["-f", "S16_LE", "-r", str(WAV_RATE), "-c", "1", "-t", "raw"]
    if seconds:
        common += ["-d", str(seconds)]
    common += ["-"]
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
    """抓取音频(含预滚)并识别.

    SenseVoice 模式(默认): 非流式引擎, 用"滚动重解码"近似实时 —— 每新增约 0.5s 音频
                          就把整段重解一次, 界面/终端文本逐步增长; 结束时以完整音频定稿.
    Vosk 模式(--asr vosk): 保持原有逐段流式识别.
    """
    pre = _capture.pre_roll if _capture else 0.0
    engine = "SenseVoice 实时" if _sv is not None else "Vosk"
    log_state(f"识别中({engine})：采集 {seconds}s + 预滚 {pre:.1f}s, 请说命令词…",
              "recognize")
    t0 = time.time()
    pcm = bytearray()
    lock = threading.Lock()
    print_lock = threading.Lock()            # 终端即时行会被两个线程刷新, 避免交错
    state = {"remain": seconds, "live": ""}

    def push_live(text):
        """把实时文本同时送到终端(原地刷新)与界面"""
        state["live"] = text
        if _ui:
            _ui.partial(text, state["remain"])
        line = f"[识别中] 剩余{state['remain']}s"
        if text:
            line += f" 「{text}」"
        with print_lock:
            print("\r" + line, end="", flush=True)

    live_thread = None
    stop_live = threading.Event()
    if _sv is not None:                      # 启动滚动重解码线程
        def _live_loop():
            decoded = 0
            while not stop_live.is_set():
                with lock:
                    data = bytes(pcm)
                if len(data) - decoded >= int(0.5 * 32000):
                    decoded = len(data)
                    try:
                        t = _sv.decode_pcm(data)
                        if t:
                            push_live(t)
                    except Exception:
                        pass
                stop_live.wait(0.1)
        live_thread = threading.Thread(target=_live_loop, daemon=True)
        live_thread.start()
        rec = None                           # sv 模式下不需要 Vosk

    if rec is not None:
        rec.Reset()
        segments = []

    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(WAV_RATE)
        for chunk in _capture.grab_stream(seconds):
            if _stop.is_set():
                break
            wf.writeframes(chunk)
            with lock:
                pcm += chunk
            heard = max(0.0, time.time() - t0 - pre)
            state["remain"] = max(0, int(seconds - heard))
            if rec is not None:               # Vosk 流式路径
                if rec.AcceptWaveform(chunk):
                    seg = parse_text(rec.Result())
                    if seg:
                        segments.append(seg)
                        log(f"[识别] 结果片段: {seg}")
                else:
                    cur = json.loads(rec.PartialResult()).get("partial", "")
                    cur = cur.replace(" ", "").replace("[unk]", "")
                    push_live(cur)
            else:                             # sv 路径: 刷新倒计时(文本由解码线程更新)
                push_live(state["live"])

    # 收尾: 停掉滚动线程, 用完整音频定稿
    stop_live.set()
    if live_thread:
        live_thread.join(timeout=2)
    _clear_line_and_return(state["live"])
    if _ui:
        _ui.level(0.0)
        _ui.partial("")

    tail = parse_text(rec.FinalResult()) if rec is not None else ""
    vosk_text = ("".join(segments) + tail) if rec is not None else ""
    text = vosk_text
    if _sv is not None:
        try:
            t1 = time.time()
            sv_text = _sv.decode_pcm(bytes(pcm)) if pcm else ""
            log(f"[识别] SenseVoice 定稿({(time.time()-t1)*1000:.0f}ms): "
                f"{sv_text or '(空)'}")
            if sv_text:
                text = sv_text
        except Exception as e:
            log(f"[警告] SenseVoice 定稿失败({type(e).__name__}: {e})")
    log_state(f"识别结果：{text or '(无有效语音)'}", "result" if text else "warn")
    return text


def _clear_line_and_return(shown: str):
    """清除终端里的原地刷新行"""
    if shown:
        print("\r" + " " * (len(shown) + 24) + "\r", end="", flush=True)
    return shown


def _rms_norm(chunk: bytes) -> float:
    """S16LE 裸流的归一化 RMS(0~1), 用于界面电平条(抽样计算, 足够 UI 用)"""
    from array import array
    a = array("h")
    a.frombytes(chunk[:len(chunk) // 2 * 2])
    if not a:
        return 0.0
    step = 8                                   # 抽样, 降低 CPU 占用
    n = 0
    s = 0
    for i in range(0, len(a), step):
        v = a[i]
        s += v * v
        n += 1
    return min(1.0, (s / n) ** 0.5 / 8000.0)


# ---------------------------------------------------------------- 主流程
def serial_reader(args):
    """独立线程: 持续读串口 -> ACK 握手 -> 解析唤醒事件入队; 掉线自动重连.
    这样录音/识别期间也不会漏掉唤醒事件, 也不会停止握手确认造成链路"盲区"."""
    last_ack = 0.0
    last_stat = time.time()
    last_stat_snap = dict(_stats)
    recent_wakes = {}
    mic = None

    def _open():
        """打开串口(自动识别或 --port)"""
        if args.port:
            return MicSerial(args.port).open()
        port, m = MicSerial.autodetect(timeout=1.5)
        if not port:
            raise SerialTimeoutError("未找到降噪板串口(检查 USB 连接)")
        return m if m else MicSerial(port).open()

    while not _stop.is_set():
        if mic is None:
            try:
                mic = _open()
                log(f"[串口线程] 已连接 {mic.port}")
            except Exception as e:      # 用宽捕获: termios.error 等不是 OSError 子类,
                log(f"[警告] 串口打开失败: {type(e).__name__}: {e}, 3 秒后重试")
                time.sleep(3)           # 绝不能让读取线程静默退出
                continue
        try:
            for typ, sid, payload in mic.read_frames(timeout=0.3):
                if typ == MSG_SHAKE:
                    _stats["shake"] += 1
                    now = time.time()
                    if now - last_ack >= 1.0:      # 节流确认, 防写缓冲积压
                        mic.ack_handshake(typ, sid, payload)
                        last_ack = now
                    continue
                if typ == MSG_AIUI:
                    _stats["events"] += 1
                    try:
                        obj = json.loads(payload.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    angles = find_angles(obj)
                    if not (has_key(obj, "ivw") or angles):
                        # 非唤醒的设备事件(如开机 started / 版本), 仅作诊断打印
                        log(f"[设备事件] {pretty_payload(typ, payload).replace(chr(10), ' ')}")
                        continue
                    stamp = tuple(find_values(obj, "start_ms"))
                    now = time.time()
                    if stamp and stamp in recent_wakes and now - recent_wakes[stamp] < 30:
                        _stats["wake_dup"] += 1
                        log_state("忽略重复唤醒事件(固件重发)", "listen")
                        continue
                    if stamp:
                        recent_wakes = {k: t for k, t in recent_wakes.items()
                                        if now - t < 120}
                        recent_wakes[stamp] = now
                    angle = sorted(set(angles))[0] if angles else None
                    beams = find_values(obj, "beam")
                    scores = find_values(obj, "score")
                    if _busy.is_set():             # 正在录音/识别, 明确提示并忽略
                        _stats["wake_busy"] += 1
                        log_state(f"识别中又听到唤醒(角度 {angle}°), 本次忽略; "
                                  f"如需重试请等提示后重新唤醒", "warn")
                        continue
                    _stats["wakes"] += 1
                    _wake_q.put((angle,
                                 beams[0] if beams else None,
                                 scores[0] if scores else None))
                else:
                    _stats["other"] += 1
                    log(f"[串口] 其它帧 type={hex(typ)} "
                        f"载荷={payload.hex(' ')[:60]}")
            # 诊断: 每 30s 汇总一次收帧情况(判断"没听到"是板子没报还是程序漏了)
            if time.time() - last_stat >= 30:
                d = {k: _stats[k] - last_stat_snap[k] for k in _stats}
                log(f"[诊断] 近 {int(time.time() - last_stat)}s: "
                    f"握手帧 {d['shake']} / 设备事件 {d['events']} / "
                    f"唤醒 {d['wakes']} / 重复忽略 {d['wake_dup']} / "
                    f"忙碌忽略 {d['wake_busy']} / 其它 {d['other']}")
                if _ui:                                  # 界面右上角诊断计数
                    _ui.stats({"握手": d["shake"], "事件": d["events"],
                               "唤醒": d["wakes"], "重复": d["wake_dup"],
                               "忙碌": d["wake_busy"]})
                last_stat = time.time()
                last_stat_snap = dict(_stats)
        except Exception as e:          # 串口异常(含非 OSError 的 termios.error): 重连不退出
            log(f"[警告] 串口异常({type(e).__name__}: {e}), 重新连接...")
            try:
                mic.close()
            except (OSError, AttributeError):
                pass
            mic = None
            time.sleep(3)
    if mic is not None:
        try:
            mic.close()
        except OSError:
            pass


def _on_audio_chunk(chunk):
    """常开采集回调: 持续喂界面(瀑布图/电平), 与是否在识别无关"""
    if _ui:
        _ui.samples(chunk)
        _ui.level(_rms_norm(chunk))


def run_live(args, rec):
    """唤醒 -> 从常开采集抓取(含预滚) -> 边录边识别 -> 执行命令"""
    global _capture
    if not os.path.isdir(AUDIO_DIR):
        os.makedirs(AUDIO_DIR)

    log("=" * 60)
    log("语音命令演示: 说「打开图片」打开 / 说「关闭图片」关闭")
    log(f"[识别] 引擎: SenseVoice(实时文本 + 定稿, CPU)"
        if _sv is not None else "[识别] 引擎: Vosk(逐段流式)")
    log(f"[识别] 唤醒后采集 {args.duration}s")
    cmd, env, desc = build_stream_cmd(None)
    _capture = AudioCapture(cmd, env=env, pre_roll=PRE_ROLL_SEC,
                            on_chunk=_on_audio_chunk,
                            on_warn=lambda m: log(f"[警告] {m}")).start()
    log(f"[采集] 常开录音已启动 ({desc}), 预滚 {PRE_ROLL_SEC}s "
        f"—— 唤醒前的声音会被保留, 命令第一个字不再丢")
    log_state(f"监听中：请先说唤醒词(当前「{WAKE_WORD_TEXT}」/ {WAKE_WORD_PINYIN}), "
              f"再说命令词(如「打开图片」「关闭图片」)", "listen")
    log("=" * 60)

    reader = threading.Thread(target=serial_reader, args=(args,), daemon=True)
    reader.start()

    last_beat_t = time.time()
    listen_since = time.time()
    while not _stop.is_set():
        try:
            angle, beam, score = _wake_q.get(timeout=0.5)
        except queue.Empty:
            if time.time() - last_beat_t >= 60:      # 心跳: 提示仍在监听
                log_state(f"监听中…（已连续监听 {int(time.time() - listen_since)}s, "
                          f"请说唤醒词「{WAKE_WORD_TEXT}」）", "listen")
                last_beat_t = time.time()
            continue

        _busy.set()                                  # 进入录音/识别/执行
        try:
            if _ui and angle is not None:            # 界面: 雷达指针 + 波束高亮
                _ui.angle(angle, beam, score)
            log_state(f"已唤醒：声源角度 {angle if angle is not None else '未知'}° "
                      f"→ 请说命令词(如「打开图片」)", "awake")
            wav = os.path.join(AUDIO_DIR, time.strftime("cmd_%Y%m%d_%H%M%S.wav"))
            text = stream_recognize(rec, args.duration, wav)
            if text and dispatch(text):
                log_state("已执行命令，回到监听中", "done")
            elif text and args.llm and ask_llm_and_speak(text):
                log_state("已播报大模型回复，回到监听中", "listen")
            else:
                # 兜底: 没听懂/无法处理的命令 -> 用 TTS 播报提示
                reason = "未识别到命令词" if not text else f"未匹配到命令: {text}"
                if args.fallback_say:
                    log_state(f"{reason} → 播报提示「{args.fallback_say}」", "warn")
                    try:
                        tts_speak(args.fallback_say)
                    except Exception as e:
                        log(f"[警告] 提示播报失败: {type(e).__name__}: {e}")
                    log_state("播报完成，回到监听中", "listen")
                else:
                    log_state(f"{reason}，回到监听中", "listen")
        finally:
            _busy.clear()
        last_beat_t = time.time()
        listen_since = time.time()
        # 录音期间可能积压了唤醒, 让用户明确知道(不自动执行, 避免误触发)
        if not _wake_q.empty():
            while not _wake_q.empty():
                _wake_q.get_nowait()
            log_state("录音期间还有唤醒事件积压, 已丢弃; 请重新唤醒并用命令词", "warn")

    if _capture:
        _capture.stop()


def ensure_x_display():
    """启动 GUI 前的 X 环境检查(GUI 需要连上 X server):
    1) 无 DISPLAY -> 明确报错(常见于 ssh 未 -X 或纯字符终端)
    2) 有 DISPLAY 但无 XAUTHORITY -> 自动补上本机 gdm 的 Xauthority(常见坑)
    3) X socket 不存在 -> 明确报错
    返回 True 表示可以尝试启动 GUI"""
    display = os.environ.get("DISPLAY", "")
    if not display:
        log("[错误] 未检测到 DISPLAY: 请在图形界面的终端里运行, 或 ssh -X 后再试")
        return False
    if not os.environ.get("XAUTHORITY"):
        cand = "/run/user/%d/gdm/Xauthority" % os.getuid()
        if os.path.exists(cand):
            os.environ["XAUTHORITY"] = cand
            log(f"[提示] 未设置 XAUTHORITY, 已自动使用 {cand}")
    num = display.split(":")[-1].split(".")[0]
    sock = "/tmp/.X11-unix/X%s" % num
    if not os.path.exists(sock):
        log(f"[错误] 找不到 X socket {sock}: DISPLAY={display} 可能无效")
        return False
    return True


def _ensure_qt_libs():
    """Qt6 的 xcb 平台插件依赖 libxcb-cursor.so.0.
    若系统缺该库但本地兜底目录存在(~/.local/lib/qt-xcb), 则带上 LD_LIBRARY_PATH 重启自身
    (LD_LIBRARY_PATH 必须在进程启动前生效, 运行中改 os.environ 无效).
    系统装好 libxcb-cursor0 后本函数自动跳过."""
    libdir = os.path.expanduser("~/.local/lib/qt-xcb")
    if os.environ.get("_QT_XCB_FALLBACK") == "1" or not os.path.isdir(libdir):
        return
    import ctypes.util
    if ctypes.util.find_library("xcb-cursor"):
        return
    env = dict(os.environ)
    old = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = libdir + ((":" + old) if old else "")
    env["_QT_XCB_FALLBACK"] = "1"
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def load_ui():
    """加载界面实现(PySide6). 返回 (类, 名字); 不可用则 (None, None)"""
    try:
        from sound_radar_hud import SoundRadarHUD as ui_cls
        return ui_cls, "qt"
    except Exception as e:
        log(f"[警告] 界面后端不可用: {type(e).__name__}: {e}")
        return None, None


def run_with_ui(args, rec):
    """界面模式: worker 线程跑主循环, 主线程跑 GUI(关窗即整体退出)"""
    global _ui
    if not ensure_x_display():
        log("[错误] 当前环境无法显示图形界面, 退回无界面模式(日志照常输出)")
        return run_live(args, rec)
    ui_cls, backend = load_ui()
    if ui_cls is None:
        log("[错误] 没有可用的界面后端, 退回无界面模式")
        return run_live(args, rec)
    log(f"[界面] 后端 = {backend}")
    _ui = ui_cls()
    _ui.on_close(lambda: _stop.set())

    worker = threading.Thread(target=run_live, args=(args, rec), daemon=True)
    worker.start()
    _ui.run()                      # 阻塞到关窗
    _stop.set()
    worker.join(timeout=3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M260C 语音命令演示(打开图片)")
    ap.add_argument("--port", help="降噪板串口(默认自动检测)")
    ap.add_argument("--duration", type=int, default=3, help="唤醒后录音秒数(默认3)")
    ap.add_argument("--log-file", help="同时把日志写入该文件")
    ap.add_argument("--ui", action="store_true",
                    help="打开实时 HUD 界面(PySide6): 环形角度雷达 + 频谱瀑布图 + 状态提示 + 日志")
    ap.add_argument("--asr", choices=["sv", "vosk"], default="sv",
                    help="识别引擎: sv=SenseVoice(实时文本+定稿, 推荐); vosk=仅 Vosk(轻量)")
    ap.add_argument("--fallback-say", default="暂时还处理不了",
                    help="命令无法处理时用 TTS 播报的提示语(留空则关闭兜底播报)")
    ap.add_argument("--no-llm", action="store_true",
                    help="关闭大模型兜底: 命令词没匹配上时不问 DeepSeek, 直接播兜底语")
    ap.add_argument("--coffee-script", default=COFFEE_SCRIPT,
                    help="「给我倒杯咖啡」时在终端窗口里执行的机械臂推理脚本")
    ap.add_argument("--wav", help="直接识别已有 WAV(不连硬件), 用于验证")
    a = ap.parse_args()

    a.llm = not a.no_llm               # 默认开启: 未命中命令词时交给 DeepSeek 问答
    COFFEE_SCRIPT = a.coffee_script

    if a.log_file:
        _log_fp = open(a.log_file, "a", encoding="utf-8")
    # Qt 的 xcb 兜底可能重启自身, 必须在加载模型之前完成, 否则模型会被加载两次
    if a.ui and not a.wav:
        _ensure_qt_libs()
    if a.asr == "sv":                            # SenseVoice: 实时文本 + 定稿都走它
        try:
            from asr_sensevoice import SenseVoice
            log("[识别] 加载 SenseVoice(加载约 5~6s)…")
            _sv = SenseVoice()
            log("[识别] SenseVoice 就绪(CPU, 实时文本用滚动重解码)")
        except Exception as e:
            log(f"[警告] SenseVoice 不可用({type(e).__name__}: {e}), 退回 Vosk 模式")
            a.asr = "vosk"
    rec = load_recognizer() if a.asr == "vosk" else None
    if a.fallback_say and not a.wav:             # 预热提示音(后台合成, 不阻塞启动)
        def _prewarm():
            try:
                from tts import prewarm
                items = [a.fallback_say]
                if a.llm:
                    items.append(LLM_ACK_TEXT)     # 大模型等待提示, 预热后能立刻出声
                for text in items:
                    ok = prewarm(text)
                    log(f"[TTS] 提示音{'已就绪' if ok else '预热失败(将在首次使用时合成)'}: "
                        f"「{text}」")
            except Exception as e:
                log(f"[警告] TTS 预热失败: {type(e).__name__}: {e}")
        threading.Thread(target=_prewarm, daemon=True).start()

    if a.wav:
        if _sv is not None:
            log(f"[识别] {a.wav} -> {_sv.decode(a.wav) or '(空)'}")
        else:
            recognize_wav(rec, a.wav)
    elif a.ui:
        run_with_ui(a, rec)
    else:
        try:
            run_live(a, rec)
        except KeyboardInterrupt:
            _stop.set()
            print("\n用户中断, 退出")
