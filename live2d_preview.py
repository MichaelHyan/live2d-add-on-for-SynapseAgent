# -*- coding: utf-8 -*-
'''
Live2D 参数预览 & 表情映射编辑器
=================================
用途:
  * 浏览当前模型的全部参数(键名), 拖动滑块实时预览效果
  * 启动时从【模型文件】预加载已有表情预设(model3.json + exp3.json)
  * 若模型有预设且尚无匹配的 expressions_custom.json, 自动生成控制文件
  * 若模型无预设, 由用户自行调参编辑
  * 把调好的参数组合保存为一条"表情映射"
  * 每条映射可自定义 快捷键 与 触发词
  * 导出 expressions_custom.json (供 live2d_test.py / chat_gui.py 使用)
  * 提供重置功能: 重置单个参数 / 重置全部(恢复模型默认)

架构:
  * 主线程: tkinter 控制面板
  * 子线程: pygame + live2d 预览窗口 (可拖动)
  * 通过 cmd_q / res_q 两个队列通信
  * 关闭任一方, 双方一起退出 (join 等待渲染线程收尾)

用法:
  python live2d_preview.py
环境变量:
  PREVIEW_TEST_SECONDS=N  运行 N 秒后自动退出(调试用)
'''
import math
import json
import os
import sys
import time
import queue
import threading
import ctypes
from ctypes import wintypes

import pygame
import live2d.v3 as live2d
import model_utils

# ---------------------------------------------------------------------------
# 路径 & 配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(BASE_DIR, 'expressions_custom.json')
TEST_SECONDS = float(os.environ.get('PREVIEW_TEST_SECONDS', '0') or 0)


