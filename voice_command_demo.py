# -*- coding: utf-8 -*-
"""
voice_command_demo.py —— 语音命令控制：说“打开图片”打开 / “关闭图片”关闭 图片

链路: M260C 音箱唤醒(板内唤醒引擎) -> XFM 麦克风录音(边录边识别) -> Vosk 本地中文识别
      -> 匹配命令词 -> 执行对应 Python 函数(此例用 xdg-open 打开、按路径关闭图片)

日志: 所有状态带时间戳实时输出, 常用状态提示:
      [状态] 监听中 / 已唤醒 / 识别中(带剩余秒数, 原地刷新) / 识别结果 / 已执行命令
      [诊断] 每 30s 汇总收帧情况(握手帧/设备事件/唤醒/忽略), 用于判断"喊了没反应"
      可用 --log-file 把日志同时写入文件

并发: 串口读取在独立线程(持续 ACK 握手 + 收事件), 业务线程只负责录音/识别/执行,
      因此录音那几秒不再是"盲区"; 若录音期间又听到唤醒, 会明确提示并忽略.

依赖(Vosk 已装在 lerobot conda 环境, 用该环境的 python 运行):
    /home/kf/miniconda3/envs/lerobot/bin/python voice_command_demo.py
    模型: models/vosk-model-small-cn-0.22 (见 .gitignore 的 models 忽略项)

当前板内唤醒词: 「小宽小宽」(xiao3 kuan1 xiao3 kuan1);
改唤醒词: python3 voice_interact_test.py --set-wakeword "..." (改完需拔插音箱)

用法:
    <lerobot-python> voice_command_demo.py                    # 唤醒后说“打开图片”
    <lerobot-python> voice_command_demo.py --ui               # 带实时界面(雷达/状态/日志/电平)
    <lerobot-python> voice_command_demo.py --duration 5       # 录音窗口 5 秒
    <lerobot-python> voice_command_demo.py --log-file run.log # 日志同时落盘
    <lerobot-python> voice_command_demo.py --wav a.wav        # 直接识别已有录音(不连硬件)
    python3 sound_radar_ui.py 20                              # 只预览界面(模拟数据, 不连硬件)
"""
import argparse
import json
import os
import queue
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
from voice_interact_test import (ala_card_ids, find_angles, find_values,
                                 has_key, pulse_source_for_xfm)

MODEL_DIR = os.path.join(PROJECT_DIR, "models", "vosk-model-small-cn-0.22")
AUDIO_DIR = os.path.join(PROJECT_DIR, "audio")
WAV_RATE = 16000
# 当前板内唤醒词(2026-09 通过 voice_interact_test.py --set-wakeword 改为「小宽小宽」;
# 出厂默认为「小微小微」= xiao3 wei1 xiao3 wei1)
WAKE_WORD_TEXT = "小宽小宽"
WAKE_WORD_PINYIN = "xiao3 kuan1 xiao3 kuan1"
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
_ui = None                # Tkinter 界面实例(--ui 时启用), 由 worker 线程经其队列更新
_stop = threading.Event()  # 界面关闭/退出信号
_wake_q = queue.Queue()    # 串口线程 -> 业务线程: (角度, beam, score)
_busy = threading.Event()  # 正在录音/识别/执行, 期间新唤醒明确提示并忽略
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


