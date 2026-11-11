# -*- coding: utf-8 -*-
"""
coffee_voice_trigger.py —— 两阶段语音任务调度:
    ACT 默认推理 -> 人工按 r -> 按唤醒角度自动分发区域子任务(不再手动按 1/2/3)

一、业务流程
    阶段一(默认): 启动即在终端窗口里跑 ACT 默认推理 run_act_cup_button.sh
                  (取杯 -> 放白托盘 -> 按按钮 -> 回零)。该脚本自带热键: 推理中按 r
                  会停掉 rollout 并进入回放菜单。本程序用两种方式判定"按过 r":
                    ① 检测到 replay_menu.sh 进程(在 ACT 窗口里按 r 的原有路径)
                    ② 在本程序终端按 r, 由本程序向 lerobot-rollout 发 SIGINT
    阶段二(ARMED): 唤醒词「你好宽宽」的唤醒事件里带 angle(降噪板六麦定位, 0~359°),
                  唤醒后用角度直接路由, 不再需要回放菜单:
                      180° ~ 240°  -> 区域 1
                      240° ~ 300°  -> 区域 2
                      300° ~ 360°  -> 区域 3
                  命中区域后在新终端窗口里串行执行该区域的子任务流水线;
                  默认每个区域一步, 即 run_act_area{1,2,3}.sh(三套 place_area ACT 权重)。

二、分层与扩展点
    ┌ 输入层   serial_reader() / keyboard_loop() / AudioCapture + SenseVoice
    ├ 路由层   angle_to_zone(): 角度 -> 区域(区间/边界/未覆盖扇区策略)
    ├ 编排层   phase1_monitor + wake_worker + 状态机 PHASE1/ARMED/RUNNING
    ├ 任务层   Step / ZoneTask: 一个区域 = 若干 Step 组成的流水线
    └ 执行层   _launch_task_terminal(): 独立终端窗口 + 退出码回传 + TTS

    子任务流水线(ZoneTask.steps)是"依次调用不同 ACT 权重"的落点; 默认每个区域只有一步。
    需要多步时, 按顺序追加真实入口脚本(命令都在 --coffee-dir 下执行):
        steps=[Step("第 1 段", "bash <真实入口脚本1>"),
               Step("第 2 段", "bash <真实入口脚本2>")]
    升级 pi0.5 时调度层完全不用改, 只把某个 Step 换成(脚本目录里已有 run_pi05_lora.sh):
        Step("pi05 全流程", "bash run_pi05_lora.sh 040000", policy="pi05")
    (Step.policy 只用于日志/启动摘要, 真正执行的是 cmd, 因此新策略只需提供
     一个"跑完就退出"的前台脚本。)

三、安全约束
    - 只有 ARMED 状态才响应唤醒, 阶段一不会误触发区域任务
    - 同一时刻只允许一个机械臂任务; 检测到 lerobot-rollout/lerobot-replay 在跑就拒绝启动
    - 子任务仍在独立终端窗口里跑, 需人工按 ENTER 确认零位, 日志实时可见
    - 运行中按 x = 中断(向 lerobot 进程发 SIGINT, 等同窗口里 Ctrl+C)
    - 0~180° 未配置扇区, 默认忽略; --outside nearest 可映射到最近区域

四、用法(需带 funasr 的 lerobot_b601 环境)
    /home/jt/dev/miniforge3/envs/lerobot_b601/bin/python demo/coffee_voice_trigger.py
    ... --skip-phase1                     # 跳过第一阶段, 直接 ARMED(联调角度分发)
    ... --zone1-cmd "bash run_act_area1.sh"   # 区域 1 的子任务入口(2/3 同理)
    ... --outside nearest                 # 0~180° 映射到最近区域(默认 ignore)
    ... --duration 3                      # 唤醒后录音秒数
    ... --task-timeout 600                # 单个区域任务的外部兜底超时(0=不限)
    ... --no-tts                          # 关闭播报(联调)
    ... --coffee-dir /path/to/lerobot     # ACT 脚本所在目录
    ... --port /dev/ttyACM0               # 手动指定降噪板串口

五、键盘(本程序终端)
    r = 停止第一阶段并进入第二阶段(也可以直接在 ACT 窗口里按 r)
    x = 中断当前第一阶段/区域任务
    q = 退出(区域任务在跑时先按 x)
    s = 打印当前状态

六、状态机
    PHASE1 --(r / 回放菜单出现)--> ARMED --(唤醒 + 角度命中)--> RUNNING
      ^                              |                              |
      +---------- x 中断 -------------+---------- 完成/中断 <--------+
    q = 退出(运行中拒绝退出)
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
import tempfile
import threading
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根目录
sys.path.insert(0, PROJECT_DIR)
from audio_capture import AudioCapture
from mic_serial import MicSerial, MSG_SHAKE, MSG_AIUI
from voice_interact_test import (ala_card_ids, find_angles, find_values,
                                 has_key, pulse_source_for_xfm)

WAV_RATE = 16000
PRE_ROLL_SEC = 1.2                      # 唤醒瞬间回溯的音频, 避免命令第一个字被切掉
WAKE_WORD_TEXT = "你好宽宽"

# ---------------------------------------------------------------- 任务脚本
# 推理脚本目录(与 run_act_cup_button.sh 同目录; 区域脚本也在这里)
COFFEE_DIR = "/home/jt/dev/lerobot/self_scripts/b601_single/inference/lerobot"
PHASE1_CMD = "bash run_act_cup_button.sh"
PHASE1_TITLE = "阶段一：ACT 默认推理 (cup_button)"
ZONE_CMD = "bash run_act_area%d.sh"     # 区域 1/2/3 的默认入口
ZONE_TITLE = "阶段二：区域%d任务"
ARM_PROMPT = "已进入第二阶段，请唤醒我，我会根据你的方位启动任务"

# 终端包装脚本把每个步骤的退出码写到这个文件, 供本程序播报/判定
RESULT_FILE = os.path.join(tempfile.gettempdir(), "m260c_zone_task_result.log")

# 角度扇区: (下界, 上界, 区域号), 左闭右开
ZONE_RANGES = ((180.0, 240.0, 1), (240.0, 300.0, 2), (300.0, 360.0, 3))
ZONE_NAMES = {1: "区域一", 2: "区域二", 3: "区域三"}
ABORT_WORDS = ("取消", "停止", "停下", "别动", "不要")

_sv = None
_capture = None
_args = None
_tasks = {}
_stop = threading.Event()
_wake_q = queue.Queue()                 # 串口线程 -> wake_worker: (angle, beam, score)
_busy = threading.Lock()                # 录音/识别/启动任务期间忽略新唤醒
_state = {"phase": "init", "task": None}
_state_lock = threading.Lock()
_term = {"fd": None, "old": None}       # 键盘单键读取用的终端原状态(退出时恢复)


def log(msg: str):
    print("%s %s" % (time.strftime("[%H:%M:%S]"), msg), flush=True)


def log_state(msg: str):
    log("[状态] " + msg)


# ---------------------------------------------------------------- 播报
def speak(text: str):
    """同步播报(会阻塞到播放结束); --no-tts 时只记日志"""
    if _args and _args.no_tts:
        log("[TTS] (已关闭) %s" % text)
        return
    try:
        from tts import speak as _speak
        _speak(text)
    except Exception as e:
        log("[TTS] 播报失败: %s: %s" % (type(e).__name__, e))


def speak_async(text: str):
    """后台播报, 不阻塞唤醒处理/任务启动"""
    threading.Thread(target=speak, args=(text,), daemon=True).start()


def prewarm_phrases():
    """只合成不播报: 让区域应答与结果播报不必现场等网络"""
    if _args and _args.no_tts:
        return
    phrases = [ARM_PROMPT, "您所在的方位没有配置任务", "已取消本次任务",
               "机械臂还在执行任务，请稍后再试", "已中断当前任务"]
    for task in _tasks.values():
        phrases.append(task.ack)
        phrases.append("%s任务已完成" % task.name)
        phrases.append("%s任务已中断" % task.name)
        phrases.append("%s任务异常结束" % task.name)
    try:
        from tts import prewarm
        for text in phrases:
            prewarm(text)
        log("[TTS] 播报语预热完成(%d 条)" % len(phrases))
    except Exception as e:
        log("[TTS] 预热失败(触发时仍会重试合成): %s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------- 任务模型
class Step(object):
    """一个推理步骤: 一个策略 + 一条前台命令。

    policy 目前只用于日志/启动摘要; 新增策略(pi0.5/SmolVLA)时, 只要 cmd 指向
    一个"跑完就退出"的启动脚本即可, 编排层无需改动。
    """

    def __init__(self, label: str, cmd: str, policy: str = "act"):
        self.label = label
        self.cmd = cmd
        self.policy = policy


class ZoneTask(object):
    """一个区域的子任务 = 按顺序执行的若干 Step(流水线)。"""

    def __init__(self, zone: int, ack: str, steps):
        self.zone = zone
        self.name = ZONE_NAMES[zone]
        self.ack = ack
        self.steps = list(steps)


def build_zone_tasks(args):
    """区域 -> 子任务流水线。

    默认每个区域一步(place_area 对应的 ACT 权重)。要"依次调用不同的 ACT 权重",
    在 steps 里按顺序追加真实入口 Step 即可; 要升级 pi0.5, 换掉/追加一个 Step:
        steps=[Step("第 1 段", "bash <真实入口脚本1>"),
               Step("第 2 段", "bash <真实入口脚本2>")]
        steps=[Step("pi05 全流程", "bash run_pi05_lora.sh 040000", policy="pi05")]
    """
    return {
        1: ZoneTask(1, "好的，开始区域一的任务",
                    [Step("ACT place_area1", args.zone1_cmd)]),
        2: ZoneTask(2, "好的，开始区域二的任务",
                    [Step("ACT place_area2", args.zone2_cmd)]),
        3: ZoneTask(3, "好的，开始区域三的任务",
                    [Step("ACT place_area3", args.zone3_cmd)]),
    }


# ---------------------------------------------------------------- 角度路由
def _ang_dist(a: float, b: float) -> float:
    """两个角度的最小圆周距离(0~180°)"""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def angle_to_zone(angle, outside: str = "ignore"):
    """声源角度 -> 区域号。

    区间按 [下界, 上界) 处理: 180~240 -> 1, 240~300 -> 2, 300~360 -> 3。
    0~180 未配置: outside="ignore" 返回 None; outside="nearest" 取最近区域中心。
    """
    if angle is None:
        return None
    a = float(angle) % 360.0
    for lo, hi, zone in ZONE_RANGES:
        if lo <= a < hi:
            return zone
    if outside == "nearest":
        return min(ZONE_RANGES,
                   key=lambda r: _ang_dist(a, (r[0] + r[1]) / 2.0))[2]
    return None


# ---------------------------------------------------------------- 进程探测
def _pgrep(pattern: str):
    """当前用户下命令行匹配 pattern 的进程 PID 列表"""
    try:
        out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", pattern],
                             capture_output=True, text=True).stdout
    except Exception:
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def _alive(pattern: str) -> bool:
    return bool(_pgrep(pattern))


def arm_in_use() -> bool:
    """机械臂是否被推理/回放进程占用(本程序自己的区域任务也算)"""
    return _alive("lerobot-rollout") or _alive("lerobot-replay")


def _kill_replay_menu():
    """接管第二阶段时关掉第一阶段的回放菜单, 避免又被手动按键启动回放"""
    pids = _pgrep("replay_menu.sh")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if pids:
        log("[接管] 已关闭第一阶段窗口里的回放菜单(pid %s)" % pids)


def _stop_phase1_rollout():
    """停止第一阶段推理: 先 SIGINT(留出断开/回零时间), 不退再 SIGTERM"""
    pids = _pgrep("lerobot-rollout")
    if not pids:
        log("[阶段一] 没有发现 lerobot-rollout 进程")
        return
    log("[阶段一] 向 rollout 发送 SIGINT(等同在 ACT 窗口按 Ctrl+C): pid %s" % pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    deadline = time.time() + 12
    while time.time() < deadline and _alive("lerobot-rollout"):
        time.sleep(0.3)
    for pid in _pgrep("lerobot-rollout"):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _interrupt_rollout() -> bool:
    """中断当前推理/回放(区域任务的中断键), 等同窗口里 Ctrl+C"""
    pids = _pgrep("lerobot-rollout") + _pgrep("lerobot-replay")
    if not pids:
        log("[中断] 未发现正在运行的 lerobot 进程")
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    log("[中断] 已向 %s 发送 SIGINT" % pids)
    return True


# ---------------------------------------------------------------- 状态机
def _phase() -> str:
    with _state_lock:
        return _state["phase"]


def _set_phase(phase: str, why: str = ""):
    with _state_lock:
        old = _state["phase"]
        _state["phase"] = phase
    log("[状态] %s -> %s%s" % (old, phase, ("：" + why) if why else ""))


def drain_wakes():
    """清掉积压的唤醒事件, 避免刚进入第二阶段就被旧事件触发"""
    n = 0
    while True:
        try:
            _wake_q.get_nowait()
            n += 1
        except queue.Empty:
            break
    if n:
        log("[接管] 丢弃积压的 %d 个唤醒事件" % n)


def arm(reason: str) -> bool:
    """进入第二阶段(ARMED): 只有这个状态才响应唤醒角度"""
    with _state_lock:
        if _state["phase"] in ("armed", "running"):
            log("[接管] 当前已是 %s，无需重复进入(触发原因: %s)"
                % (_state["phase"], reason))
            return False
        _state["phase"] = "armed"
    _kill_replay_menu()
    drain_wakes()
    log_state("第二阶段已就绪(ARMED)：唤醒我，按声源方位自动启动区域任务")
    log("[角度映射] 180~240 -> 区域1 | 240~300 -> 区域2 | 300~360 -> 区域3")
    speak_async(ARM_PROMPT)
    return True


# ---------------------------------------------------------------- 区域任务执行
def _read_result_lines():
    try:
        with open(RESULT_FILE, "r", encoding="utf-8", errors="replace") as fp:
            return [ln.strip() for ln in fp if ln.strip()]
    except OSError:
        return []


def _task_shell(task: ZoneTask) -> str:
    """生成终端里执行的 bash 片段: 串行跑 steps, 每步退出码写入 RESULT_FILE。

    写文件而不是靠进程退出码, 是因为 gnome-terminal --wait 会在脚本结束后停在
    "按回车关闭窗口"上, 外层进程要等用户关窗才退出。
    """
    lines = [
        "RESULT=%s" % shlex.quote(RESULT_FILE),
        ': > "$RESULT"',
        "TASK=%s" % shlex.quote(task.name),
        "TOTAL=%d" % len(task.steps),
        "_LAST_RC=0",
        "run_step() {",
        '  _n="$1"; _label="$2"; _cmd="$3"',
        '  echo ""; echo "[$TASK] 步骤 ${_n}/${TOTAL}：${_label}"',
        '  bash -c "$_cmd"',
        "  _LAST_RC=$?",
        '  printf "STEP %s %s\\n" "$_n" "$_LAST_RC" >> "$RESULT"',
        '  case "$_LAST_RC" in 0) return 0;; 130) return 2;; *) return 1;; esac',
        "}",
    ]
    for i, step in enumerate(task.steps, 1):
        lines.append(
            'run_step %d %s %s || { printf "DONE %%s\\n" "$_LAST_RC" >> "$RESULT"; '
            'echo "[$TASK] 步骤 %d 未正常完成(退出码 $_LAST_RC)，任务中止"; '
            'read -r -p "[$TASK] 按回车关闭本窗口 " _; exit 0; }'
            % (i, shlex.quote(step.label), shlex.quote(step.cmd), i))
    lines.append('printf "DONE 0\\n" >> "$RESULT"')
    lines.append('echo ""; echo "[$TASK] 全部步骤结束"')
    lines.append('read -r -p "[$TASK] 按回车关闭本窗口 " _')
    return "\n".join(lines)


def _manual_hint(task: ZoneTask):
    cmds = " && ".join(step.cmd for step in task.steps)
    log("[任务] 可手动执行: cd %s && %s" % (_args.coffee_dir, cmds))


def _launch_task_terminal(task: ZoneTask) -> str:
    """在独立终端窗口里跑区域任务。

    返回: "ok"=全部步骤正常结束; "interrupted"=被 Ctrl+C/中断键停止;
          "failed"=失败、超时或窗口提前关闭。
    """
    term = shutil.which("gnome-terminal")
    if not term:
        log("[任务] 未找到 gnome-terminal，无法在终端窗口里执行")
        _manual_hint(task)
        return "failed"
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        log("[任务] 没有 DISPLAY/WAYLAND_DISPLAY，无法打开终端窗口")
        _manual_hint(task)
        return "failed"

    try:
        os.remove(RESULT_FILE)          # 清掉上一次的结果, 避免误判成本次
    except OSError:
        pass

    proc = subprocess.Popen(
        [term, "--title=%s" % (ZONE_TITLE % task.zone), "--wait",
         "--", "bash", "-c", _task_shell(task)],
        cwd=_args.coffee_dir, start_new_session=True)
    log("[任务] 终端已打开(gnome-terminal PID=%d)，窗口里按 ENTER 开始" % proc.pid)

    announced = 0
    deadline = (time.time() + _args.task_timeout) if _args.task_timeout > 0 else None
    hard_deadline = None
    while not _stop.is_set():
        lines = _read_result_lines()
        for ln in lines[announced:]:
            announced += 1
            parts = ln.split()
            if not parts:
                continue
            if parts[0] == "STEP" and len(parts) >= 3:
                log("[任务] 步骤 %s 结束，退出码 %s" % (parts[1], parts[2]))
            elif parts[0] == "DONE" and len(parts) >= 2:
                if parts[1] == "0":
                    return "ok"
                if parts[1] == "130":
                    return "interrupted"
                return "failed"
        if proc.poll() is not None:
            log("[任务] 终端窗口已关闭，未拿到完成标记(可能提前关窗)")
            return "failed"
        if deadline and time.time() > deadline:
            log("[任务] 超过兜底超时 %ss，发送 SIGINT 中断" % _args.task_timeout)
            _interrupt_rollout()
            hard_deadline = time.time() + 20      # 给脚本留收尾/回零时间
            deadline = None
        if hard_deadline and time.time() > hard_deadline:
            log("[任务] 中断后仍未结束，放弃等待")
            return "failed"
        time.sleep(0.5)
    return "failed"


def _run_zone_task(task: ZoneTask):
    status = "failed"
    try:
        status = _launch_task_terminal(task)
    except Exception as e:
        log("[任务] 启动失败: %s: %s" % (type(e).__name__, e))
    finally:
        with _state_lock:
            _state["task"] = None
            _state["phase"] = "armed"
    if _stop.is_set():
        log_state("程序退出，%s任务随之中止" % task.name)
        return
    if status == "ok":
        log_state("%s任务已完成，回到第二阶段就绪状态" % task.name)
        speak_async("%s任务已完成" % task.name)
    elif status == "interrupted":
        log_state("%s任务已中断，回到第二阶段就绪状态" % task.name)
        speak_async("%s任务已中断" % task.name)
    else:
        log_state("%s任务异常结束，请查看窗口日志" % task.name)
        speak_async("%s任务异常结束" % task.name)


def start_zone_task(zone: int):
    """ARMED -> RUNNING, 在后台线程里跑区域任务"""
    task = _tasks[zone]
    with _state_lock:
        if _state["phase"] != "armed":
            log("[任务] 当前状态 %s 不是 ARMED，忽略" % _state["phase"])
            return
        _state["phase"] = "running"
        _state["task"] = {"zone": zone, "since": time.time()}
    log_state("区域 %d 命中，启动子任务(%d 步)"
              % (zone, len(task.steps)))
    for i, step in enumerate(task.steps, 1):
        log("[任务] 步骤 %d/%d：%s（策略 %s）" % (i, len(task.steps),
                                                 step.label, step.policy))
    log("[任务] 机械臂即将动作，请确认周边安全")
    speak_async(task.ack)
    threading.Thread(target=_run_zone_task, args=(task,), daemon=True).start()


# ---------------------------------------------------------------- 唤醒处理
def record_and_decode(seconds: float) -> str:
    """抓取"预滚 + seconds 秒"音频并交给 SenseVoice 识别"""
    pcm = bytearray()
    t0 = time.time()
    for chunk in _capture.grab_stream(seconds):
        pcm += chunk
        if _stop.is_set():
            break
    if not pcm:
        return ""
    t1 = time.time()
    try:
        text = _sv.decode_pcm(bytes(pcm)) or ""
    except Exception as e:
        log("[识别] 解码失败: %s: %s" % (type(e).__name__, e))
        return ""
    log("[识别] %s（录音 %.1fs，解码 %.0fms）"
        % (text or "(无有效语音)", time.time() - t0, (time.time() - t1) * 1000))
    return text


def handle_wake(angle, beam, score):
    """一次唤醒: 只在 ARMED 时按角度路由到区域任务"""
    phase = _phase()
    angle_txt = "未知" if angle is None else "%d°" % angle
    if phase != "armed":
        log_state("唤醒(角度 %s, beam=%s, score=%s)但当前是 %s："
                  "先按 r 进入第二阶段" % (angle_txt, beam, score, phase))
        return
    if not _busy.acquire(blocking=False):
        log_state("正在处理上一条指令，忽略本次唤醒(角度 %s)" % angle_txt)
        return
    try:
        zone = angle_to_zone(angle, _args.outside)
        if zone is None:
            log_state("唤醒角度 %s 不在 180~360 的任务扇区，忽略" % angle_txt)
            speak_async("您所在的方位没有配置任务")
            return
        log_state("唤醒：角度 %s，beam=%s，score=%s -> %s"
                  % (angle_txt, beam, score, ZONE_NAMES[zone]))
        text = record_and_decode(_args.duration)
        if text and any(w in text for w in ABORT_WORDS):
            log_state("听到取消词，放弃本次任务：%r" % text)
            speak_async("已取消本次任务")
            return
        if arm_in_use():
            log_state("机械臂仍被其它任务占用(lerobot 进程在跑)，拒绝启动")
            speak_async("机械臂还在执行任务，请稍后再试")
            return
        start_zone_task(zone)
    finally:
        _busy.release()


def wake_worker():
    """单线程消费唤醒事件, 保证区域路由/录音不会并发执行"""
    while not _stop.is_set():
        try:
            angle, beam, score = _wake_q.get(timeout=0.3)
        except queue.Empty:
            continue
        handle_wake(angle, beam, score)


# ---------------------------------------------------------------- 串口/音频
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


def serial_reader(port=None):
    """串口线程: 持续 ACK 握手, 收唤醒事件(角度)入队; 掉线自动重连"""
    mic = None
    last_ack = 0.0
    recent = {}                         # start_ms -> 时间, 固件重发去重
    while not _stop.is_set():
        if mic is None:
            try:
                if port:
                    mic = MicSerial(port).open()
                else:
                    detected, m = MicSerial.autodetect(timeout=1.5)
                    if not detected:
                        raise RuntimeError("未找到降噪板串口(检查 USB 连接)")
                    mic = m if m else MicSerial(detected).open()
                log("[串口] 已连接 %s，等待唤醒词「%s」…" % (mic.port, WAKE_WORD_TEXT))
            except Exception as e:
                log("[串口] 打开失败(%s: %s)，3 秒后重试" % (type(e).__name__, e))
                time.sleep(3)
                continue
        try:
            for typ, sid, payload in mic.read_frames(timeout=0.3):
                if typ == MSG_SHAKE:
                    now = time.time()
                    if now - last_ack >= 1.0:
                        mic.ack_handshake(typ, sid, payload)
                        last_ack = now
                    continue
                if typ != MSG_AIUI:
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                angles = find_angles(obj)
                if not (has_key(obj, "ivw") or angles):
                    continue                 # 开机 started 之类的非唤醒事件
                stamp = tuple(find_values(obj, "start_ms"))
                now = time.time()
                if stamp and stamp in recent and now - recent[stamp] < 30:
                    continue                 # 同一唤醒事件固件重发
                if stamp:
                    recent = {k: t for k, t in recent.items() if now - t < 120}
                    recent[stamp] = now
                angle = angles[0] if angles else None
                beams = find_values(obj, "beam")
                scores = find_values(obj, "score")
                beam = beams[0] if beams else None
                score = scores[0] if scores else None
                log("[唤醒] 角度 %s beam=%s score=%s"
                    % ("未知" if angle is None else "%d°" % angle, beam, score))
                _wake_q.put((angle, beam, score))
        except Exception as e:
            log("[串口] 异常(%s: %s)，重新连接…" % (type(e).__name__, e))
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


# ---------------------------------------------------------------- 阶段一
def _start_phase1():
    if _phase() in ("armed", "running"):     # 启动瞬间已按 r 进入第二阶段
        return
    if _args.skip_phase1:
        log("[阶段一] --skip-phase1：跳过 ACT 默认推理，直接进入第二阶段")
        arm("skip-phase1")
        return
    term = shutil.which("gnome-terminal")
    if not term or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        log("[阶段一] 无法打开终端窗口；请手动执行: cd %s && %s"
            % (_args.coffee_dir, _args.phase1_cmd))
        _set_phase("phase1", "等待手动启动 ACT 推理")
        return
    inner = ('%s; rc=$?; echo; echo "[阶段一] 脚本已结束，退出码 $rc"; '
             'read -r -p "[阶段一] 按回车关闭本窗口 " _' % _args.phase1_cmd)
    _set_phase("phase1", "启动 ACT 默认推理")
    proc = subprocess.Popen(
        [term, "--title=%s" % PHASE1_TITLE, "--wait", "--", "bash", "-c", inner],
        cwd=_args.coffee_dir, start_new_session=True)
    log("[阶段一] 终端已打开(PID=%d): 窗口里按 ENTER 开始推理，"
        "推理中按 r 进入第二阶段" % proc.pid)


def phase1_monitor():
    """盯第一阶段: 回放菜单出现 = 用户按过 r; rollout 消失 = 推理已结束。
    进入第二阶段后仍会关闭新冒出来的回放菜单, 避免手动 1/2/3 与语音任务抢机械臂。"""
    started = time.time()
    idle_warned = False
    while not _stop.is_set():
        if _phase() == "phase1":
            if _alive("replay_menu.sh"):
                log("[阶段一] 检测到 ACT 窗口已按 r(回放菜单出现)，接管第二阶段")
                arm("回放菜单出现")
            elif not _alive("lerobot-rollout") and not idle_warned \
                    and time.time() - started > 15:
                idle_warned = True
                log("[阶段一] 当前没有 rollout 在跑；推理结束后按 r 进入第二阶段")
        elif _alive("replay_menu.sh"):
            log("[接管] 第二阶段期间又出现回放菜单，关闭它(避免手动回放抢机械臂)")
            _kill_replay_menu()
        else:
            idle_warned = False
        _stop.wait(0.5)


# ---------------------------------------------------------------- 键盘
def _enter_cbreak():
    """键盘单键: 终端切 cbreak(不等回车); 非终端(重定向)保持原样"""
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


def _on_key_r():
    phase = _phase()
    if phase in ("armed", "running"):
        log("[按键] r：当前已是 %s，无需重复进入" % phase)
        return
    if phase == "phase1":
        log("[按键] r：停止第一阶段 ACT 推理，进入第二阶段")
        log("[按键] ACT 窗口里显示'推理异常退出'属正常(SIGINT 主动停止)")
        _stop_phase1_rollout()
    arm("按键 r")


def _on_key_x():
    phase = _phase()
    if phase == "phase1":
        log("[按键] x：停止第一阶段 ACT 推理")
        _stop_phase1_rollout()
        _set_phase("phase1", "推理已停止，按 r 进入第二阶段")
        return
    if phase != "running":
        log("[按键] x：当前没有运行中的任务")
        return
    log("[按键] x：中断当前区域任务")
    speak_async("已中断当前任务")
    _interrupt_rollout()


def _on_key_q():
    if _phase() == "running":
        log("[按键] q：区域任务还在运行，先按 x 中断，再按 q 退出")
        return
    if _phase() == "phase1" and _alive("lerobot-rollout"):
        log("[按键] q：第一阶段推理仍在运行，退出后它不会被自动停止")
    log("[按键] q：退出程序")
    _stop.set()


def keyboard_loop():
    _enter_cbreak()
    try:
        while not _stop.is_set():
            ch = sys.stdin.read(1)
            if not ch:                   # stdin 关闭
                break
            if ch in ("r", "R"):
                threading.Thread(target=_on_key_r, daemon=True).start()
            elif ch in ("x", "X"):
                _on_key_x()
            elif ch in ("q", "Q"):
                _on_key_q()
            elif ch in ("s", "S"):
                with _state_lock:
                    task = _state["task"]
                    phase = _state["phase"]
                log_state("当前状态：%s%s" % (phase,
                          ("，区域 %d 任务运行中" % task["zone"]) if task else ""))
    except Exception as e:
        log("[按键] 读取结束(%s: %s)" % (type(e).__name__, e))
    finally:
        restore_terminal()


# ---------------------------------------------------------------- 主流程
def main():
    global _sv, _capture, _args, _tasks
    ap = argparse.ArgumentParser(
        description="两阶段语音调度: ACT 默认推理 -> 按 r -> 按声源角度启动区域任务")
    ap.add_argument("--coffee-dir", default=COFFEE_DIR,
                    help="ACT 推理脚本所在目录(默认 %(default)s)")
    ap.add_argument("--phase1-cmd", default=PHASE1_CMD,
                    help="第一阶段 ACT 默认推理命令(默认 %(default)s)")
    ap.add_argument("--zone1-cmd", default=ZONE_CMD % 1,
                    help="区域 1 的子任务入口(默认 %(default)s)")
    ap.add_argument("--zone2-cmd", default=ZONE_CMD % 2,
                    help="区域 2 的子任务入口(默认 %(default)s)")
    ap.add_argument("--zone3-cmd", default=ZONE_CMD % 3,
                    help="区域 3 的子任务入口(默认 %(default)s)")
    ap.add_argument("--duration", type=float, default=3.0,
                    help="唤醒后录音秒数(默认 %(default)s)")
    ap.add_argument("--outside", choices=["ignore", "nearest"], default="ignore",
                    help="0~180° 未配置扇区的处理: ignore=忽略(默认), nearest=最近区域")
    ap.add_argument("--task-timeout", type=int, default=600,
                    help="单个区域任务的外部兜底超时秒数(0=不限, 默认 %(default)s)")
    ap.add_argument("--skip-phase1", action="store_true",
                    help="跳过第一阶段, 启动即进入 ARMED(联调角度分发)")
    ap.add_argument("--no-tts", action="store_true", help="关闭语音播报(联调)")
    ap.add_argument("--port", help="降噪板串口(默认按 USB ID 自动识别)")
    _args = ap.parse_args()
    _tasks = build_zone_tasks(_args)

    log("加载 SenseVoice(首次约 5~6 秒)…")
    from asr_sensevoice import SenseVoice
    _sv = SenseVoice()

    cmd, env = build_stream_cmd()
    _capture = AudioCapture(cmd, env=env, pre_roll=PRE_ROLL_SEC,
                            on_warn=lambda m: log("[录音] %s" % m)).start()
    log("[录音] 常开采集已启动: %s" % " ".join(cmd))
    threading.Thread(target=prewarm_phrases, daemon=True).start()

    log("=" * 64)
    log("阶段一: cd %s && %s" % (_args.coffee_dir, _args.phase1_cmd))
    log("阶段二: 唤醒词「%s」-> 按声源角度启动区域子任务" % WAKE_WORD_TEXT)
    log("        180~240° -> 区域1(%s)" % _args.zone1_cmd)
    log("        240~300° -> 区域2(%s)" % _args.zone2_cmd)
    log("        300~360° -> 区域3(%s)" % _args.zone3_cmd)
    log("键盘: r=进入第二阶段  x=中断  q=退出  s=状态")
    if _args.skip_phase1:
        log("注意: --skip-phase1 已开启, 启动即进入第二阶段")
    log("=" * 64)

    threading.Thread(target=keyboard_loop, daemon=True).start()
    threading.Thread(target=wake_worker, daemon=True).start()
    _start_phase1()
    threading.Thread(target=phase1_monitor, daemon=True).start()
    try:
        serial_reader(_args.port)
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