def _load_json(name, default=None):
    try:
        with open(os.path.join(BASE_DIR, name), encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print('[preview] load %s failed: %s' % (name, e))
        return default if default is not None else {}


def _abspath(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(BASE_DIR, p))


config = _load_json('config.json', {})

# 模型信息(名称/表情/动作)统一由 model_utils 解析
model_path = model_utils.get_model_path()
MODEL_NAME = model_utils.get_model_name()
MODEL_INFO = model_utils.get_model_info()

# 队列: 控制面板 -> 渲染线程 / 渲染线程 -> 控制面板
cmd_q = queue.Queue()
res_q = queue.Queue()

# 表情名 -> 中文别名 (仅用于预设显示)
NAME_ALIAS = {
    'love': '爱心', 'sweat': '流汗', 'angry': '生气气', 'dizzy': '蚊香',
    'ear_fold': '折耳', 'star': '星星', 'tongue': '舌头', 'blush': '脸红',
}

# 表情名 -> 默认触发词 (预设用)
DEFAULT_TRIGGERS = {
    'love': ['爱你', '喜欢', '么么'],
    'sweat': ['流汗', '热'],
    'angry': ['生气', '哼', '讨厌'],
    'dizzy': ['晕', '不懂'],
    'ear_fold': ['折耳'],
    'star': ['星星', '厉害'],
    'tongue': ['舌头', '略略略'],
    'blush': ['脸红', '害羞'],
}


def load_model_presets(mpath):
    """
    从【模型文件】解析已有表情预设:
      1. 读 model3.json 的 FileReferences.Expressions (Name -> File)
      2. 逐个读 exp3.json, 提取 Parameters (Id -> Value)
    返回映射列表 (name/alias/hotkey/triggers/params)
    """
    presets = []
    try:
        with open(mpath, encoding='utf-8-sig') as f:
            mj = json.load(f)
    except Exception as e:
        print('[preview] 读取模型文件失败:', e)
        return presets

    fr = mj.get('FileReferences', {}) or {}
    exprs_def = fr.get('Expressions', []) or []
    base = os.path.dirname(mpath)

    for i, e in enumerate(exprs_def):
        name = str(e.get('Name') or '').strip()
        file = str(e.get('File') or '').strip()
        if not name:
            continue
        params = {}
        if file:
            epath = os.path.join(base, file.replace('/', os.sep))
            try:
                with open(epath, encoding='utf-8-sig') as ef:
                    ed = json.load(ef)
                for p in ed.get('Parameters', []) or []:
                    pid = p.get('Id')
                    val = p.get('Value')
                    if pid is None or val is None:
                        continue
                    try:
                        params[str(pid)] = float(val)
                    except (TypeError, ValueError):
                        params[str(pid)] = 1.0
            except Exception as ex:
                print('[preview] 读取表情文件失败:', file, ex)
        presets.append({
            'name': name,
            'alias': NAME_ALIAS.get(name, ''),
            'hotkey': str(i + 1) if i < 9 else '',
            'triggers': list(DEFAULT_TRIGGERS.get(name, [])),
            'params': params,
        })
    print('[preview] 从模型文件预加载 %d 个表情预设' % len(presets))
    return presets


def ensure_preset_file(presets):
    """
    若模型有预设, 且当前没有匹配该模型的 expressions_custom.json,
    则自动生成预设控制文件。
    返回 (是否生成, 说明文本)
    """
    ok, reason = model_utils.has_preset()
    if ok:
        return False, reason
    if not presets:
        return False, '模型未定义表情预设, 请自行调参并保存'
    try:
        data = {
            'model': os.path.relpath(model_path, BASE_DIR),
            'mappings': presets,
        }
        with open(MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print('[preview] 已根据模型预设生成 expressions_custom.json')
        return True, '已根据模型预设生成 %d 条表情控制映射' % len(presets)
    except Exception as e:
        return False, '生成预设文件失败: %s' % e


# ---------------------------------------------------------------------------
# Win32 辅助 (置顶 / 拖动 / 取光标)
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def set_topmost(hwnd):
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception as e:
        print('[preview] topmost failed:', e)


def get_cursor():
    p = _POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def get_win_pos(hwnd):
    r = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top


def move_win(hwnd, x, y):
    user32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


# ---------------------------------------------------------------------------
# 渲染线程
# ---------------------------------------------------------------------------
WIN_W, WIN_H = 560, 660


def render_main():
    pygame.init()
    live2d.init()
    live2d.setLogEnable(False)

    pygame.display.set_mode((WIN_W, WIN_H),
                            pygame.DOUBLEBUF | pygame.OPENGL | pygame.NOFRAME)
    pygame.display.set_caption('Live2D 预览 - %s (可拖动)' % MODEL_NAME)
    live2d.glInit()

    hwnd = None
    try:
        hwnd = pygame.display.get_wm_info().get('window')
        if hwnd:
            set_topmost(hwnd)
    except Exception:
        hwnd = None

    model = live2d.LAppModel()
    model.LoadModelJson(model_path)
    model.Resize(WIN_W, WIN_H)
    model.SetAutoBlinkEnable(True)
    model.SetAutoBreathEnable(True)

    # 收集参数
    params = []
    try:
        for i in range(model.GetParameterCount()):
            p = model.GetParameter(i)
            params.append({'id': str(p.id), 'min': float(p.min),
                           'max': float(p.max), 'default': float(p.default),
                           'value': float(p.value), 'type': int(p.type)})
    except Exception as e:
        print('[preview] 读取参数失败:', e)
    defaults = {p['id']: p['default'] for p in params}
    exprs = []
    try:
        exprs = list(model.GetExpressionIds())
    except Exception:
        pass

    res_q.put(('ready', params, exprs))

    held = {}
    running = True
    dragging = False
    drag_start = (0, 0)
    drag_orig = (0, 0)
    start_ts = time.time()

    def do_reset_all():
        """重置全部: 恢复模型默认参数 (多重兜底)"""
        held.clear()
        done = False
        try:
            model.ResetParameters()
            done = True
        except Exception:
            pass
        if not done:
            for pid, dv in defaults.items():
                try:
                    model.SetParameterValue(pid, dv)
                except Exception:
                    pass
        try:
            model.ResetExpression()
        except Exception:
            pass

    def do_reset_param(pid):
        """重置单个参数到默认值"""
        held.pop(pid, None)
        dv = defaults.get(pid, None)
        if dv is None:
            return
        try:
            model.SetParameterValue(pid, float(dv))
        except Exception:
            pass

    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                if hwnd:
                    dragging = True
                    drag_start = get_cursor()
                    drag_orig = get_win_pos(hwnd)
            elif e.type == pygame.MOUSEMOTION and dragging:
                cx, cy = get_cursor()
                move_win(hwnd, drag_orig[0] + cx - drag_start[0],
                         drag_orig[1] + cy - drag_start[1])
            elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                dragging = False
            elif e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                running = False

        # 处理命令
        try:
            while True:
                cmd = cmd_q.get_nowait()
                c = cmd[0]
                if c == 'set_param':
                    held[cmd[1]] = float(cmd[2])
                elif c == 'del_param':
                    held.pop(cmd[1], None)
                elif c == 'clear':
                    do_reset_all()
                elif c == 'reset_all':
                    do_reset_all()
                elif c == 'reset_param':
                    do_reset_param(cmd[1])
                elif c == 'apply':
                    held.clear()
                    held.update(cmd[1])
                elif c == 'set_expr':
                    try:
                        model.SetExpression(cmd[1])
                    except Exception:
                        pass
                elif c == 'reset_expr':
                    try:
                        model.ResetExpression()
                    except Exception:
                        pass
                elif c == 'reset_pose':
                    do_reset_all()
                elif c == 'quit':
                    running = False
        except queue.Empty:
            pass

        if TEST_SECONDS > 0 and (time.time() - start_ts) > TEST_SECONDS:
            running = False

        # 应用预览参数
        for pid, v in held.items():
            try:
                model.SetParameterValue(pid, v)
            except Exception:
                pass

        model.Update()
        live2d.clearBuffer(0.16, 0.17, 0.19, 1.0)
        model.Draw()
        pygame.display.flip()
        pygame.time.wait(10)

    # 收尾: 释放 GL / pygame (确保预览窗关闭)
    try:
        live2d.dispose()
        live2d.quit()
    except Exception:
        pass
    try:
        pygame.display.quit()
    except Exception:
        pass
    pygame.quit()
    res_q.put(('closed',))


# ---------------------------------------------------------------------------
# tkinter 控制面板
# ---------------------------------------------------------------------------
import tkinter as tk
from tkinter import messagebox

FONT = ('Microsoft YaHei UI', 10)
FONT_B = ('Microsoft YaHei UI', 10, 'bold')
FONT_T = ('Microsoft YaHei UI', 12, 'bold')


class MappingDialog:
    """表情映射编辑对话框"""

    def __init__(self, master, data=None, combo=None):
        self.result = None
        data = data or {}
        self.top = tk.Toplevel(master)
        self.top.title('编辑映射')
        self.top.configure(bg='#f3f3f3')
        self.top.resizable(False, False)
        self.top.transient(master)
        self.top.grab_set()

        pad = {'padx': 8, 'pady': 4}
        row = 0

        def field(label, init):
            nonlocal row
            tk.Label(self.top, text=label, font=FONT, bg='#f3f3f3',
                     anchor='e', width=8).grid(row=row, column=0, sticky='e', **pad)
            e = tk.Entry(self.top, font=FONT, width=34)
            e.insert(0, init)
            e.grid(row=row, column=1, sticky='we', **pad)
            row += 1
            return e

        self.e_name = field('名称', data.get('name', ''))
        self.e_alias = field('别名', data.get('alias', ''))
        self.e_hotkey = field('快捷键', data.get('hotkey', ''))
        self.e_triggers = field('触发词', '，'.join(data.get('triggers', [])))

        tk.Label(self.top, text='参数', font=FONT, bg='#f3f3f3',
                 anchor='e', width=8).grid(row=row, column=0, sticky='ne', **pad)
        self.t_params = tk.Text(self.top, font=FONT, width=34, height=7)
        self.t_params.grid(row=row, column=1, sticky='we', **pad)
        row += 1

        init_params = data.get('params')
        if init_params is None:
            init_params = combo or {}
        self.t_params.insert('1.0', '\n'.join(
            '%s=%g' % (k, v) if not isinstance(v, str) else '%s=%s' % (k, v)
            for k, v in init_params.items()))

        tk.Label(self.top, text='格式: 每行一个 参数ID=值；快捷键填 1~9 / 字母 / F1~F12；'
                                '触发词用逗号分隔',
                 font=('Microsoft YaHei UI', 8), bg='#f3f3f3',
                 fg='#888').grid(row=row, column=1, sticky='w', **pad)
        row += 1

        btns = tk.Frame(self.top, bg='#f3f3f3')
        btns.grid(row=row, column=0, columnspan=2, pady=8)
        tk.Button(btns, text='确定', font=FONT, width=10,
                  command=self._ok).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text='取消', font=FONT, width=10,
                  command=self._cancel).pack(side=tk.LEFT, padx=6)

        self.top.bind('<Return>', lambda e: self._ok())
        self.top.bind('<Escape>', lambda e: self._cancel())

    def _ok(self):
        params = {}
        for line in self.t_params.get('1.0', tk.END).splitlines():
            line = line.strip()
            if not line or '=' not in line:
                continue
            k, v = line.split('=', 1)
            try:
                params[k.strip()] = float(v.strip())
            except ValueError:
                pass
        triggers = [t.strip() for t in
                    self.e_triggers.get().replace('，', ',').split(',')
                    if t.strip()]
        self.result = {
            'name': self.e_name.get().strip(),
            'alias': self.e_alias.get().strip(),
            'hotkey': self.e_hotkey.get().strip(),
            'triggers': triggers,
            'params': params,
        }
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


class PreviewApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Live2D 参数预览 & 表情映射编辑器 - %s' % MODEL_NAME)
        self.root.geometry('900x680')
        self.root.minsize(760, 540)

        self.params = []          # 全部参数
        self.expr_ids = []
        self.combo = {}           # 当前预览组合 pid->value
        self.mappings = []        # 映射列表
        self.presets = []         # 从模型文件预加载的预设
        self.cur_param = None
        self._setting_scale = False
        self._ready = False
        self._closed = False
        self.render_thread = None

        # 从模型文件预加载表情预设 (不依赖 expressions_custom.json)
        self.presets = load_model_presets(model_path)

        # 若模型有预设但没有匹配的控制文件 -> 自动生成
        self._preset_generated, self._preset_msg = ensure_preset_file(self.presets)

        self._build()

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.render_thread = threading.Thread(
            target=render_main, daemon=True, name='render')
        self.render_thread.start()
        self.root.after(100, self._poll)
        self.root.mainloop()

    # ------------------------- UI -------------------------
    def _build(self):
        root = self.root
        root.configure(bg='#f3f3f3')

        tk.Label(root, text='Live2D 参数预览 & 表情映射编辑器 · %s' % MODEL_NAME,
                 font=FONT_T, bg='#f3f3f3').pack(anchor='w', padx=10, pady=(8, 2))
        self.status_lbl = tk.Label(root, text='正在加载模型…', font=FONT,
                                   bg='#f3f3f3', fg='#2f7cf6', anchor='w')
        self.status_lbl.pack(fill=tk.X, padx=10)

        # 模型信息行 (模型名 / 动作 / 表情)
        mot = MODEL_INFO.get('motions', {}) or {}
        groups = mot.get('groups') or []
        mfile = mot.get('motion_file') or '-'
        info_txt = ('模型：%s ｜ 动作：%s%s ｜ 表情：%d 个'
                    % (MODEL_NAME,
                       ('/'.join(groups) if groups else '自写播放器'),
                       ('(%s)' % mfile) if mfile and mfile != '-' else '',
                       len(MODEL_INFO.get('expressions', []))))
        tk.Label(root, text=info_txt, font=('Microsoft YaHei UI', 9),
                 bg='#f3f3f3', fg='#555', anchor='w').pack(fill=tk.X, padx=10)

        main = tk.Frame(root, bg='#f3f3f3')
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        # 左列: 参数列表
        left = tk.Frame(main, bg='#f3f3f3', width=250)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)

        tk.Label(left, text='模型参数（键名）', font=FONT_B,
                 bg='#f3f3f3').pack(anchor='w')
        self.search_var = tk.StringVar()
        e = tk.Entry(left, textvariable=self.search_var, font=FONT)
        e.pack(fill=tk.X, pady=3)
        e.bind('<KeyRelease>', lambda ev: self._refill_params())

        lf = tk.Frame(left, bg='#f3f3f3')
        lf.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(lf, orient=tk.VERTICAL)
        self.param_list = tk.Listbox(lf, font=FONT, yscrollcommand=sb.set,
                                     activestyle='none')
        sb.config(command=self.param_list.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.param_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.param_list.bind('<<ListboxSelect>>', self._on_select_param)

        # 右列
        right = tk.Frame(main, bg='#f3f3f3')
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        # 参数调节
        box1 = tk.LabelFrame(right, text='参数调节（拖动即时预览）', font=FONT_B,
                             bg='#f3f3f3')
        box1.pack(fill=tk.X)
        self.sel_lbl = tk.Label(box1, text='（未选择参数）', font=FONT,
                                bg='#f3f3f3', anchor='w')
        self.sel_lbl.pack(fill=tk.X, padx=6, pady=(2, 0))
        self.scale = tk.Scale(box1, from_=-1, to=1, resolution=0.01,
                              orient=tk.HORIZONTAL, length=400, font=FONT,
                              bg='#f3f3f3', command=self._on_slide)
        self.scale.pack(fill=tk.X, padx=6)
        rng = tk.Frame(box1, bg='#f3f3f3')
        rng.pack(fill=tk.X, padx=6)
        self.range_lbl = tk.Label(rng, text='', font=FONT, bg='#f3f3f3',
                                  fg='#888', anchor='w')
        self.range_lbl.pack(side=tk.LEFT)
        self.value_lbl = tk.Label(rng, text='', font=FONT_B, bg='#f3f3f3',
                                  fg='#2f7cf6')
        self.value_lbl.pack(side=tk.RIGHT)
        btns = tk.Frame(box1, bg='#f3f3f3')
        btns.pack(fill=tk.X, padx=6, pady=4)
        tk.Button(btns, text='重置此参数', font=FONT, fg='#c0392b',
                  command=self._reset_param).pack(side=tk.LEFT)
        tk.Button(btns, text='设为默认值', font=FONT,
                  command=self._to_default).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text='从组合移除', font=FONT,
                  command=self._del_param).pack(side=tk.LEFT)

        # 当前组合
        box2 = tk.LabelFrame(right, text='当前组合（实时预览中）', font=FONT_B,
                             bg='#f3f3f3')
        box2.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.combo_list = tk.Listbox(box2, font=FONT, height=5,
                                     activestyle='none')
        self.combo_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        btns2 = tk.Frame(box2, bg='#f3f3f3')
        btns2.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Button(btns2, text='重置全部', font=FONT, fg='#c0392b',
                  command=self._reset_all).pack(side=tk.LEFT)
        tk.Button(btns2, text='清空组合', font=FONT,
                  command=self._clear_combo).pack(side=tk.LEFT, padx=6)
        tk.Button(btns2, text='存为映射 →', font=FONT,
                  command=self._save_as_mapping).pack(side=tk.LEFT)

        # 映射列表
        box3 = tk.LabelFrame(right, text='表情映射（快捷键 & 触发词可自定义）',
                             font=FONT_B, bg='#f3f3f3')
        box3.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.map_list = tk.Listbox(box3, font=FONT, height=5,
                                   activestyle='none')
        self.map_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        btns3 = tk.Frame(box3, bg='#f3f3f3')
        btns3.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Button(btns3, text='新建', font=FONT,
                  command=self._new_mapping).pack(side=tk.LEFT)
        tk.Button(btns3, text='编辑', font=FONT,
                  command=self._edit_mapping).pack(side=tk.LEFT, padx=4)
        tk.Button(btns3, text='删除', font=FONT,
                  command=self._del_mapping).pack(side=tk.LEFT)
        tk.Button(btns3, text='应用到预览', font=FONT,
                  command=self._apply_mapping).pack(side=tk.LEFT, padx=4)
        btns3b = tk.Frame(box3, bg='#f3f3f3')
        btns3b.pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Button(btns3b, text='保存到 expressions_custom.json', font=FONT,
                  command=self._save_file).pack(side=tk.LEFT)
        tk.Button(btns3b, text='重新加载', font=FONT,
                  command=self._load_file).pack(side=tk.LEFT, padx=4)

    # ------------------------- 轮询渲染线程 -------------------------
    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                msg = res_q.get_nowait()
                if msg[0] == 'ready':
                    self.params = msg[1]
                    self.expr_ids = msg[2]
                    self._ready = True
                    tail = ('；%s' % self._preset_msg) if self._preset_msg else ''
                    self.status_lbl.config(
                        text='模型 %s 已加载：%d 个参数，%d 个原生表情%s'
                             % (MODEL_NAME, len(self.params),
                                len(self.expr_ids), tail))
                    self._refill_params()
                    self._init_mappings()
                elif msg[0] == 'closed':
                    # 渲染窗已关闭 -> 同步关闭控制面板
                    self._on_close()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ------------------------- 映射初始化 -------------------------
    def _read_custom_file(self):
        try:
            if os.path.exists(MAP_FILE):
                with open(MAP_FILE, encoding='utf-8') as f:
                    data = json.load(f)
                return data.get('mappings', []) or []
        except Exception as e:
            print('[preview] 读取自定义映射失败:', e)
        return []

    @staticmethod
    def _norm_mapping(m):
        return {
            'name': str(m.get('name') or ''),
            'alias': str(m.get('alias') or ''),
            'hotkey': str(m.get('hotkey') or ''),
            'triggers': [str(t).strip() for t in (m.get('triggers') or [])
                         if str(t).strip()],
            'params': {str(k): float(v)
                       for k, v in (m.get('params') or {}).items()},
        }

    def _init_mappings(self):
        """
        以【模型文件】预加载的预设为基础, 若存在自定义文件则按名合并
        (自定义覆盖同名的预设, 新增项追加)。
        """
        base = [self._norm_mapping(p) for p in self.presets]
        by_name = {m['name']: m for m in base}
        for m in self._read_custom_file():
            nm = self._norm_mapping(m)
            name = nm['name']
            if not name:
                continue
            if name in by_name:
                base[base.index(by_name[name])] = nm
                by_name[name] = nm
            else:
                base.append(nm)
                by_name[name] = nm
        self.mappings = base
        self._refresh_map()

    # ------------------------- 参数列表 -------------------------
    def _refill_params(self):
        kw = self.search_var.get().strip().lower()
        self.param_list.delete(0, tk.END)
        for p in self.params:
            pid = p['id']
            if kw and kw not in pid.lower():
                continue
            mark = ' ●' if pid in self.combo else ''
            self.param_list.insert(tk.END, pid + mark)

    def _on_select_param(self, _evt=None):
        sel = self.param_list.curselection()
        if not sel:
            return
        pid = self.param_list.get(sel[0]).replace(' ●', '')
        self.cur_param = pid
        p = next((x for x in self.params if x['id'] == pid), None)
        if not p:
            return
        lo, hi = p['min'], p['max']
        if hi <= lo:
            lo, hi = -1.0, 1.0
        val = self.combo.get(pid, p['default'])
        self._setting_scale = True
        self.scale.config(from_=lo, to=hi,
                          resolution=max((hi - lo) / 200.0, 0.001))
        self.scale.set(val)
        self._setting_scale = False
        self.sel_lbl.config(text='选中参数：%s' % pid)
        self.range_lbl.config(text='min %.3f  max %.3f  default %.3f'
                              % (p['min'], p['max'], p['default']))
        self.value_lbl.config(text='%.3f' % val)

    def _on_slide(self, val):
        if self._setting_scale or not self.cur_param:
            return
        v = float(val)
        self.combo[self.cur_param] = v
        self.value_lbl.config(text='%.3f' % v)
        cmd_q.put(('set_param', self.cur_param, v))

    def _to_default(self):
        if not self.cur_param:
            return
        p = next((x for x in self.params if x['id'] == self.cur_param), None)
        if p:
            self._setting_scale = True
            self.scale.set(p['default'])
            self._setting_scale = False
            self.combo[self.cur_param] = p['default']
            self.value_lbl.config(text='%.3f' % p['default'])
            cmd_q.put(('set_param', self.cur_param, p['default']))

    def _reset_param(self):
        """重置当前参数到模型默认值"""
        if not self.cur_param:
            return
        p = next((x for x in self.params if x['id'] == self.cur_param), None)
        self.combo.pop(self.cur_param, None)
        cmd_q.put(('reset_param', self.cur_param))
        if p:
            self._setting_scale = True
            self.scale.set(p['default'])
            self._setting_scale = False
            self.value_lbl.config(text='%.3f' % p['default'])
        self._refresh_combo()
        self._refill_params()

    def _del_param(self):
        if not self.cur_param:
            return
        self.combo.pop(self.cur_param, None)
        cmd_q.put(('del_param', self.cur_param))
        self._refresh_combo()
        self._refill_params()

    # ------------------------- 组合 -------------------------
    def _refresh_combo(self):
        self.combo_list.delete(0, tk.END)
        for k, v in self.combo.items():
            self.combo_list.insert(tk.END, '%s = %g' % (k, v))

    def _clear_combo(self):
        """清空组合并恢复模型默认"""
        self.combo.clear()
        cmd_q.put(('reset_all',))
        self._refresh_combo()
        self._refill_params()

    def _reset_all(self):
        """重置全部: 恢复模型默认参数与表情"""
        self.combo.clear()
        cmd_q.put(('reset_all',))
        self._refresh_combo()
        self._refill_params()
        self._on_select_param()
        self.status_lbl.config(text='已重置全部参数到模型默认值')

    def _save_as_mapping(self):
        if not self.combo:
            messagebox.showinfo('提示', '当前组合为空，请先拖动滑块调参。')
            return
        self._open_dialog(None)

    # ------------------------- 映射 -------------------------
    def _refresh_map(self):
        self.map_list.delete(0, tk.END)
        for m in self.mappings:
            hk = m.get('hotkey') or '-'
            tg = '/'.join(m.get('triggers', [])) or '-'
            self.map_list.insert(tk.END, '%-12s [键:%s] 触发:%s'
                                 % (m.get('name') or '(无名)', hk, tg))

    def _open_dialog(self, data):
        dlg = MappingDialog(self.root, data=data, combo=self.combo)
        self.root.wait_window(dlg.top)
        if dlg.result:
            if data is not None and data in self.mappings:
                self.mappings[self.mappings.index(data)] = dlg.result
            else:
                if not dlg.result['name']:
                    dlg.result['name'] = 'mapping_%d' % (len(self.mappings) + 1)
                self.mappings.append(dlg.result)
            self._refresh_map()

    def _new_mapping(self):
        self._open_dialog(None)

    def _edit_mapping(self):
        sel = self.map_list.curselection()
        if not sel:
            return
        self._open_dialog(self.mappings[sel[0]])

    def _del_mapping(self):
        sel = self.map_list.curselection()
        if not sel:
            return
        if messagebox.askyesno('确认', '删除该映射？'):
            del self.mappings[sel[0]]
            self._refresh_map()

    def _apply_mapping(self):
        sel = self.map_list.curselection()
        if not sel:
            return
        m = self.mappings[sel[0]]
        self.combo = dict(m.get('params', {}))
        cmd_q.put(('apply', self.combo))
        self._refresh_combo()
        self._refill_params()
        self.status_lbl.config(text='已应用映射：%s' % (m.get('name') or ''))

    # ------------------------- 文件 -------------------------
    def _save_file(self):
        try:
            data = {'model': os.path.relpath(model_path, BASE_DIR),
                    'mappings': self.mappings}
            with open(MAP_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.status_lbl.config(text='已保存到 expressions_custom.json')
            messagebox.showinfo('成功', '已保存 %d 条映射到\n%s'
                                % (len(self.mappings), MAP_FILE))
        except Exception as e:
            messagebox.showerror('保存失败', str(e))

    def _load_file(self):
        self._init_mappings()
        self.status_lbl.config(text='已重新加载 %d 条映射'
                                    % len(self.mappings))

    # ------------------------- 关闭 -------------------------
    def _on_close(self):
        if self._closed:
            return
        self._closed = True
        # 通知渲染线程退出
        try:
            cmd_q.put(('quit',))
        except Exception:
            pass
        # 等待渲染线程优雅收尾 (关闭预览窗 / 释放 GL)
        rt = self.render_thread
        if rt is not None and rt.is_alive():
            try:
                rt.join(timeout=2.5)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass


if __name__ == '__main__':
    PreviewApp()