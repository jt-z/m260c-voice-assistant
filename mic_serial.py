# -*- coding: utf-8 -*-
"""
mic_serial.py —— M260C 智能音箱(M2 系列降噪板, 讯飞 XFM-DP)串口协议 Python 实现

协议来源: 客户资料 V6.0 中离线/在线 SDK 源码(wheeltec_mic.cpp / wheeltec_m2n.cpp /
          service.cpp)与语音模块使用指南。纯 Python 标准库实现, 无第三方依赖。

帧格式 (大端序号, 长度/序号为小端):
    [0]     0xA5           帧头 FRAME_HEADER
    [1]     0x01           用户 ID USER_ID
    [2]     type           消息类型
    [3..4]  len (uint16 LE)  JSON 载荷长度
    [5..6]  sid (uint16 LE)  消息序号
    [7..7+len) JSON 载荷
    [last]  checksum       前面所有字节求和后取补码(即 ~sum+1 & 0xff)

消息类型 (MsgType):
    0x01 Shake     设备握手请求 (载荷固定 b"\xA5\x00\x00\x00", 收到后须回 0xFF 确认)
    0x04 AIUI_MSG  设备消息 (唤醒/识别等, 载荷为 JSON)
    0x05 CONTROL   主控消息 (上位机下发命令用此类型)
    0x06 VOICE     语音数据
    0xFF CONFIRM   确认消息 (回复握手/命令用)

上位机可下发命令(JSON 载荷, 类型 0x05):
    {"type":"version"}                                      查询版本
    {"type":"manual_wakeup","content":{"beam":N}}           手动唤醒/设定波束 0-5
    {"type":"wakeup_keywords","content":{"keyword":"..",
                                          "threshold":"900"}} 更换唤醒词(需重新插拔)
    {"type":"switch_mic","content":{"mic":"m2/m2n/..."}}    切换麦克风类型(仅 M2 系列)
    {"type":"get_original_audio","content":{"audio":0/1}}    原始音频流开关
"""
import os
import struct
import time
import select
import termios
import glob

FRAME_HEADER = 0xA5
USER_ID = 0x01


class SerialTimeoutError(OSError):
    """串口读写超时/设备异常"""

MSG_SHAKE = 0x01    # 设备->主机 握手请求
MSG_AIUI = 0x04     # 设备->主机 设备消息(JSON)
MSG_CONTROL = 0x05  # 主机->设备 命令; 设备侧主控消息
MSG_VOICE = 0x06
MSG_CONFIRM = 0xFF  # 确认

HANDSHAKE_PAYLOAD = b"\xA5\x00\x00\x00"
BAUD_DEFAULT = 115200


def checksum(data: bytes) -> int:
    """和校验: 所有字节求和取补码(与 C 端 ~sum+1 一致)"""
    return (~sum(data) + 1) & 0xFF


def make_frame(msg_type: int, payload: bytes, sid: int = 0) -> bytes:
    """组帧: 0xA5 0x01 type len(LE) sid(LE) payload checksum"""
    body = bytes([FRAME_HEADER, USER_ID, msg_type & 0xFF]) + \
        struct.pack("<H", len(payload)) + struct.pack("<H", sid & 0xFFFF) + payload
    return body + bytes([checksum(body)])


def iter_frames(stream: bytes, start: int = 0):
    """从字节流中逐个解析出完整帧, 返回 (type, sid, payload) 生成器"""
    n = len(stream)
    i = start
    while i < n:
        if stream[i] != FRAME_HEADER or i + 7 > n:
            i += 1
            continue
        if stream[i + 1] != USER_ID:
            i += 1
            continue
        plen = struct.unpack("<H", stream[i + 3:i + 5])[0]
        total = 7 + plen + 1
        if total > n:
            break  # 半包, 等待更多数据
        frame = stream[i:i + total]
        if frame[-1] != checksum(frame[:-1]):
            i += 1
            continue
        msg_type = frame[2]
        sid = struct.unpack("<H", frame[5:7])[0]
        yield msg_type, sid, frame[7:7 + plen]
        i += total


