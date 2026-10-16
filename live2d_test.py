# -*- coding: utf-8 -*-
'''
Live2D loader for ChatBot (enhanced v1.6)
=========================================
相对 v1.4.5 增强版新增:
  * 鼠标左键拖动窗口移动人物 (松开若未拖动则视为点击, 重播动作)
  * 透明方案升级: 逐像素 alpha (UpdateLayeredWindow), 消除颜色键的紫色轮廓
    由 live2d_window.json 的 transparent_mode 选择:
      "alpha" / "colorkey" / "none"
  * 自定义表情映射 (来自 expressions_custom.json, 由 live2d_preview.py 生成):
      - 支持自定义快捷键
      - 支持任意"参数组合"式表情 (不依赖模型注册 Expressions)
      - 无该文件时自动生成内置 8 表情的默认映射
  * 动作: 自写 MotionPlayer 解析 motion3.json (绕开 live2d-py 加载 motion 崩溃)
  * 保留: vol / wav / trace 接口, 背景图, 置顶, 视线跟随, 桌宠/背景切换
配置: live2d_window.json (窗口/透明/动作), config.json (模型/背景),
      expressions_custom.json (自定义表情映射)
调试: 环境变量 LIVE2D_TEST_SECONDS=N 运行 N 秒后自动退出
'''
import math, json, os, sys, time, queue, threading, ctypes
from ctypes import wintypes
import numpy as np
import pygame
import waver
from pygame.locals import *
import live2d.v3 as live2d
from live2d.v3 import StandardParams
from live2d.utils.image import Image
from OpenGL import GL

try:
    import expression_map as emap
except Exception as _e:  # 允许缺失
    emap = None
    print('[live2d] expression_map 不可用:', _e)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# ---------------------------------------------------------------------------
# 路径 & 配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _abspath(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(BASE_DIR, p))


