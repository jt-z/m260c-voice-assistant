# -*- coding: utf-8 -*-
"""
coffee_voice_trigger.py —— 极简版「做咖啡」语音触发器

链路: M260C 音箱板内唤醒「你好宽宽」-> XFM 录音(带 1.2s 预滚) -> SenseVoice 识别
      -> 听到「为我做杯咖啡」-> 播报「好的，正在为您制作咖啡」
      -> 在新终端窗口里跑 ACT 推理脚本(run_act_cup_button.sh)

与 voice_command_demo.py 的区别: 只认这一条咖啡指令, 不做界面/大模型兜底/其它命令,
适合演示。推理脚本需要人工按 ENTER 确认零位并保证周边安全, 所以放在终端窗口里跑,
不在本进程里自动按键。

运行(需要带 funasr 的 lerobot_b601 环境):
    /home/jt/dev/miniforge3/envs/lerobot_b601/bin/python demo/coffee_voice_trigger.py
    # 键盘热键: 1 = 播报「正在启动机器臂为您制作咖啡」
    #           2 = 播报「您的咖啡制作完成，请慢用！」
    # 可选: --coffee-dir <脚本目录>   换推理脚本所在目录
    #       --coffee-cmd "bash xxx.sh" 换要执行的命令
    #       --duration 5               唤醒后录音时长(秒)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根目录
sys.path.insert(0, PROJECT_DIR)
from audio_capture import AudioCapture
from mic_serial import MicSerial, MSG_SHAKE, MSG_AIUI
from voice_interact_test import ala_card_ids, find_angles, has_key, pulse_source_for_xfm

WAV_RATE = 16000
PRE_ROLL_SEC = 1.2                          # 唤醒瞬间回溯的音频, 避免命令第一个字被切掉
COFFEE_DIR = "/home/jt/dev/lerobot/self_scripts/b601_single/inference/lerobot"
COFFEE_CMD = "bash run_act_cup_button.sh"
COFFEE_ACK = "好的，正在为您制作咖啡"
COFFEE_KEYWORDS = ("咖啡",)
COFFEE_TERM_TITLE = "咖啡任务(cup_button)"
# 键盘热键 -> 播报语(不用唤醒, 直接按键触发)
KEY_PHRASES = {
    "1": "正在启动机器臂为您制作咖啡",
    "2": "您的咖啡制作完成，请慢用！",
}

_sv = None
_capture = None
_busy = threading.Lock()                    # 录音/识别/启动任务期间忽略新唤醒
_coffee_proc = None                         # 当前终端任务(存活 = 窗口还开着)
_stop = threading.Event()
_term = {"fd": None, "old": None}           # 键盘单键读取用的终端原状态(退出时恢复)


def log(msg: str):
    print("%s %s" % (time.strftime("[%H:%M:%S]"), msg), flush=True)


def speak(text: str):
    try:
        from tts import speak as _speak
        _speak(text)
    except Exception as e:
        log("[TTS] 播报失败: %s: %s" % (type(e).__name__, e))


def prewarm_phrases():
    """只合成不播报: 让唤醒答复与按键播报不必现场等网络"""
    try:
        from tts import prewarm
        for text in [COFFEE_ACK] + list(KEY_PHRASES.values()):
            prewarm(text)
    except Exception as e:
        log("[TTS] 预热失败(触发时仍会重试合成): %s: %s" % (type(e).__name__, e))


def _enter_cbreak():
    """键盘单键触发: 终端切到 cbreak(不等回车); 非终端(重定向)则保持原样"""
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        return
    import termios
    import tty
    _term["old"] = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    _term["fd"] = fd


def restore_terminal():
    """恢复终端设置(cbreak 下不恢复会把 shell 的行编辑弄乱)"""
    if _term["fd"] is None:
        return
    try:
        import termios
        termios.tcsetattr(_term["fd"], termios.TCSADRAIN, _term["old"])
    except Exception:
        pass
    _term["fd"], _term["old"] = None, None


def keyboard_loop():
    """读单键: 1 = 开跑播报, 2 = 完成播报"""
    _enter_cbreak()
    try:
        while not _stop.is_set():
            ch = sys.stdin.read(1)
            if not ch:                       # stdin 关闭
                break
            phrase = KEY_PHRASES.get(ch)
            if phrase:
                log("[按键] %s -> 播报: %s" % (ch, phrase))
                speak(phrase)
    except Exception as e:
        log("[按键] 读取结束(%s: %s)" % (type(e).__name__, e))
    finally:
        restore_terminal()


def build_stream_cmd():
    """arecord 常开采集(不加 -d): 优先 PulseAudio 的 XFM 源, 回退 ALSA plughw"""
    common = ["-f", "S16_LE", "-r", str(WAV_RATE), "-c", "1", "-t", "raw", "-"]
    src = pulse_source_for_xfm()
    if src:
        return ["arecord", "-D", "default"] + common, dict(os.environ, PULSE_SOURCE=src)
    cap = ala_card_ids("arecord")
    xfm = next((c for c, d in cap.items() if any("XFM" in x for x in d)), None)
    if xfm:
        return ["arecord", "-D", "plughw:CARD=%s,DEV=0" % xfm] + common, None
    return ["arecord"] + common, None


def record_and_decode(seconds: float) -> str:
    """抓取"预滚 + seconds 秒"音频并交给 SenseVoice 识别"""
    pcm = bytearray()
    for chunk in _capture.grab_stream(seconds):
        pcm += chunk
        if _stop.is_set():
            break
    if not pcm:
        return ""
    t0 = time.time()
    text = _sv.decode_pcm(bytes(pcm)) or ""
    log("[识别] %s (%.0fms, 音频 %.1fs)"
        % (text or "(无有效语音)", (time.time() - t0) * 1000,
           len(pcm) / (WAV_RATE * 2)))
    return text


def coffee_running() -> bool:
    return _coffee_proc is not None and _coffee_proc.poll() is None


def inference_active() -> bool:
    """推理真的在跑(已按过 ENTER, lerobot-rollout 已启动); 只开了窗口不算"""
    try:
        r = subprocess.run(["pgrep", "-f", "lerobot-rollout"],
                           capture_output=True, text=True)
    except Exception:
        return False
    return bool(r.stdout.strip())


def make_coffee(coffee_dir: str, coffee_cmd: str, announce: bool = True):
    """在新终端窗口里跑推理脚本; announce=True(语音触发)时先播报应答"""
    global _coffee_proc
    if coffee_running():
        if inference_active():
            log("[咖啡] 推理正在进行, 忽略本次")
            if announce:
                speak("咖啡任务已经在运行了")
            return
        # 启动时已预开的窗口还停在 ENTER 确认, 不重复开窗, 只提示去按回车
        log("[咖啡] 推理窗口已预启动, 正在等 ENTER: 请切到「%s」窗口按回车开始"
            % COFFEE_TERM_TITLE)
        if announce:
            speak(COFFEE_ACK)
        return
    script = os.path.join(coffee_dir, coffee_cmd.split()[-1])
    if not os.path.exists(script):
        log("[咖啡] 脚本不存在: %s" % script)
        if announce:
            speak("找不到咖啡任务脚本")
        return
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        log("[咖啡] 没有 DISPLAY, 无法打开终端窗口; 请手动运行: cd %s && %s"
            % (coffee_dir, coffee_cmd))
        if announce:
            speak("无法打开终端窗口，请手动启动咖啡任务")
        return
    term = shutil.which("gnome-terminal")
    if not term:
        log("[咖啡] 未找到 gnome-terminal; 请手动运行: cd %s && %s"
            % (coffee_dir, coffee_cmd))
        if announce:
            speak("无法打开终端窗口，请手动启动咖啡任务")
        return

    if announce:                            # 语音触发: 先答复, 再拉窗口
        speak(COFFEE_ACK)
    inner = ('%s; rc=$?; echo; echo "[咖啡] 脚本已结束，退出码 $rc"; '
             'read -r -p "[咖啡] 按回车关闭本窗口 " _' % coffee_cmd)
    log("[咖啡] 启动终端: cd %s && %s" % (coffee_dir, coffee_cmd))
    log("[咖啡] 机械臂即将动作, 请确认周边安全")
    try:
        _coffee_proc = subprocess.Popen(
            [term, "--title=%s" % COFFEE_TERM_TITLE, "--wait",
             "--", "bash", "-c", inner],
            cwd=coffee_dir, start_new_session=True)
    except Exception as e:
        log("[咖啡] 启动失败: %s: %s" % (type(e).__name__, e))
        speak("咖啡任务启动失败")
        return
    log("[咖啡] 终端已打开(gnome-terminal PID=%d), 窗口里按 ENTER 开始推理"
        % _coffee_proc.pid)


def handle_wake(coffee_dir: str, coffee_cmd: str, duration: float):
    """一次唤醒的完整处理: 录音 -> 识别 -> 匹配咖啡指令 -> 答复并启动"""
    if not _busy.acquire(blocking=False):        # 上一轮还没结束
        log("[状态] 正在处理上一条指令, 忽略本次唤醒")
        return
    try:
        log("[状态] 已唤醒, 请说「为我做杯咖啡」")
        text = record_and_decode(duration)
        if not text:
            log("[状态] 没听到内容, 回到监听")
            return
        if any(k in text for k in COFFEE_KEYWORDS):
            log("[命令] 匹配到咖啡指令: %s" % text)
            make_coffee(coffee_dir, coffee_cmd)
        else:
            log("[命令] 不是咖啡指令, 忽略: %s" % text)
    finally:
        _busy.release()


def listen_loop(coffee_dir: str, coffee_cmd: str, duration: float):
    """串口线程: 持续 ACK 握手, 收唤醒事件(掉线自动重连)"""
    mic = None
    last_wake = 0.0
    while not _stop.is_set():
        if mic is None:
            try:
                port, _m = MicSerial.autodetect(timeout=1.5)
                if not port:
                    raise RuntimeError("未找到降噪板串口(检查 USB 连接)")
                mic = _m if _m else MicSerial(port).open()
                log("[串口] 已连接 %s, 等待唤醒词「你好宽宽」…" % mic.port)
            except Exception as e:
                log("[串口] 打开失败(%s: %s), 3 秒后重试"
                    % (type(e).__name__, e))
                time.sleep(3)
                continue
        try:
            for typ, sid, payload in mic.read_frames(timeout=0.3):
                if typ == MSG_SHAKE:
                    mic.ack_handshake(typ, sid, payload)
                    continue
                if typ != MSG_AIUI:
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not (has_key(obj, "ivw") or find_angles(obj)):
                    continue                     # 开机 started 之类的非唤醒事件
                now = time.time()
                if now - last_wake < 2.0:        # 固件重发, 去重
                    continue
                last_wake = now
                threading.Thread(target=handle_wake, daemon=True,
                                 args=(coffee_dir, coffee_cmd, duration)).start()
        except Exception as e:
            log("[串口] 异常(%s: %s), 重新连接…" % (type(e).__name__, e))
            try:
                mic.close()
            except Exception:
                pass
            mic = None
            time.sleep(2)
    if mic is not None:
        try:
            mic.close()
        except Exception:
            pass


def main():
    global _sv, _capture
    ap = argparse.ArgumentParser(description="你好宽宽唤醒 -> 做咖啡")
    ap.add_argument("--coffee-dir", default=COFFEE_DIR, help="推理脚本所在目录")
    ap.add_argument("--coffee-cmd", default=COFFEE_CMD, help="要执行的命令")
    ap.add_argument("--duration", type=float, default=4.0, help="唤醒后录音时长(秒)")
    args = ap.parse_args()

    log("加载 SenseVoice(首次约 5~6 秒)…")
    from asr_sensevoice import SenseVoice
    _sv = SenseVoice()

    cmd, env = build_stream_cmd()
    _capture = AudioCapture(cmd, env=env, pre_roll=PRE_ROLL_SEC).start()
    log("[录音] 常开采集已启动: %s" % " ".join(cmd))
    threading.Thread(target=prewarm_phrases, daemon=True).start()  # 只合成不播放

    log("=" * 56)
    log("语音命令: 先说「你好宽宽」唤醒, 再说「为我做杯咖啡」")
    log("将执行: cd %s && %s" % (args.coffee_dir, args.coffee_cmd))
    log("键盘热键: 1 = 「正在启动机器臂为您制作咖啡」  "
        "2 = 「您的咖啡制作完成，请慢用！」")
    log("按 Ctrl+C 退出")
    log("=" * 56)
    threading.Thread(target=keyboard_loop, daemon=True).start()
    make_coffee(args.coffee_dir, args.coffee_cmd, announce=False)  # 启动即拉起推理脚本
    try:
        listen_loop(args.coffee_dir, args.coffee_cmd, args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        restore_terminal()
        if _capture:
            _capture.stop()
        log("已退出")


if __name__ == "__main__":
    main()
