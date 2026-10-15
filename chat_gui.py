# -*- coding: utf-8 -*-
'''
聊天界面 v5 (接入 SynapseAgent)
================================
在 v4 基础上新增:
  * 界面名使用【模型名】(由 model_utils 自动解析, 不再写死)
  * 启动时检测表情预设: 无匹配预设则弹提示 (仍允许继续使用)
  * 语音微调: 以 '[' 开头的系统信息不合成语音

功能:
  * AI 对话 : 通过 synapse_client 走 http://127.0.0.1:56700/cnmd
              - 启动时读取本地提示词 (config.json -> synapse.prompt_file,
                默认 prompt.md), 组装后发送人设覆写指令:
                #bot prompt PROMPTOVERWRITE <prompt.md 全文>
              - 用户消息 -> ask; 单开守护线程轮询 answer(type=rp)
              - 回复剥离指令标签后上屏
  * 表情控制 :
      - 语气表情 : 回复命中触发词 -> 播放 1 秒后复位 (expression_controller)
      - 自动表情 : AI 闲置(status==0)时随机眼跟踪; 语音播放时驱动嘴型
  * 语音 : 开关开启时合成语音并播放 (edge-tts, 失败回退本机离线语音),
          同时驱动 Live2D 嘴型; 对话窗按钮 / 指令 #voice on|off 切换
          每次语音使用唯一临时文件, 播完释放句柄并删除, 避免文件占用
          以 '[' 开头的系统信息不做语音合成

直接运行:
    python chat_gui.py                # 对话窗 + 托管 live2d 桌宠
    python chat_gui.py --no-live2d    # 仅对话窗 (live2d 另开)
被其他程序调用:
    import chat_gui
    chat_gui.start(threaded=True)
    chat_gui.start(manage_live2d=True, block=True)
'''
import os
import sys
import glob
import json
import threading
import queue
import time

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception as e:  # pragma: no cover
    tk = None
    messagebox = None
    print('[chat] tkinter 不可用:', e)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import model_utils
except Exception as e:
    model_utils = None
    print('[chat] model_utils 不可用:', e)