def _load_json(name, default=None):
    try:
        with open(os.path.join(BASE_DIR, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print('[live2d] load %s failed: %s' % (name, e))
        return default if default is not None else {}


config = _load_json('config.json', {})

model_path = _abspath(config.get('model_path', '.\\MAOMAO\\MAOMAO.model3.json'))
background_path = _abspath(
    os.path.join(config.get('background_path', '.\\pic\\'),
                 config.get('background', 'bak0.jpg')))
ver = config.get('version', '1.4.5')

DEFAULT_MOTION_FILE = '123.motion3.json'

WIN_CFG_DEFAULT = {
    "width": 500,
    "height": 600,
    "frameless": True,
    "transparent": True,
    "transparent_mode": "alpha",   # alpha | colorkey | none
    "always_on_top": True,
    "click_through": False,
    "pet_mode": True,
    "chroma_key": [255, 0, 255],
    "caption": None,
    "motion_file": DEFAULT_MOTION_FILE,
    "auto_motion": True,
}
win_cfg = dict(WIN_CFG_DEFAULT)
win_cfg.update(_load_json('live2d_window.json', {}))

TEST_SECONDS = float(os.environ.get('LIVE2D_TEST_SECONDS', '0') or 0)

# ---------------------------------------------------------------------------
# 模块级状态 (保留给上层 chatbot 使用)
# ---------------------------------------------------------------------------
live2d.setLogEnable(False)

vol = 0
wav = []
trace = False

_commands = queue.Queue()
_status_lock = threading.Lock()
_status = {
    'expression': None,
    'motion': None,
    'pet_mode': bool(win_cfg.get('pet_mode', True)),
    'trace': False,
    'expression_list': [],
    'motion_groups': [],
    'mappings': [],
}

EXPRESSION_KEYS = ["love", "sweat", "angry", "dizzy",
                   "ear_fold", "star", "tongue", "blush"]
EXPRESSION_ALIAS = {
    "love": "爱心", "sweat": "流汗", "angry": "生气气", "dizzy": "蚊香",
    "ear_fold": "折耳", "star": "星星", "tongue": "舌头", "blush": "脸红",
}


def _pygame_key_to_hotkey(k):
    """把 pygame 键码转成映射里使用的快捷键字符串 (1-9/0/a-z/f1-f12)"""
    if pygame.K_1 <= k <= pygame.K_9:
        return chr(ord('1') + (k - pygame.K_1))
    if k == pygame.K_0:
        return '0'
    if pygame.K_a <= k <= pygame.K_z:
        return chr(ord('a') + (k - pygame.K_a))
    if pygame.K_F1 <= k <= pygame.K_F12:
        return 'f%d' % (k - pygame.K_F1 + 1)
    if pygame.K_KP1 <= k <= pygame.K_KP9:
        return str(k - pygame.K_KP1 + 1)
    return ''


# ---------------------------------------------------------------------------
# 自写动作播放器
# ---------------------------------------------------------------------------
class MotionPlayer:
    def __init__(self, path):
        self.path = path
        self.curves = {}
        self.duration = 0.0
        self.loop = True
        self.time = 0.0
        self.playing = False
        self.enabled = False
        self._valid_ids = None
        self.ok = False
        self._load(path)

    def _load(self, path):
        try:
            with open(path, encoding='utf-8-sig') as f:
                d = json.load(f)
        except Exception as e:
            print('[motion] 解析失败:', path, e)
            return
        meta = d.get('Meta', {})
        self.duration = float(meta.get('Duration', 0.0) or 0.0)
        self.loop = bool(meta.get('Loop', False))
        for c in d.get('Curves', []):
            if c.get('Target') != 'Parameter':
                continue
            pts = self._parse_segments(c.get('Segments', []))
            if pts:
                self.curves[c.get('Id')] = pts
        self.ok = bool(self.curves) and self.duration > 0
        print('[motion] 已加载 %s : %d 条曲线, 时长 %.2fs, loop=%s'
              % (os.path.basename(path), len(self.curves), self.duration, self.loop))

    @staticmethod
    def _parse_segments(segs):
        pts = []
        n = len(segs)
        if n < 2:
            return pts
        pts.append((float(segs[0]), float(segs[1])))
        i = 2
        while i < n:
            stype = int(segs[i]); i += 1
            if stype == 1:
                i += 4
                if i + 1 < n:
                    pts.append((float(segs[i]), float(segs[i + 1])))
                i += 2
            elif stype in (0, 2, 3):
                if i + 1 < n:
                    pts.append((float(segs[i]), float(segs[i + 1])))
                i += 2
            else:
                break
        return pts

    @staticmethod
    def _sample(pts, t):
        if not pts:
            return 0.0
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        lo, hi = 0, len(pts) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if pts[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        t0, v0 = pts[lo]
        t1, v1 = pts[hi]
        if t1 <= t0:
            return v1
        k = (t - t0) / (t1 - t0)
        return v0 + (v1 - v0) * k

    def set_valid_ids(self, ids):
        self._valid_ids = set(ids)

    def start(self, loop=None):
        if not self.ok:
            return
        if loop is not None:
            self.loop = loop
        self.time = 0.0
        self.playing = True
        self.enabled = True

    def stop(self):
        self.playing = False
        self.enabled = False

    def update(self, dt):
        if not self.playing or self.duration <= 0:
            return
        self.time += dt
        if self.time >= self.duration:
            if self.loop:
                self.time = self.time % self.duration
            else:
                self.time = self.duration
                self.playing = False

    def apply(self, model):
        if not self.enabled:
            return
        t = self.time
        for pid, pts in self.curves.items():
            if self._valid_ids is not None and pid not in self._valid_ids:
                continue
            model.SetParameterValue(pid, self._sample(pts, t))


# ---------------------------------------------------------------------------
# 对外 Python 接口 (线程安全)
# ---------------------------------------------------------------------------
def submit(cmd, *args):
    _commands.put((cmd, args))


def set_expression(name):
    """设置表情: 若 name 命中自定义映射则应用其参数组合, 否则用模型原生表情"""
    submit('expression', name)


def set_expression_index(index):
    submit('expression_index', int(index))


def reset_expression():
    submit('expression_reset')


def apply_mapping(name):
    """按名字应用自定义映射 (expressions_custom.json)"""
    submit('mapping', name)


def set_params(params):
    """直接应用一组参数 {参数ID: 值} (持久)"""
    submit('apply_params', dict(params or {}))


def clear_params():
    """清除当前参数组合"""
    submit('clear_params')


def play_motion(replay=True, loop=True):
    submit('motion', bool(replay), bool(loop))


def stop_motion():
    submit('motion_stop')


def set_pet_mode(flag):
    submit('pet_mode', bool(flag))


def set_background(path):
    submit('background', _abspath(path))


def set_visible(flag):
    submit('visible', bool(flag))


def set_trace(flag):
    submit('trace', bool(flag))


def quit_app():
    submit('quit')


def get_status():
    with _status_lock:
        return dict(_status)


def list_mappings():
    """返回当前可用的自定义映射列表 (名称列表)"""
    if emap is None:
        return []
    return [m['name'] for m in emap.all_mappings()]


# ---------------------------------------------------------------------------
# Win32 结构体 & 常量 + 正确的函数原型
# ---------------------------------------------------------------------------
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte),
                ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte),
                ("AlphaFormat", ctypes.c_byte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
LWA_COLORKEY = 0x00000001
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT),
    ctypes.POINTER(_SIZE), wintypes.HDC, ctypes.POINTER(_POINT),
    wintypes.DWORD, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]