def list_candidate_ports():
    """候选串口: 优先 wheeltec udev 别名, 其次所有 ttyACM/ttyUSB"""
    ports = []
    for pat in ("/dev/wheeltec_mic*", "/dev/ttyACM*", "/dev/ttyUSB*"):
        ports += sorted(glob.glob(pat))
    return ports


def _usb_identity(tty_path: str):
    """沿 sysfs 向上找到 USB 设备目录, 返回 (vid, pid, serial, product)"""
    dev = os.path.realpath("/sys/class/tty/%s/device" % os.path.basename(tty_path))
    d = dev
    for _ in range(6):  # ttyUSB: .../ttyX/.. 各层; ttyACM: 接口层
        if os.path.exists(os.path.join(d, "idVendor")):
            def rd(n):
                try:
                    return open(os.path.join(d, n)).read().strip()
                except OSError:
                    return ""
            return rd("idVendor"), rd("idProduct"), rd("serial"), rd("product")
        d = os.path.dirname(d)
    return None


class MicSerial:
    """基于标准库 termios 的串口封装(8N1, 无流控)"""

    def __init__(self, port: str, baud: int = BAUD_DEFAULT, timeout: float = 0.05):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._fd = None
        self._buf = b""

    # ---------------- 打开 / 关闭 ----------------
    def open(self):
        """打开并配置串口(8N1). 注意 termios.error 不是 OSError 子类,
        这里统一转成 SerialTimeoutError, 并在失败时关闭 fd 防止句柄泄漏"""
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
            # 原始模式 8N1
            iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                       termios.ISTRIP | termios.INLCR | termios.IGNCR |
                       termios.ICRNL | termios.IXON)
            oflag &= ~(termios.OPOST)
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
            cflag |= (termios.CS8 | termios.CREAD | termios.CLOCAL)
            lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                       termios.ISIG | termios.IEXTEN)
            baud_const = getattr(termios, "B%d" % self.baud, termios.B115200)
            ispeed = ospeed = baud_const
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW,
                              [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
        except termios.error as e:                 # 非 tty 设备 / 配置失败
            os.close(fd)
            raise SerialTimeoutError(
                "打开串口失败(%s 可能不是串口设备): %s" % (self.port, e)) from e
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        self._buf = b""
        return self

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    @property
    def is_open(self):
        return self._fd is not None

    # ---------------- 收发 ----------------
    def write(self, data: bytes, timeout: float = 3.0):
        """非阻塞 fd 上的安全写: 等待可写并处理 EAGAIN, 超时抛 SerialTimeoutError"""
        mv = memoryview(data)
        deadline = time.time() + timeout
        while len(mv):
            remain = deadline - time.time()
            if remain <= 0:
                raise SerialTimeoutError("串口写超时: %s (设备未及时读取)" % self.port)
            _, w, _ = select.select([], [self._fd], [], min(remain, 0.2))
            if not w:
                continue
            try:
                n = os.write(self._fd, mv)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError as e:
                raise SerialTimeoutError(
                    "串口写入失败: %s (%s)" % (self.port, e)) from e
            mv = mv[n:]
        return len(data)

    def send(self, msg_type: int, payload: bytes, sid: int = 0) -> int:
        return self.write(make_frame(msg_type, payload, sid))

    def _read_available(self) -> bytes:
        out = b""
        while True:
            r, _, _ = select.select([self._fd], [], [], 0)
            if not r:
                break
            chunk = os.read(self._fd, 4096)
            if not chunk:
                break
            out += chunk
        return out

    def read_frames(self, timeout: float = None):
        """等待并产出 (type, sid, payload); timeout=None 时按构造参数阻塞轮询"""
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while True:
            data = self._read_available()
            if data:
                self._buf += data
                for f in iter_frames(self._buf):
                    yield f
                # 只保留未完成的半包
                for n in range(len(self._buf) - 1, -1, -1):
                    if self._buf[n] == FRAME_HEADER:
                        self._buf = self._buf[n:]
                        break
                else:
                    self._buf = b""
                if timeout is not None and time.time() >= deadline:
                    return
            if time.time() >= deadline:
                return
            time.sleep(0.02)

    # ---------------- 自动识别 ----------------
    @staticmethod
    def detect_by_usb(prefer=("1a86", "55d4")):
        """按 USB 芯片标识找降噪板串口(CH9102 = 1a86:55d4, 文档别名 wheeltec_mic).
        返回端口路径或 None; 这是确定性的, 不依赖设备是否正在发握手帧"""
        for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
            for p in sorted(glob.glob(pat)):
                try:
                    ident = _usb_identity(p)
                except OSError:
                    continue
                if ident and (ident[0], ident[1]) == prefer:
                    return p
        return None

    @staticmethod
    def autodetect(timeout: float = 1.5):
        """自动识别降噪板串口: 先按 USB 芯片标识, 再退化为握手帧扫描."""
        p = MicSerial.detect_by_usb()
        if p:
            try:
                MicSerial(p).open().close()
                print(f"[检测] 端口 {p} (CH9102 降噪板串口)")
                return p, None
            except OSError:
                pass
        for port in list_candidate_ports():
            try:
                m = MicSerial(port, timeout=0.05).open()
            except OSError:
                continue
            try:
                got = False
                for _type, _sid, payload in m.read_frames(timeout=timeout):
                    if _type == MSG_SHAKE and payload == HANDSHAKE_PAYLOAD:
                        got = True
                        break
                if got:
                    print(f"[检测] 端口 {port} 检测到降噪板握手帧")
                    return port, m
                print(f"[检测] 端口 {port} 无响应, 跳过")
            finally:
                if not got:
                    m.close()
        return None, None

    # ---------------- 应用层 ----------------
    def ack_handshake(self, msg_type: int, sid: int, payload: bytes) -> bool:
        """收到握手(0x01)后回 0xFF 确认(照抄载荷与序号), 成功后设备停止握手等待"""
        if msg_type == MSG_SHAKE and payload == HANDSHAKE_PAYLOAD:
            self.send(MSG_CONFIRM, HANDSHAKE_PAYLOAD, sid)
            return True
        return False

    def cmd_json(self, cmd: dict, sid: int = 0, wait_reply: float = 3.0, retries: int = 3):
        """下发 0x05 命令(JSON). 先应答设备握手突发至静默(固件就绪后才会处理命令),
        随后发送命令并等待回包; 无回应则自动重发.
        返回 (回应帧列表, 是否成功)"""
        import json
        replies = []
        payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")

        # 阶段1: 应答握手直至 1s 无新帧(突发结束)或超时
        quiet = time.time() + 5.0
        last_seen = time.time()
        while time.time() < quiet:
            got = False
            for typ, rid, rpayload in self.read_frames(timeout=0.2):
                got = True
                last_seen = time.time()
                if typ == MSG_SHAKE:
                    self.ack_handshake(typ, rid, rpayload)
                else:
                    replies.append((typ, rid, rpayload))
            if got:
                continue
            if time.time() - last_seen > 1.0:
                break

        # 阶段2: 发送命令, 无回应重发几次
        for _ in range(retries):
            self.send(MSG_CONTROL, payload, sid)
            deadline = time.time() + wait_reply
            while time.time() < deadline:
                for typ, rid, rpayload in self.read_frames(timeout=0.2):
                    if typ == MSG_SHAKE:
                        self.ack_handshake(typ, rid, rpayload)
                    else:
                        replies.append((typ, rid, rpayload))
                        if typ in (MSG_CONFIRM, MSG_AIUI):
                            return replies, True
            time.sleep(0.5)
        return replies, False


# 消息类型中文名, 便于打印
TYPE_NAMES = {MSG_SHAKE: "握手", MSG_AIUI: "设备消息", MSG_CONTROL: "主控",
              MSG_VOICE: "语音", MSG_CONFIRM: "确认"}


def pretty_payload(msg_type: int, payload: bytes) -> str:
    """载荷友好显示: JSON 打印缩进, 其余显示 hex"""
    if msg_type in (MSG_AIUI, MSG_CONTROL):
        try:
            import json
            obj = json.loads(payload.decode("utf-8"))
            return json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return payload.hex(" ")
