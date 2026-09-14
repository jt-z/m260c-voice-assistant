# -*- coding: utf-8 -*-
"""
audio_capture.py —— 常开录音 + 环形预滚缓冲(解决"命令第一个字被切掉")

问题: 唤醒事件经串口上报后才启动 arecord, 从唤醒到真正采集有 100~300ms 延迟,
      用户紧接着说的命令第一个字会落在这段空隙里被切掉
      (实测: 打开图片→开图片, 关闭图片→闭图片, 看一下图片→下图片).
方案: 进程启动即持续录音, 线程维护最近 PRE_ROLL 秒的环形缓冲;
      唤醒时 grab_stream() 先吐"预滚"音频再接实时音频, 起始字不再丢.

线程模型: reader 线程持续读 arecord 的 raw 输出 -> 环形缓冲(环形裁剪仅在未抓取时进行);
          grab_stream() 由业务线程调用, 抓取期间暂停裁剪, 保证不丢块.
"""
import subprocess
import threading
import time

WAV_RATE = 16000
BYTES_PER_SEC = WAV_RATE * 2          # S16LE 单声道


class AudioCapture:
    """常开录音 + 环形预滚缓冲

    cmd:  arecord 命令行(不含 -d, 持续输出 raw S16LE 16k 单声道)
    on_chunk: 每个音频块回调(可用于界面实时波形/瀑布图)
    on_warn:   异常提示回调
    """

    def __init__(self, cmd, env=None, pre_roll=1.2, on_chunk=None, on_warn=None):
        self.cmd = cmd
        self.env = env
        self.pre_roll = pre_roll
        self._pre_bytes = int(pre_roll * BYTES_PER_SEC)
        self.on_chunk = on_chunk
        self.on_warn = on_warn
        self._cond = threading.Condition()
        self._chunks = []              # [(seq, bytes)] 近 pre_roll 秒
        self._seq = 0
        self._grabbing = False
        self._stop = threading.Event()
        self._proc = None
        self._thread = None

    # ---------------- 生命周期 ----------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="audio-capture")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        with self._cond:
            self._cond.notify_all()

    # ---------------- 采集线程 ----------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(self.cmd, env=self.env,
                                              stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL)
                while not self._stop.is_set():
                    chunk = self._proc.stdout.read(4000)
                    if not chunk:
                        break
                    with self._cond:
                        self._chunks.append((self._seq, chunk))
                        self._seq += 1
                        if not self._grabbing:
                            self._trim_locked()
                        self._cond.notify_all()
                    if self.on_chunk:
                        try:
                            self.on_chunk(chunk)
                        except Exception:
                            pass
                if not self._stop.is_set() and self.on_warn:
                    self.on_warn("录音进程退出, 2 秒后重启")
            except Exception as e:                     # 设备被占用/拔插等
                if self.on_warn:
                    self.on_warn(f"录音异常: {type(e).__name__}: {e}, 2 秒后重试")
            time.sleep(2)

    def _trim_locked(self):
        """只保留最近 pre_roll 秒(调用者需持锁)"""
        total = sum(len(c) for _, c in self._chunks)
        while len(self._chunks) > 1 and total - len(self._chunks[0][1]) >= self._pre_bytes:
            total -= len(self._chunks[0][1])
            self._chunks.pop(0)

    # ---------------- 抓取 ----------------
    def grab_stream(self, seconds):
        """产出音频块: 先给预滚(最近 pre_roll 秒), 再跟实时, 累计约 seconds 秒.

        seconds 指"唤醒后的时长"; 实际总时长 ≈ seconds + pre_roll.
        """
        target = int((seconds + self.pre_roll) * BYTES_PER_SEC)
        deadline = time.time() + seconds + self.pre_roll + 3.0
        got = 0
        with self._cond:
            self._grabbing = True
            next_seq = self._chunks[0][0] if self._chunks else self._seq
        try:
            while got < target and time.time() < deadline and not self._stop.is_set():
                with self._cond:
                    batch = [(s, c) for (s, c) in self._chunks if s >= next_seq]
                    if batch:
                        next_seq = batch[-1][0] + 1
                    else:
                        self._cond.wait(0.1)
                if not batch:
                    continue
                for _s, c in batch:
                    got += len(c)
                    yield c
                    if got >= target:
                        break
        finally:
            with self._cond:
                self._grabbing = False
                # 抓取期间未裁剪, 收工后恢复: 只留最后一块, 交给 _trim_locked
                self._chunks = self._chunks[-1:] if self._chunks else []
                self._trim_locked()

    # ---------------- 工具 ----------------
    def buffered_seconds(self):
        with self._cond:
            return sum(len(c) for _, c in self._chunks) / BYTES_PER_SEC