def _make_layered(hwnd):
    if sys.platform != 'win32' or not hwnd:
        return
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)


def set_window_colorkey(hwnd, rgb):
    if sys.platform != 'win32' or not hwnd:
        return
    try:
        _make_layered(hwnd)
        r, g, b = rgb
        colorref = (b << 16) | (g << 8) | r
        user32.SetLayeredWindowAttributes(hwnd, colorref, 0, LWA_COLORKEY)
    except Exception as e:
        print('[live2d] colorkey failed:', e)


def set_window_click_through(hwnd, enable):
    if sys.platform != 'win32' or not hwnd:
        return
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT | WS_EX_LAYERED) if enable \
            else (style & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception as e:
        print('[live2d] click_through failed:', e)


def set_window_topmost(hwnd):
    if sys.platform != 'win32' or not hwnd:
        return
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception as e:
        print('[live2d] topmost failed:', e)


def set_window_visible(hwnd, flag):
    if sys.platform != 'win32' or not hwnd:
        return
    try:
        user32.ShowWindow(hwnd, 5 if flag else 0)
    except Exception as e:
        print('[live2d] visible failed:', e)


def get_cursor_pos():
    p = _POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def get_window_rect(hwnd):
    r = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top


def move_window(hwnd, x, y):
    user32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


class AlphaLayeredWindow:
    """逐像素 alpha 分层窗口 (UpdateLayeredWindow)"""

    def __init__(self, hwnd, width, height):
        self.hwnd = hwnd
        self.width = width
        self.height = height

        _make_layered(hwnd)

        self.hdc_screen = user32.GetDC(0)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)
        self.old_bmp = None
        if not self.hdc_mem:
            raise RuntimeError('CreateCompatibleDC failed')

        self.bmi = _BITMAPINFO()
        h = self.bmi.bmiHeader
        h.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        h.biWidth = width
        h.biHeight = -height          # top-down
        h.biPlanes = 1
        h.biBitCount = 32
        h.biCompression = 0           # BI_RGB

        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(
            self.hdc_mem, ctypes.byref(self.bmi), 0,
            ctypes.byref(self.bits), None, 0)
        if not self.hbmp:
            raise RuntimeError('CreateDIBSection failed')
        self.old_bmp = gdi32.SelectObject(self.hdc_mem, self.hbmp)

        self.blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._size = _SIZE(width, height)
        self._pt_src = _POINT(0, 0)
        self._nbytes = width * height * 4

    def update(self, data):
        if len(data) != self._nbytes:
            return False
        ctypes.memmove(self.bits, data, self._nbytes)
        return bool(user32.UpdateLayeredWindow(
            self.hwnd, self.hdc_screen,
            None,
            ctypes.byref(self._size),
            self.hdc_mem, ctypes.byref(self._pt_src),
            0, ctypes.byref(self.blend), ULW_ALPHA))

    def dispose(self):
        try:
            if self.old_bmp:
                gdi32.SelectObject(self.hdc_mem, self.old_bmp)
            if self.hbmp:
                gdi32.DeleteObject(self.hbmp)
            if self.hdc_mem:
                gdi32.DeleteDC(self.hdc_mem)
            if self.hdc_screen:
                user32.ReleaseDC(0, self.hdc_screen)
        except Exception:
            pass


def snapshot_bgra(buf, width, height):
    """读取当前 OpenGL 后缓冲(预乘 RGBA), 转为 top-down BGRA bytes"""
    GL.glReadPixels(0, 0, width, height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, buf)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)
    arr = arr[::-1]
    bgra = np.ascontiguousarray(arr[:, :, [2, 1, 0, 3]])
    return bgra.tobytes()