def _load_config():
    default = {
        'synapse': {
            'url': 'http://127.0.0.1:56700/cnmd',
            'timeout': 15,
            'bot_prompt': '#bot prompt',
            'prompt_file': 'prompt.md',
            'poll_interval': 0.5,
        },
        'tts': {'enabled': True, 'voice_offset': 4, 'output_path': 'output.mp3'},
        'auto_expression': {'tone_duration': 1.0, 'idle_trace': True},
        'ffmpeg_path': 'D:\\python\\a\\ffmpeg\\bin\\ffmpeg.exe',
    }
    try:
        with open(os.path.join(BASE_DIR, 'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        for k, v in default.items():
            if k not in cfg:
                cfg[k] = v
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    cfg[k].setdefault(kk, vv)
        return cfg
    except Exception as e:
        print('[chat] config.json 加载失败, 使用默认:', e)
        return default


CONFIG = _load_config()

# 人设覆写标记: '#bot prompt PROMPTOVERWRITE <提示词>' 可覆盖 SynapseAgent 预设人设
PROMPT_OVERWRITE_FLAG = 'PROMPTOVERWRITE'


def load_bot_prompt():
    '''
    组装人设覆写指令:

        #bot prompt PROMPTOVERWRITE <本地提示词全文>

    指令前缀取自 config.json -> synapse.bot_prompt (默认 '#bot prompt'),
    提示词正文取自 config.json -> synapse.prompt_file (默认 prompt.md)。
    读取失败或内容为空时返回 '', 由调用方跳过发送。
    '''
    syn_cfg = CONFIG.get('synapse', {})
    prefix = (syn_cfg.get('bot_prompt') or '#bot prompt').strip()
    fname = syn_cfg.get('prompt_file') or 'prompt.md'
    path = fname if os.path.isabs(fname) else os.path.join(BASE_DIR, fname)
    try:
        with open(path, encoding='utf-8') as f:
            content = f.read().strip()
    except Exception as e:
        print('[chat] 人设提示词读取失败 (%s): %s' % (path, e))
        return ''
    if not content:
        print('[chat] 人设提示词为空, 跳过覆写:', path)
        return ''
    return '%s %s %s' % (prefix, PROMPT_OVERWRITE_FLAG, content)

# 模型名 (界面显示用)
if model_utils is not None:
    try:
        MODEL_NAME = model_utils.get_model_name()
    except Exception:
        MODEL_NAME = 'Live2D'
else:
    MODEL_NAME = 'Live2D'

# 可选依赖
try:
    import expression_map
except Exception as e:
    expression_map = None
    print('[chat] expression_map 不可用:', e)

try:
    import synapse_client
except Exception as e:
    synapse_client = None
    print('[chat] synapse_client 不可用:', e)

try:
    import expression_controller as expr_ctrl_mod
except Exception as e:
    expr_ctrl_mod = None
    print('[chat] expression_controller 不可用:', e)


def clean_for_speech(text):
    """去掉（...）/(...) 内的动作描述, 便于语音朗读"""
    out = ''
    depth = 0
    for ch in text or '':
        if ch in '（(':
            depth += 1
            continue
        if ch in '）)':
            if depth > 0:
                depth -= 1
            else:
                out += '。'
            continue
        if depth == 0:
            out += ch
    return out.strip()


def is_system_message(text):
    """以 '[' 开头(忽略前导空白)视为系统信息, 不合成语音"""
    return bool(text) and text.lstrip().startswith('[')


def safe_remove(path, retries=6, delay=0.1):
    """尽力删除文件 (句柄可能延迟释放), 失败不抛异常"""
    for _ in range(retries):
        try:
            if path and os.path.exists(path):
                os.remove(path)
            return True
        except Exception:
            time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
THEMES = {
    'light': {
        'bg': '#eceff4', 'header_bg': '#ffffff', 'header_fg': '#2b2b2b',
        'header_sub': '#8a94a6', 'chat_bg': '#eceff4', 'user_bubble': '#2f7cf6',
        'user_fg': '#ffffff', 'bot_bubble': '#ffffff', 'bot_fg': '#222222',
        'sys_fg': '#9aa3b2', 'input_bg': '#ffffff', 'input_fg': '#222222',
        'btn_bg': '#2f7cf6', 'btn_fg': '#ffffff', 'btn_hover': '#1f63d0',
        'sep': '#dfe3ea',
    },
    'dark': {
        'bg': '#1e1f22', 'header_bg': '#2b2d31', 'header_fg': '#e8e8e8',
        'header_sub': '#8a90a0', 'chat_bg': '#1e1f22', 'user_bubble': '#3b82f6',
        'user_fg': '#ffffff', 'bot_bubble': '#313338', 'bot_fg': '#e8e8e8',
        'sys_fg': '#7a8190', 'input_bg': '#313338', 'input_fg': '#e8e8e8',
        'btn_bg': '#3b82f6', 'btn_fg': '#ffffff', 'btn_hover': '#2f6fd0',
        'sep': '#3a3d42',
    },
}

FONT = ('Microsoft YaHei UI', 10)
FONT_TITLE = ('Microsoft YaHei UI', 12, 'bold')
FONT_BUBBLE = ('Microsoft YaHei UI', 10)
BUBBLE_WRAP = 280


# ---------------------------------------------------------------------------
# 聊天窗口
# ---------------------------------------------------------------------------
class ChatWindow:
    def __init__(self, title='对话', on_reply=None, manage_live2d=False):
        self.on_reply = on_reply
        self.root = None
        self.theme = 'light'
        self.messages = []                # [(role, row_frame, bubble_label)]
        self.root_title = title
        self.manage_live2d = manage_live2d
        self.live2d_thread = None
        self._live2d = None
        self._shutdown_done = False
        self._preset_checked = False

        # 语音开关
        self.voice_enabled = bool(CONFIG['tts'].get('enabled', True))
        self.voice_offset = int(CONFIG['tts'].get('voice_offset', 4))
        self._voice_seq = 0                # 语音文件序号 (保证文件名唯一)

        # Synapse 客户端
        syn_cfg = CONFIG['synapse']
        self.poll_interval = float(syn_cfg.get('poll_interval', 0.5))
        self.bot_prompt = load_bot_prompt()
        self.client = None
        if synapse_client is not None:
            self.client = synapse_client.SynapseClient(
                url=syn_cfg.get('url', synapse_client.DEFAULT_URL),
                timeout=int(syn_cfg.get('timeout', 15)))

        # 线程 / 队列
        self._reply_queue = queue.Queue()     # AI 回复文本
        self._voice_queue = queue.Queue()     # 待朗读文本
        self._stop = threading.Event()
        self._agent_busy = False
        self._poll_thread = None
        self._voice_thread = None
        self._reply_started = False

        # 表情控制器
        self.ctrl = None
        if expr_ctrl_mod is not None:
            ae = CONFIG.get('auto_expression', {})
            self.ctrl = expr_ctrl_mod.ExpressionController(
                tone_duration=float(ae.get('tone_duration', 1.0)),
                idle_trace=bool(ae.get('idle_trace', True)),
                idle_checker=lambda: not self._agent_busy)

        # widget refs
        self.header = self.title_lbl = self.sub_lbl = None
        self.theme_btn = self.voice_btn = None
        self.body = self.canvas = self.vsb = self.msg_frame = None
        self.bottom = self.entry = self.send_btn = None

    # ------------------------- UI 构建 -------------------------
    def _build(self):
        root = tk.Tk()
        root.title('%s · 对话' % MODEL_NAME)
        root.geometry('440x640')
        root.minsize(340, 480)
        root.configure(bg=THEMES[self.theme]['bg'])
        root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.header = tk.Frame(root, height=52)
        self.header.pack(fill=tk.X, side=tk.TOP)
        self.header.pack_propagate(False)

        # 标题使用模型名 (不再写死)
        self.title_lbl = tk.Label(self.header, text=MODEL_NAME, font=FONT_TITLE)
        self.title_lbl.pack(side=tk.LEFT, padx=(14, 6))
        self.sub_lbl = tk.Label(self.header, text='在线', font=FONT)
        self.sub_lbl.pack(side=tk.LEFT)

        self.theme_btn = tk.Button(
            self.header, text='🌙 暗色', font=FONT, relief=tk.FLAT,
            bd=0, cursor='hand2', command=self.toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT, padx=(0, 10))

        self.voice_btn = tk.Button(
            self.header, font=FONT, relief=tk.FLAT, bd=0,
            cursor='hand2', command=self.toggle_voice)
        self.voice_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self.sep = tk.Frame(root, height=1)
        self.sep.pack(fill=tk.X)

        self.body = tk.Frame(root)
        self.body.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self.body, highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(self.body, orient=tk.VERTICAL,
                                command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.msg_frame = tk.Frame(self.canvas)
        self._cw = self.canvas.create_window((0, 0), window=self.msg_frame,
                                             anchor='nw')
        self.msg_frame.bind(
            '<Configure>',
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind(
            '<Configure>',
            lambda e: self.canvas.itemconfig(self._cw, width=e.width))
        self.canvas.bind_all('<MouseWheel>', self._on_wheel)

        self.bottom = tk.Frame(root)
        self.bottom.pack(fill=tk.X, side=tk.BOTTOM)

        self.entry = tk.Entry(self.bottom, font=FONT, relief=tk.FLAT, bd=0)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                        padx=(12, 6), pady=10, ipady=8)
        self.entry.bind('<Return>', lambda e: self.on_send())

        self.send_btn = tk.Button(
            self.bottom, text='发送', font=FONT, relief=tk.FLAT, bd=0,
            cursor='hand2', command=self.on_send)
        self.send_btn.pack(side=tk.LEFT, padx=(0, 12), ipadx=14, ipady=6)

        self._add_bubble('bot', '你好呀～我是 %s，正在连接 Agent…' % MODEL_NAME)
        self.root = root
        self._apply_theme()

    # ------------------------- 主题 -------------------------
    def toggle_theme(self):
        self.theme = 'dark' if self.theme == 'light' else 'light'
        self._apply_theme()

    def _apply_theme(self):
        t = THEMES[self.theme]
        self.root.configure(bg=t['bg'])
        self.header.configure(bg=t['header_bg'])
        self.title_lbl.configure(bg=t['header_bg'], fg=t['header_fg'])
        self.sub_lbl.configure(bg=t['header_bg'], fg=t['header_sub'])
        self.theme_btn.configure(
            text=('🌙 暗色' if self.theme == 'light' else '☀ 亮色'),
            bg=t['header_bg'], fg=t['header_fg'],
            activebackground=t['header_bg'], activeforeground=t['header_fg'])
        if self.voice_btn is not None:
            self.voice_btn.configure(
                text=('🔊 语音开' if self.voice_enabled else '🔇 语音关'),
                bg=t['header_bg'],
                fg=(t['header_fg'] if self.voice_enabled else t['header_sub']),
                activebackground=t['header_bg'], activeforeground=t['header_fg'])
        self.sep.configure(bg=t['sep'])
        self.body.configure(bg=t['chat_bg'])
        self.canvas.configure(bg=t['chat_bg'])
        self.msg_frame.configure(bg=t['chat_bg'])
        self.bottom.configure(bg=t['bg'])
        self.entry.configure(bg=t['input_bg'], fg=t['input_fg'],
                             insertbackground=t['input_fg'])
        self.send_btn.configure(
            bg=t['btn_bg'], fg=t['btn_fg'],
            activebackground=t['btn_hover'], activeforeground=t['btn_fg'])
        for role, row, bubble in self.messages:
            row.configure(bg=t['chat_bg'])
            if role == 'user':
                bubble.configure(bg=t['user_bubble'], fg=t['user_fg'])
            elif role == 'bot':
                bubble.configure(bg=t['bot_bubble'], fg=t['bot_fg'])
            else:
                bubble.configure(bg=t['chat_bg'], fg=t['sys_fg'])

    # ------------------------- 消息气泡 -------------------------
    def _add_bubble(self, role, text):
        t = THEMES[self.theme]
        row = tk.Frame(self.msg_frame, bg=t['chat_bg'])
        row.pack(fill=tk.X, padx=10, pady=4)
        if role == 'user':
            bubble = tk.Label(row, text=text, font=FONT_BUBBLE,
                              bg=t['user_bubble'], fg=t['user_fg'],
                              wraplength=BUBBLE_WRAP, justify=tk.LEFT,
                              padx=12, pady=8)
            bubble.pack(side=tk.RIGHT, anchor='e')
        elif role == 'bot':
            bubble = tk.Label(row, text=text, font=FONT_BUBBLE,
                              bg=t['bot_bubble'], fg=t['bot_fg'],
                              wraplength=BUBBLE_WRAP, justify=tk.LEFT,
                              padx=12, pady=8)
            bubble.pack(side=tk.LEFT, anchor='w')
        else:
            bubble = tk.Label(row, text=text, font=FONT_BUBBLE,
                              bg=t['chat_bg'], fg=t['sys_fg'],
                              wraplength=BUBBLE_WRAP, justify=tk.CENTER)
            bubble.pack(anchor='center')
        self.messages.append((role, row, bubble))
        self._scroll_bottom()

    def _scroll_bottom(self):
        try:
            self.canvas.update_idletasks()
            self.canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), 'units')
        except Exception:
            pass

    # ------------------------- 语音开关 -------------------------
    def toggle_voice(self):
        self.voice_enabled = not self.voice_enabled
        self._apply_theme()
        self._add_bubble('sys', '—— 语音已%s ——' %
                         ('开启' if self.voice_enabled else '关闭'))

    # ------------------------- 预设检测 -------------------------
    def _check_preset(self):
        """启动时检测表情预设; 无匹配预设则弹提示 (仍允许继续使用)"""
        if self._preset_checked:
            return
        self._preset_checked = True
        if model_utils is None:
            return
        try:
            ok, reason = model_utils.has_preset()
        except Exception as e:
            print('[chat] 预设检测失败:', e)
            return
        if ok:
            print('[chat] %s' % reason)
            return
        # 无预设: 提示用户先去预览编辑器生成
        self._add_bubble(
            'sys', '—— 未检测到该模型的表情预设：%s ——' % reason)
        self._add_bubble(
            'sys', '—— 可运行 live2d_preview.py 编辑并保存表情控制文件 ——')
        if messagebox is not None:
            try:
                messagebox.showwarning(
                    '未检测到表情预设',
                    '%s\n\n建议先运行 live2d_preview.py，\n'
                    '为当前模型 %s 编辑并保存表情控制文件。\n\n'
                    '（仍可继续使用，但表情/触发词映射可能不可用）'
                    % (reason, MODEL_NAME))
            except Exception:
                pass

    # ------------------------- 发送 -------------------------
    def on_send(self):
        msg = self.entry.get().strip()
        if not msg:
            return
        self.entry.delete(0, tk.END)

        if msg == '#clear':
            self._clear_chat()
            return
        if msg == '#reload':
            if expression_map is not None:
                expression_map.reload()
            self._add_bubble('sys', '—— 已重新加载自定义触发词映射 ——')
            return
        if msg in ('#voice on', '#voice 开'):
            self.voice_enabled = True
            self._apply_theme()
            self._add_bubble('sys', '—— 语音已开启 ——')
            return
        if msg in ('#voice off', '#voice 关'):
            self.voice_enabled = False
            self._apply_theme()
            self._add_bubble('sys', '—— 语音已关闭 ——')
            return

        self._add_bubble('user', msg)

        # 发送到 SynapseAgent (非阻塞)
        if self.client is None:
            self._add_bubble('sys', '—— SynapseAgent 客户端不可用 ——')
            return
        threading.Thread(target=self._ask_worker, args=(msg,),
                         daemon=True, name='ask').start()

    def _ask_worker(self, msg):
        try:
            self.client.ask(msg)
        except Exception as e:
            self._reply_queue.put('[连接 Agent 失败] %s' % e)

    def _clear_chat(self):
        for _, row, _ in self.messages:
            row.destroy()
        self.messages.clear()
        self._add_bubble('sys', '—— 对话已清空 ——')

    # ------------------------- 回复轮询 -------------------------
    def _start_reply_poll(self):
        if self._reply_started or self.client is None:
            return
        self._reply_started = True
        # 初始化: 连通测试 + 覆写人设
        threading.Thread(target=self._init_agent, daemon=True,
                         name='agent-init').start()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='agent-poll')
        self._poll_thread.start()

    def _init_agent(self):
        try:
            self.client.ping()
        except Exception as e:
            print('[chat] Agent ping 失败:', e)
        try:
            if self.bot_prompt:
                self.client.set_prompt(self.bot_prompt)
                print('[chat] 已发送人设覆写指令 (PROMPTOVERWRITE), '
                      '提示词长度 %d 字符' % len(self.bot_prompt))
            else:
                print('[chat] 未加载到人设提示词, 跳过覆写')
        except Exception as e:
            print('[chat] 人设指令发送失败:', e)

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                status, resp = self.client.answer_rp()
                self._agent_busy = bool(status)
                text = synapse_client.strip_tool_call(resp)
                if text:
                    self._reply_queue.put(text)
            except Exception:
                self._agent_busy = False
                time.sleep(1.0)
            self._stop.wait(self.poll_interval)

    def _poll_replies(self):
        """UI 线程轮询回复队列"""
        try:
            while True:
                text = self._reply_queue.get_nowait()
                self._add_bubble('bot', text)
                if self.on_reply:
                    try:
                        self.on_reply(None, text, None)
                    except Exception:
                        pass
                # 语气表情 (1 秒)
                if self.ctrl is not None:
                    try:
                        self.ctrl.play_tone_expression(text)
                    except Exception:
                        pass
                # 语音: 系统信息 (以 '[' 开头) 不合成
                if self.voice_enabled and not is_system_message(text):
                    spoken = clean_for_speech(text)
                    if spoken:
                        self._voice_queue.put(spoken)
        except queue.Empty:
            pass
        if self.root is not None and not self._shutdown_done:
            self.root.after(50, self._poll_replies)

    # ------------------------- 语音播放 -------------------------
    def _cleanup_old_voice_files(self):
        """清理历史遗留的临时语音文件 (.mp3 / .wav)"""
        for pat in ('voice_*.mp3', 'voice_*.wav'):
            for p in glob.glob(os.path.join(BASE_DIR, pat)):
                safe_remove(p)

    def _start_voice_worker(self):
        if self._voice_thread is not None and self._voice_thread.is_alive():
            return
        self._cleanup_old_voice_files()
        self._voice_thread = threading.Thread(
            target=self._voice_loop, daemon=True, name='voice')
        self._voice_thread.start()

    def _voice_loop(self):
        while not self._stop.is_set():
            try:
                text = self._voice_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._speak(text)
            except Exception as e:
                print('[chat] 语音播放失败:', e)

    def _speak(self, text):
        """
        合成并播放语音:
          - 使用唯一文件名, 播完释放句柄并删除 (避免文件占用)
          - tts.bake 可能返回 .mp3 (联网) 或 .wav (离线回退), 以其返回值为准
        """
        import tts
        import waver
        import live2d_test
        import pygame

        self._voice_seq += 1
        base_path = os.path.join(BASE_DIR, 'voice_%d.mp3' % self._voice_seq)

        # 1) 合成语音 (联网失败会回退离线, 返回实际文件路径)
        try:
            actual_path = tts.bake(text, base_path, self.voice_offset)
        except Exception:
            safe_remove(base_path)
            safe_remove(os.path.splitext(base_path)[0] + '.wav')
            raise
        if not actual_path or not os.path.exists(actual_path):
            raise RuntimeError('语音文件未生成')

        # 2) 生成口型包络
        try:
            live2d_test.wav = waver.get_wave(actual_path)
        except Exception as e:
            print('[chat] 音频包络生成失败:', e)
            live2d_test.wav = []

        # 3) 播放 (播完释放句柄)
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(actual_path)
            live2d_test.vol = 1
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy() and not self._stop.is_set():
                time.sleep(0.1)
        finally:
            try:
                live2d_test.vol = 0
            except Exception:
                pass
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            try:
                pygame.mixer.music.unload()      # 释放文件句柄
            except Exception:
                pass
            safe_remove(actual_path)
            if actual_path != base_path:
                safe_remove(base_path)

    # ------------------------- live2d 托管 -------------------------
    def _start_live2d(self):
        try:
            import live2d_test
            self._live2d = live2d_test
            self.live2d_thread = threading.Thread(
                target=live2d_test.main, daemon=True, name='live2d')
            self.live2d_thread.start()
            print('[chat] live2d 已启动 (随窗口关闭一并退出)')
        except Exception as e:
            print('[chat] live2d 启动失败(可忽略):', e)

    def _stop_live2d(self):
        if self._live2d is not None:
            try:
                self._live2d.quit_app()
            except Exception:
                pass
        if self.live2d_thread is not None:
            try:
                self.live2d_thread.join(timeout=2.5)
            except Exception:
                pass
            self.live2d_thread = None

    # ------------------------- 生命周期 -------------------------
    def _on_close(self):
        self._shutdown()

    def _shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stop.set()
        if self.ctrl is not None:
            try:
                self.ctrl.stop_auto_trace()
            except Exception:
                pass
        self._stop_live2d()
        try:
            if self.root is not None:
                self.root.destroy()
        except Exception:
            pass
        self._cleanup_old_voice_files()

    def run(self, title='对话'):
        if tk is None:
            print('[chat] 无 tkinter, 无法启动界面')
            return
        self.root_title = title
        self._build()
        if self.manage_live2d:
            self._start_live2d()
        if expression_map is not None:
            try:
                expression_map.reload()
            except Exception:
                pass
        self._start_reply_poll()
        self._start_voice_worker()
        if self.ctrl is not None:
            self.ctrl.start_auto_trace()
        # 启动后检测预设 (不阻塞界面)
        self.root.after(300, self._check_preset)
        self.root.after(50, self._poll_replies)
        try:
            self.root.mainloop()
        finally:
            self._shutdown()


def start(title='对话', on_reply=None, threaded=False, block=True,
          manage_live2d=False):
    """启动对话窗口"""
    win = ChatWindow(title=title, on_reply=on_reply,
                     manage_live2d=manage_live2d)
    if threaded:
        t = threading.Thread(target=win.run, args=(title,), daemon=True,
                             name='chat-gui')
        t.start()
        return t
    win.run(title)
    return None


if __name__ == '__main__':
    no_live2d = '--no-live2d' in sys.argv
    if no_live2d:
        print('[chat] 仅启动对话窗口 (未托管 live2d)')
    start('与 %s 对话' % MODEL_NAME, manage_live2d=not no_live2d)