# 命令词 -> 动作; 顺序即优先级(“关闭”类必须先判, 否则含“图”会被打开命令截走)
COMMANDS = [
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
    log_state(f"识别中：开始录音 {seconds}s ({desc}), 请说命令词…", "recognize")
    rec.Reset()
    segments = []
    shown = ""            # 上一次刷新的整行内容, 用于原地覆盖
    t0 = time.time()
    peak = 0.0
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(WAV_RATE)
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        try:
            while True:
                if _stop.is_set():
                    break
                chunk = proc.stdout.read(4000)
                if not chunk:
                    break
                wf.writeframes(chunk)                      # 原始音频留档
                remain = max(0, seconds - int(time.time() - t0))
                # 实时电平 + 原始音频(供界面波形/频谱)
                rms = _rms_norm(chunk)
                peak = max(peak, rms)
                if _ui:
                    _ui.level(peak)
                    _ui.samples(chunk)
                if rec.AcceptWaveform(chunk):
                    seg = parse_text(rec.Result())         # 分段定稿, 实时打印
                    if seg:
                        _clear_line(shown)
                        shown = ""
                        segments.append(seg)
                        log(f"[识别] 结果片段: {seg}")
                else:
                    cur = json.loads(rec.PartialResult()).get("partial", "").replace(" ", "")
                    cur = cur.replace("[unk]", "")
                    if _ui:
                        _ui.partial(cur, remain)
                    line = f"[识别中] 剩余{remain}s"
                    if cur:
                        line += f" 「{cur}」"
                    if line != shown:                      # 原地刷新, 不换行
                        shown = line
                        print("\r" + shown, end="", flush=True)
        finally:
            proc.wait()
    _clear_line(shown)
    if _ui:
        _ui.level(0.0)
        _ui.partial("")
    # FinalResult 只含尾段, 需与已定稿分段拼接
    text = "".join(segments) + parse_text(rec.FinalResult())
    log_state(f"识别结果：{text or '(无有效语音)'}", "result" if text else "warn")
    return text


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


def _clear_line(shown: str):
    """清除原地刷新的状态行"""
    if shown:
        print("\r" + " " * len(shown) + "\r", end="", flush=True)


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
            except (OSError, SerialTimeoutError) as e:
                log(f"[警告] 串口打开失败: {e}, 3 秒后重试")
                time.sleep(3)
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
        except (OSError, SerialTimeoutError) as e:
            log(f"[警告] 串口异常({e}), 重新连接...")
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


def run_live(args, rec):
    """唤醒 -> 边录边识别 -> 执行命令(串口读取在独立线程, 录音期间不再有盲区)"""
    if not os.path.isdir(AUDIO_DIR):
        os.makedirs(AUDIO_DIR)

    log("=" * 60)
    log("语音命令演示: 说「打开图片」打开 / 说「关闭图片」关闭")
    log(f"[识别] Vosk 本地离线识别(语法限制), 录音时长 {args.duration}s")
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
            if not text:
                log_state("未识别到命令词，回到监听中", "listen")
            elif dispatch(text):
                log_state("已执行命令，回到监听中", "done")
            else:
                log_state("未匹配到命令，回到监听中", "listen")
        finally:
            _busy.clear()
        last_beat_t = time.time()
        listen_since = time.time()
        # 录音期间可能积压了唤醒, 让用户明确知道(不自动执行, 避免误触发)
        if not _wake_q.empty():
            while not _wake_q.empty():
                _wake_q.get_nowait()
            log_state("录音期间还有唤醒事件积压, 已丢弃; 请重新唤醒并用命令词", "warn")


def run_with_ui(args, rec):
    """界面模式: worker 线程跑主循环, 主线程跑 Tk(界面关闭即整体退出)"""
    global _ui
    from sound_radar_ui import SoundRadarUI
    _ui = SoundRadarUI()
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
                    help="打开实时界面(Tkinter): 环形角度雷达 + 状态提示 + 日志 + 电平条")
    ap.add_argument("--wav", help="直接识别已有 WAV(不连硬件), 用于验证")
    a = ap.parse_args()

    if a.log_file:
        _log_fp = open(a.log_file, "a", encoding="utf-8")
    rec = load_recognizer()
    if a.wav:
        recognize_wav(rec, a.wav)
    elif a.ui:
        run_with_ui(a, rec)
    else:
        try:
            run_live(a, rec)
        except KeyboardInterrupt:
            _stop.set()
            print("\n用户中断, 退出")
