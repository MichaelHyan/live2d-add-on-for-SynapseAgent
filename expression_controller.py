# -*- coding: utf-8 -*-
'''
表情控制器
==========
统一管理两类表情:

1) 语气表情 (tone expression)
   回复文本命中触发词时播放, 持续 tone_duration 秒后复位。
   触发词来自 expressions_custom.json (expression_map),
   无命中回退内置关键词表 KEYWORD_EXPRESSION。

2) 自动表情
   - 眼跟踪: AI 闲置 (status == 0) 时随机时间跟踪鼠标,
     通过 live2d_test.set_trace(True/False) 控制。
   - 嘴型: 语音播放期间由 chat_gui 侧的 vol / wav 机制驱动
     (本模块不处理, 由音频播放层设置)。

用法:
    ctrl = ExpressionController(tone_duration=1.0, idle_checker=lambda: busy == 0)
    ctrl.play_tone_expression('嘿嘿，好喜欢你喵')   # 播放语气表情(1秒)
    ctrl.start_auto_trace()                         # 开启闲置眼跟踪
'''
import random
import threading
import time

try:
    import expression_map
except Exception:  # pragma: no cover
    expression_map = None

try:
    import live2d_test
except Exception:  # pragma: no cover
    live2d_test = None
    print('[expr] live2d_test 不可用, 表情控制将无效')

# 内置触发词兜底 (自定义映射没命中时使用)
KEYWORD_EXPRESSION = {
    '爱': 'love', '喜欢': 'love', '么么': 'love',
    '生气': 'angry', '哼': 'angry', '讨厌': 'angry',
    '流汗': 'sweat', '热': 'sweat',
    '晕': 'dizzy', '不懂': 'dizzy',
    '星星': 'star', '厉害': 'star',
    '舌头': 'tongue', '略略略': 'tongue',
    '脸红': 'blush', '害羞': 'blush',
}


class ExpressionController:
    def __init__(self, tone_duration=1.0, idle_trace=True, idle_checker=None):
        self.tone_duration = float(tone_duration)
        self.idle_trace_enabled = bool(idle_trace)
        self._idle_checker = idle_checker
        self._tone_timer = None
        self._tone_lock = threading.RLock()
        self._trace_thread = None
        self._trace_stop = threading.Event()

    # ------------------------- 语气表情 -------------------------
    def match_expression(self, text):
        """按触发词匹配表情名, 无命中返回 None"""
        if not text:
            return None
        # 1) 自定义映射触发词 (expressions_custom.json)
        if expression_map is not None:
            try:
                m = expression_map.match_trigger(text)
                if m:
                    return m['name']
            except Exception:
                pass
        # 2) 内置关键词兜底
        for kw, e in KEYWORD_EXPRESSION.items():
            if kw in text:
                return e
        return None

    def play_tone_expression(self, text):
        """播放语气表情: 命中即设置, tone_duration 秒后复位。返回表情名或 None"""
        name = self.match_expression(text)
        if not name or live2d_test is None:
            return None
        with self._tone_lock:
            if self._tone_timer is not None:
                self._tone_timer.cancel()
                self._tone_timer = None
            try:
                live2d_test.set_expression(name)
            except Exception:
                return None
            self._tone_timer = threading.Timer(
                self.tone_duration, self._reset_expression)
            self._tone_timer.daemon = True
            self._tone_timer.start()
        return name

    def _reset_expression(self):
        with self._tone_lock:
            self._tone_timer = None
        try:
            if live2d_test is not None:
                live2d_test.reset_expression()
        except Exception:
            pass

    # ------------------------- 自动表情: 眼跟踪 -------------------------
    def start_auto_trace(self):
        if not self.idle_trace_enabled or live2d_test is None:
            return
        if self._trace_thread is not None and self._trace_thread.is_alive():
            return
        self._trace_stop.clear()
        self._trace_thread = threading.Thread(
            target=self._auto_trace_loop, daemon=True, name='auto-trace')
        self._trace_thread.start()

    def stop_auto_trace(self):
        self._trace_stop.set()
        t = self._trace_thread
        if t is not None:
            t.join(timeout=1.0)
        self._trace_thread = None
        try:
            if live2d_test is not None:
                live2d_test.set_trace(False)
        except Exception:
            pass

    def _auto_trace_loop(self):
        """AI 闲置时随机切换眼跟踪; 非闲置时关闭跟踪"""
        while not self._trace_stop.is_set():
            try:
                idle = True
                if self._idle_checker is not None:
                    idle = bool(self._idle_checker())
                if idle:
                    if random.random() < 0.5:
                        live2d_test.set_trace(True)
                    else:
                        live2d_test.set_trace(False)
                    self._trace_stop.wait(random.uniform(2.0, 5.0))
                else:
                    live2d_test.set_trace(False)
                    self._trace_stop.wait(0.5)
            except Exception:
                self._trace_stop.wait(1.0)