# ---------------------------------------------------------------------------
# 主渲染循环
# ---------------------------------------------------------------------------
def main():
    global vol, wav, trace

    # 确保存在映射文件 (无则用内置默认生成)
    if emap is not None:
        if emap.ensure_default_file():
            print('[live2d] 已生成默认 expressions_custom.json')
        emap.reload()

    pygame.init()
    try:
        pygame.mixer.init()
    except Exception:
        pass
    live2d.init()

    width = int(win_cfg.get('width', 500))
    height = int(win_cfg.get('height', 600))
    frameless = bool(win_cfg.get('frameless', True))
    transparent = bool(win_cfg.get('transparent', True))
    tmode = str(win_cfg.get('transparent_mode', 'alpha')).lower()
    always_on_top = bool(win_cfg.get('always_on_top', True))
    click_through = bool(win_cfg.get('click_through', False))
    chroma = tuple(int(c) for c in win_cfg.get('chroma_key', [255, 0, 255]))
    caption = win_cfg.get('caption') or ("ChatBot %s" % ver)
    pet_mode = bool(win_cfg.get('pet_mode', True))

    use_alpha = transparent and tmode == 'alpha'
    use_colorkey = transparent and tmode == 'colorkey'

    flags = pygame.DOUBLEBUF | pygame.OPENGL
    flags |= pygame.NOFRAME if frameless else pygame.RESIZABLE

    pygame.display.set_mode((width, height), flags)
    pygame.display.set_caption(caption)
    live2d.glInit()

    hwnd = None
    try:
        hwnd = pygame.display.get_wm_info().get('window')
    except Exception:
        hwnd = None

    layered = None
    if hwnd:
        if always_on_top:
            set_window_topmost(hwnd)
        if use_alpha:
            try:
                layered = AlphaLayeredWindow(hwnd, width, height)
                print('[live2d] 透明模式: 逐像素 alpha')
            except Exception as e:
                print('[live2d] alpha 初始化失败, 回退 colorkey:', e)
                use_alpha = False
                use_colorkey = transparent
        if use_colorkey:
            set_window_colorkey(hwnd, chroma)
            print('[live2d] 透明模式: 颜色键', chroma)
        if click_through:
            set_window_click_through(hwnd, True)

    # 加载模型
    model = live2d.LAppModel()
    model.LoadModelJson(model_path)
    model.Resize(width, height)
    model.SetAutoBlinkEnable(True)
    model.SetAutoBreathEnable(True)

    try:
        expr_ids = list(model.GetExpressionIds())
    except Exception:
        expr_ids = list(EXPRESSION_KEYS)
    try:
        valid_params = set(model.GetParamIds())
    except Exception:
        valid_params = None

    motion_file = os.path.join(os.path.dirname(model_path),
                               win_cfg.get('motion_file', DEFAULT_MOTION_FILE))
    motion_player = MotionPlayer(motion_file)
    if valid_params:
        motion_player.set_valid_ids(valid_params)

    map_names = list_mappings()
    with _status_lock:
        _status['expression_list'] = expr_ids
        _status['motion_groups'] = ['Idle'] if motion_player.ok else []
        _status['pet_mode'] = pet_mode
        _status['mappings'] = map_names

    print('[live2d] expressions:', expr_ids)
    print('[live2d] mappings:', map_names)
    print('[live2d] motion ok:', motion_player.ok,
          '| params:', (len(valid_params) if valid_params else 'n/a'))

    background = None
    if os.path.exists(background_path):
        try:
            background = Image(background_path)
        except Exception as e:
            print('[live2d] background load failed:', e)

    if motion_player.ok and win_cfg.get('auto_motion', True):
        motion_player.start(loop=True)
        with _status_lock:
            _status['motion'] = 'Idle'

    gl_buf = (ctypes.c_ubyte * (width * height * 4))()

    running = True
    dx = dy = 0.0
    scale = 1.0
    progress = 0
    check = 0
    start_ts = time.time()
    last_time = time.time()
    alpha_failed = False

    dragging = False
    drag_moved = False
    drag_start_cursor = (0, 0)
    drag_win_origin = (0, 0)

    # 持久参数组合 (自定义映射) : 每帧应用
    applied_params = {}

    def do_set_expression(name):
        try:
            model.SetExpression(name)
            with _status_lock:
                _status['expression'] = name
        except Exception as e:
            print('[live2d] set expression failed (%s):' % name, e)

    def apply_mapping_dict(m):
        """应用一条自定义映射: 清除原生表情, 设置参数组合 (每帧持久)"""
        applied_params.clear()
        applied_params.update(m.get('params') or {})
        try:
            model.ResetExpression()
        except Exception:
            pass
        with _status_lock:
            _status['expression'] = m.get('name')

    def resolve_expression(name):
        """名字优先命中自定义映射, 否则走模型原生表情"""
        if emap is not None:
            m = emap.get_by_name(name)
            if m:
                apply_mapping_dict(m)
                return
        do_set_expression(name)

    def apply_command(cmd, args):
        global trace
        nonlocal pet_mode, running, background, dx, dy, scale
        if cmd == 'expression' and args:
            resolve_expression(args[0])
        elif cmd == 'mapping' and args:
            m = emap.get_by_name(args[0]) if emap else None
            if m:
                apply_mapping_dict(m)
        elif cmd == 'apply_params' and args:
            applied_params.clear()
            applied_params.update(args[0] or {})
            with _status_lock:
                _status['expression'] = '__params__'
        elif cmd == 'clear_params':
            applied_params.clear()
            try:
                model.ResetExpression()
            except Exception:
                pass
            with _status_lock:
                _status['expression'] = None
        elif cmd == 'expression_index' and args:
            i = int(args[0])
            if 0 <= i < len(expr_ids):
                resolve_expression(expr_ids[i])
        elif cmd == 'expression_reset':
            applied_params.clear()
            try:
                model.ResetExpression()
                with _status_lock:
                    _status['expression'] = None
            except Exception:
                pass
        elif cmd == 'motion':
            replay = args[0] if args else True
            loop = args[1] if len(args) > 1 else True
            if motion_player.ok:
                if replay:
                    motion_player.start(loop=loop)
                else:
                    motion_player.loop = loop
                    motion_player.playing = True
                    motion_player.enabled = True
                with _status_lock:
                    _status['motion'] = 'Idle'
        elif cmd == 'motion_stop':
            motion_player.stop()
            with _status_lock:
                _status['motion'] = None
        elif cmd == 'pet_mode':
            pet_mode = bool(args[0])
            with _status_lock:
                _status['pet_mode'] = pet_mode
        elif cmd == 'background' and args:
            p = args[0]
            if os.path.exists(p):
                try:
                    background = Image(p)
                    pet_mode = False
                    with _status_lock:
                        _status['pet_mode'] = False
                except Exception as e:
                    print('[live2d] background failed:', e)
        elif cmd == 'visible':
            set_window_visible(hwnd, bool(args[0]))
        elif cmd == 'trace':
            trace = bool(args[0])
            with _status_lock:
                _status['trace'] = trace
        elif cmd == 'quit':
            running = False

    # ------------------------- 主循环 -------------------------
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                break

            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if hwnd:
                    dragging = True
                    drag_moved = False
                    drag_start_cursor = get_cursor_pos()
                    drag_win_origin = get_window_rect(hwnd)

            elif event.type == pygame.MOUSEMOTION:
                if dragging:
                    cx, cy = get_cursor_pos()
                    ox, oy = drag_win_origin
                    sx, sy = drag_start_cursor
                    move_window(hwnd, ox + (cx - sx), oy + (cy - sy))
                    if abs(cx - sx) + abs(cy - sy) > 3:
                        drag_moved = True
                elif trace:
                    mx, my = pygame.mouse.get_pos()
                    model.Drag(mx, my)

            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if dragging:
                    dragging = False
                    if not drag_moved and motion_player.ok:
                        motion_player.start(loop=True)

            if event.type == pygame.KEYDOWN:
                k = event.key

                # 先查自定义映射快捷键
                hk = _pygame_key_to_hotkey(k)
                m = emap.get_by_hotkey(hk) if (emap is not None and hk) else None
                if m is not None:
                    apply_mapping_dict(m)
                    continue

                if k in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif k == pygame.K_LEFT:
                    dx -= 0.1
                elif k == pygame.K_RIGHT:
                    dx += 0.1
                elif k == pygame.K_UP:
                    dy += 0.1
                elif k == pygame.K_DOWN:
                    dy -= 0.1
                elif k == pygame.K_i:
                    scale += 0.1
                elif k == pygame.K_u:
                    scale -= 0.1
                elif k == pygame.K_r:
                    model.StopAllMotions()
                    model.ResetPose()
                elif k == pygame.K_e:
                    applied_params.clear()
                    model.ResetExpression()
                    with _status_lock:
                        _status['expression'] = None
                elif k == pygame.K_t:
                    trace = not trace
                    with _status_lock:
                        _status['trace'] = trace
                elif k == pygame.K_b:
                    pet_mode = not pet_mode
                    with _status_lock:
                        _status['pet_mode'] = pet_mode
                elif k == pygame.K_SPACE:
                    if motion_player.ok:
                        motion_player.start(loop=True)
                elif pygame.K_1 <= k <= pygame.K_8:
                    # 无自定义映射时的兜底: 用模型原生第 N 个表情
                    idx = k - pygame.K_1
                    if idx < len(expr_ids):
                        resolve_expression(expr_ids[idx])
                elif k == pygame.K_0:
                    # 恢复默认状态: 停止动作, 清表情/动作, 参数复位到默认值
                    motion_player.stop()
                    applied_params.clear()
                    try:
                        model.StopAllMotions()
                        model.ResetExpression()
                        model.ResetPose()
                        model.ResetParameters()
                    except Exception:
                        pass
                    with _status_lock:
                        _status['expression'] = None
                        _status['motion'] = None

            if event.type == pygame.VIDEORESIZE:
                pygame.display.set_mode(event.size, flags)
                model.Resize(event.size[0], event.size[1])

        try:
            while True:
                cmd, args = _commands.get_nowait()
                apply_command(cmd, args)
        except queue.Empty:
            pass

        if not running:
            break

        now = time.time()
        dt = now - last_time
        last_time = now
        if dt > 0.1:
            dt = 0.1

        if TEST_SECONDS > 0 and (now - start_ts) > TEST_SECONDS:
            print('[live2d] 测试时间到, 自动退出')
            running = False
            break

        # 1) 动作
        motion_player.update(dt)
        motion_player.apply(model)

        # 2) 更新
        model.Update()

        # 2.5) 自定义映射参数 (持久, 每帧应用)
        if applied_params:
            for pid, v in applied_params.items():
                if valid_params is None or pid in valid_params:
                    model.SetParameterValue(pid, float(v))

        # 3) 口型同步
        if vol == 1:
            try:
                if wav[check] >= 0.1:
                    model.SetParameterValue(
                        StandardParams.ParamMouthOpenY,
                        math.sin(progress / 3) * 0.5)
                else:
                    model.SetParameterValue(StandardParams.ParamMouthOpenY, 0)
                check += 1
            except Exception as e:
                check = 0
                vol = 0
                print(e)
        if vol == 0:
            model.SetParameterValue(StandardParams.ParamMouthOpenY, 0)
            check = 0

        model.SetOffset(dx, dy)
        model.SetScale(scale)

        # 4) 清屏
        if use_alpha:
            live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
        elif use_colorkey:
            live2d.clearBuffer(chroma[0] / 255.0, chroma[1] / 255.0,
                               chroma[2] / 255.0, 1.0)
        else:
            live2d.clearBuffer(1, 1, 1, 0)

        if (not pet_mode) and background is not None:
            background.Draw()

        model.Draw()

        # 5) 逐像素 alpha: 必须在 flip 之前读后缓冲 (预乘 RGBA)
        data = None
        if layered is not None and not alpha_failed:
            try:
                data = snapshot_bgra(gl_buf, width, height)
            except Exception as e:
                print('[live2d] 读取像素失败, 回退 colorkey:', e)
                alpha_failed = True
                data = None

        pygame.display.flip()

        if layered is not None and not alpha_failed and data is not None:
            try:
                if not layered.update(data):
                    print('[live2d] UpdateLayeredWindow 返回失败')
            except Exception as e:
                print('[live2d] alpha 合成失败, 回退 colorkey:', e)
                alpha_failed = True
                if hwnd:
                    set_window_colorkey(hwnd, chroma)

        pygame.time.wait(10)
        progress += 1

    try:
        if layered is not None:
            layered.dispose()
    except Exception:
        pass
    try:
        live2d.dispose()
        live2d.quit()
    except Exception:
        pass
    pygame.quit()


if __name__ == "__main__":
    main()