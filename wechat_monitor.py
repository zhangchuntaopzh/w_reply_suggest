#!/usr/bin/env python3
"""
微信消息监测 GUI — 5秒轮询，AI 回复建议
监测 /v1/sessions, /v1/messages, /v1/contacts
"""

import os as _os
if _os.environ.get('SSLKEYLOGFILE') == 'C:\\ssolog':
    del _os.environ['SSLKEYLOGFILE']

import json, time, re, os, sys, threading
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
import tkinter as tk
from tkinter import ttk, font

# ── 配置 ──
BASE_URL = 'http://127.0.0.1:5031/v1'
MAX_SESSIONS = 20
MSG_HISTORY = 10
TZ = timezone(timedelta(hours=8))
if getattr(sys, 'frozen', False):
    CONFIG_DIR = os.path.dirname(sys.executable)
else:
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'wechat_monitor_config.json')

# LLM 默认配置
DEFAULT_LLM_CONFIG = {
    'endpoint': 'https://api.deepseek.com/anthropic/v1/messages',
    'model': 'deepseek-v4-flash',
    'max_tokens': 150,
    'temperature': 0.7,
}

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                # 合并默认值（新增字段用默认）
                for k in DEFAULT_LLM_CONFIG:
                    cfg.setdefault(k, DEFAULT_LLM_CONFIG[k])
                return cfg
    except Exception:
        pass
    return dict(DEFAULT_LLM_CONFIG)

def save_config(cfg):
    cfg['important_contacts'] = list(IMPORTANT_CONTACTS)
    cfg['session_categories'] = dict(SESSION_CATEGORIES)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

llm_config = load_config()

# ── 重要联系人标记（勾选后使用 LLM 仔细回答）──
IMPORTANT_CONTACTS = set(llm_config.get('important_contacts', []))

# ── 会话分类（影响 LLM 回复语气）──
SESSION_CATEGORIES = llm_config.get('session_categories', {})
CATEGORY_PROMPTS = {
    '家人': '（对方是你的家人，语气要温暖亲切，像对待家人一样自然随意）',
    '朋友': '（对方是你的朋友，语气要轻松友好，像朋友一样随和）',
    '爱人': '（对方是你的爱人，语气要温柔体贴，充满爱意）',
    '同学': '（对方是你的老同学，语气要平等友善，自然亲切）',
    '同事': '（对方是你的同事，语气要礼貌专业，保持适当的职场距离）',
    '其他': '',
    '敌人': '（对方与你不和，语气要冷淡疏远，保持距离）',
}

# ── 状态 ──
name_cache = {}
processed = set()
last_contact_sync = 0
last_session_sync = 0
last_msg_sync = 0

# ── HTTP ──

def api_get(path, params=None):
    url = f'{BASE_URL}{path}'
    if params:
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        url = f'{url}?{qs}'
    try:
        with urlopen(Request(url, method='GET'), timeout=8) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'success': False, 'error': str(e)}

def ts_str(ts):
    dt = datetime.fromtimestamp(ts, tz=TZ)
    now = datetime.now(TZ)
    if dt.date() == now.date():
        return dt.strftime('%H:%M:%S')
    if dt.date() == (now - timedelta(days=1)).date():
        return '昨天 ' + dt.strftime('%H:%M')
    return dt.strftime('%m-%d %H:%M')

# ── 联系人缓存 ──

def refresh_names():
    global name_cache, last_contact_sync
    ok = 0
    fail = 0
    for ep, limit in [('/contacts', 500), ('/sessions', 100)]:
        r = api_get(ep, {'limit': limit, 'includeAvatar': False})
        if r.get('success'):
            items = r.get('data', {}).get(ep.strip('/'), [])
            for it in items:
                uname = it.get('username', '')
                dname = it.get('displayName', '') or it.get('nickname', '') or uname
                if dname:
                    name_cache[uname] = dname
            ok += 1
        else:
            fail += 1
    last_contact_sync = int(time.time())
    return ok > 0, f'联系人 {len(name_cache)} 个'

def get_name(uid):
    return name_cache.get(uid, uid)

# ── LLM ──

def llm_suggest(text, context_msgs, sender_name, category=''):
    global llm_config
    cat_hint = CATEGORY_PROMPTS.get(category, '') if category else ''
    lines = []
    for m in context_msgs[-8:]:
        who = '我' if m.get('direction') == 'out' else m.get('senderName', '对方')
        lines.append(f'{who}: {m.get("parsedContent", "")}')
    ctx = '\n'.join(lines)
    tone = f' {cat_hint}' if cat_hint else ''
    prompt = f'你是一个得体的微信聊天助手。根据以下对话上下文，为最新一条来自【{sender_name}】的消息\n生成一个自然、简短、得体的回复建议（一句话即可，不要解释，直接给出建议内容）。{tone}\n\n对话上下文：\n{ctx}\n\n【{sender_name}】的最新消息：\n{text}\n\n回复建议：'
    payload = json.dumps({
        'model': llm_config['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 150,
        'temperature': 0.7,
    }).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': llm_config.get('api_key', ''),
        'anthropic-version': '2023-06-01',
    }
    try:
        req = Request(llm_config['endpoint'], data=payload, headers=headers, method='POST')
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode('utf-8'))
            for block in data.get('content', []):
                if block.get('type') == 'text':
                    return block['text'].strip()
            choice = data.get('choices', [{}])[0]
            msg = choice.get('message', {}).get('content', '') or choice.get('text', '')
            return msg.strip() if msg else None
    except Exception:
        return None

def llm_suggest_important(text, context_msgs, sender_name, category=''):
    """为重要联系人生成更详细、体贴的回复建议"""
    global llm_config
    cat_hint = CATEGORY_PROMPTS.get(category, '') if category else ''
    lines = []
    for m in context_msgs[-8:]:
        who = '我' if m.get('direction') == 'out' else m.get('senderName', '对方')
        lines.append(f'{who}: {m.get("parsedContent", "")}')
    ctx = '\n'.join(lines)
    tone = f' {cat_hint}' if cat_hint else ''
    prompt = f'你是一个得体的微信聊天助手。以下是与【{sender_name}】的对话上下文。\n{sender_name}对你来说是一位重要的人，请为TA的最新消息\n生成一个详细、体贴、真诚的回复建议（2-3句话，自然口语化，不要解释，直接给出建议内容）。{tone}\n\n对话上下文：\n{ctx}\n\n【{sender_name}】的最新消息：\n{text}\n\n回复建议：'
    payload = json.dumps({
        'model': llm_config['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 300,
        'temperature': 0.8,
    }).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': llm_config.get('api_key', ''),
        'anthropic-version': '2023-06-01',
    }
    try:
        req = Request(llm_config['endpoint'], data=payload, headers=headers, method='POST')
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode('utf-8'))
            for block in data.get('content', []):
                if block.get('type') == 'text':
                    return block['text'].strip()
            choice = data.get('choices', [{}])[0]
            msg = choice.get('message', {}).get('content', '') or choice.get('text', '')
            return msg.strip() if msg else None
    except Exception:
        return None

# ── 规则引擎 ──

def fallback_reply(text, msg_type):
    if msg_type == 'image':
        return '有图片，可回复"看到了"或直接评论图片内容'
    if msg_type == 'emoji':
        return '回复对应表情即可'
    if msg_type in ('video', 'voice'):
        return '看完/听完后回复'
    if msg_type in ('app_file', 'app_mini_program'):
        return '回复"收到"并确认'
    t = text.strip()
    if re.fullmatch(r'[\U0001F300-\U0010FFFF☀-➿︀-️‍]+', t):
        return '回复对应表情即可'
    if t in ('好', '嗯', '哦', 'OK', 'ok', '好的', '嗯嗯', '收到', '是的', '对', '行'):
        return '可回复"好的"或确认'
    if any(k in t for k in ('谢谢', '感谢', '多谢', '辛苦了')):
        return '回复"不客气"或"应该的"'
    if '？' in t or '?' in t:
        return '对方在提问，请根据实际情况回答'
    if len(t) > 60:
        return '对方发了较长的消息，仔细阅读后给出针对性回应'
    return '阅读后根据实际情况回复'

# ── 消息分析 ──

def analyze_session(session_id, session_name, session_type):
    r = api_get('/messages', {
        'sessionId': session_id,
        'limit': MSG_HISTORY,
        'sort': 'createTime_desc',
    })
    if not r.get('success'):
        return None, None
    msgs = r.get('data', {}).get('messages', [])
    if not msgs:
        return None, None
    msgs.reverse()
    context = []
    new_items = []
    for m in msgs:
        sid = str(m.get('serverId', 0))
        direction = m.get('direction', 'in')
        parsed = m.get('parsedContent', '')
        kind = m.get('messageKind', 'text')
        ct = m.get('createTime', 0)
        sender = m.get('senderUsername', '')
        sender_obj = m.get('sender', {})
        is_self = direction == 'out' or sender_obj.get('isSelf', False)
        if is_self:
            context.append({'direction': 'out', 'parsedContent': parsed, 'senderName': '我', 'createTime': ct})
            continue
        if sid not in processed:
            processed.add(sid)
            new_items.append({'sender': sender, 'content': parsed, 'type': kind, 'time': ct, 'sessionId': session_id, 'sessionName': session_name, 'sessionType': session_type})
        context.append({'direction': 'in', 'parsedContent': parsed, 'senderName': get_name(sender), 'createTime': ct})
    return new_items, context


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GUI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class WeChatMonitorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title('微信消息监测 · AI 回复建议（仅供学习，与CipherTalk配合使用）')
        self.root.geometry('1280x760')
        self.root.minsize(900, 600)

        # 内置变量
        self.auto_sync = tk.BooleanVar(value=True)
        self.all_messages = []
        self.session_messages = {}
        self.selected_session = None
        self.countdown_remaining = 0
        self.countdown_timer_id = None
        self.running = True
        self.polling_busy = False
        self.last_unread = {}         # session_id -> (unread_count, sortTimestamp)
        self.push_timer_id = None     # after() 定时器ID

        self._build_ui()
        refresh_names()
        # 延迟加载联系人列表（等 name_cache 填充完毕）
        self.root.after(500, self._update_contact_display)
        self.schedule_push()

    # ── UI 构建 ──

    def _build_ui(self):
        self.root.configure(bg='#f0f0f0')
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('Treeview', rowheight=30, font=('Microsoft YaHei', 10))
        self.style.configure('Treeview.Heading', font=('Microsoft YaHei', 10, 'bold'))

        # ═══ 顶部蓝色标题栏 ═══
        title_bar = tk.Frame(self.root, bg='#2b5797', height=42)
        title_bar.pack(fill=tk.X)
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text='社恐救星，社牛神器',
                 fg='white', bg='#2b5797', font=('Microsoft YaHei', 13, 'bold')).pack(side=tk.LEFT, padx=15)
        self.api_status_icon = tk.Label(title_bar, text='●', fg='#7fba00', bg='#2b5797', font=('Microsoft YaHei', 12))
        self.api_status_icon.pack(side=tk.RIGHT, padx=(0, 2))
        self.api_status_label = tk.Label(title_bar, text='已连接', fg='#aaccff', bg='#2b5797', font=('Microsoft YaHei', 9))
        self.api_status_label.pack(side=tk.RIGHT, padx=15)

        # ═══ 工具栏（同步按钮 + 自动同步设置）═══
        toolbar = tk.Frame(self.root, bg='#e0e0e0', height=42)
        toolbar.pack(fill=tk.X)
        toolbar.pack_propagate(False)

        # --- 同步按钮组 ---
        btn_frame = tk.Frame(toolbar, bg='#e0e0e0')
        btn_frame.pack(side=tk.LEFT, padx=8, pady=4)

        self.btn_contacts = tk.Button(btn_frame, text='☰ 同步联系人', command=self.sync_contacts,
                                       bg='#4CAF50', fg='white', relief=tk.FLAT, padx=10,
                                       font=('Microsoft YaHei', 9, 'bold'))
        self.btn_contacts.pack(side=tk.LEFT, padx=2)

        self.btn_sessions = tk.Button(btn_frame, text='💬 同步会话', command=self.sync_sessions,
                                       bg='#2196F3', fg='white', relief=tk.FLAT, padx=10,
                                       font=('Microsoft YaHei', 9, 'bold'))
        self.btn_sessions.pack(side=tk.LEFT, padx=2)

        self.btn_messages = tk.Button(btn_frame, text='✉ 同步消息', command=self.sync_messages,
                                       bg='#FF9800', fg='white', relief=tk.FLAT, padx=10,
                                       font=('Microsoft YaHei', 9, 'bold'))
        self.btn_messages.pack(side=tk.LEFT, padx=2)

        # 模型设置按钮
        self.btn_settings = tk.Button(btn_frame, text='⚙ 模型设置', command=self.open_settings,
                                       bg='#9C27B0', fg='white', relief=tk.FLAT, padx=10,
                                       font=('Microsoft YaHei', 9, 'bold'))
        self.btn_settings.pack(side=tk.LEFT, padx=2)

        # 分隔线
        sep = tk.Frame(toolbar, bg='#bbb', width=1, height=28)
        sep.pack(side=tk.LEFT, padx=10, pady=6)

        # --- 自动同步设置 ---
        sync_frame = tk.Frame(toolbar, bg='#e0e0e0')
        sync_frame.pack(side=tk.LEFT, padx=5)

        self.sync_check = tk.Checkbutton(sync_frame, text='推送监听', variable=self.auto_sync,
                                          bg='#e0e0e0', font=('Microsoft YaHei', 9, 'bold'),
                                          command=self.on_auto_sync_toggle)
        self.sync_check.pack(side=tk.LEFT)
        tk.Label(sync_frame, text='  间隔:', bg='#e0e0e0', font=('Microsoft YaHei', 9)).pack(side=tk.LEFT)
        self.poll_interval = tk.IntVar(value=llm_config.get('sync_interval', 3))
        self.interval_spin = tk.Spinbox(sync_frame, from_=1, to=60, width=3,
                                         textvariable=self.poll_interval,
                                         font=('Microsoft YaHei', 9),
                                         command=self._on_interval_change)
        self.interval_spin.pack(side=tk.LEFT)
        tk.Label(sync_frame, text='秒', bg='#e0e0e0', font=('Microsoft YaHei', 9)).pack(side=tk.LEFT)

        # --- 复制提示（居中显示） ---
        self.lbl_copy_status = tk.Label(toolbar, text='', bg='#e0e0e0',
                                         font=('Microsoft YaHei', 9), fg='#e67e22')
        self.lbl_copy_status.pack(side=tk.RIGHT, padx=5)

        # --- 右侧统计 ---
        stats_frame = tk.Frame(toolbar, bg='#e0e0e0')
        stats_frame.pack(side=tk.RIGHT, padx=5)

        self.lbl_msg_count = tk.Label(stats_frame, text='消息: 0', bg='#e0e0e0', font=('Microsoft YaHei', 9))
        self.lbl_msg_count.pack(side=tk.LEFT, padx=5)
        self.lbl_session_count = tk.Label(stats_frame, text='会话: 0', bg='#e0e0e0', font=('Microsoft YaHei', 9))
        self.lbl_session_count.pack(side=tk.LEFT, padx=5)
        self.lbl_countdown = tk.Label(stats_frame, text='⏳ 0s', bg='#e0e0e0', font=('Microsoft YaHei', 9), fg='#555')
        self.lbl_countdown.pack(side=tk.LEFT, padx=5)

        # ═══ 上次同步时间条 ═══
        sync_status = tk.Frame(self.root, bg='#f5f5f5', height=24)
        sync_status.pack(fill=tk.X)
        sync_status.pack_propagate(False)
        self.lbl_last_contact = tk.Label(sync_status, text='联系人: --', bg='#f5f5f5',
                                          font=('Microsoft YaHei', 8), fg='#666')
        self.lbl_last_contact.pack(side=tk.LEFT, padx=10)
        self.lbl_last_session = tk.Label(sync_status, text='会话: --', bg='#f5f5f5',
                                          font=('Microsoft YaHei', 8), fg='#666')
        self.lbl_last_session.pack(side=tk.LEFT, padx=20)
        self.lbl_last_message = tk.Label(sync_status, text='消息: --', bg='#f5f5f5',
                                          font=('Microsoft YaHei', 8), fg='#666')
        self.lbl_last_message.pack(side=tk.LEFT, padx=20)

        # ═══ 主区域：左侧会话列表 + 中间消息面板 + 右侧联系人列表 ═══
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        # --- 左侧：会话列表 ---
        left_frame = ttk.Frame(main_paned, width=280)
        main_paned.add(left_frame, weight=0)
        tk.Label(left_frame, text='会话列表', font=('Microsoft YaHei', 10, 'bold'),
                 bg='#e8e8e8').pack(fill=tk.X, padx=2, pady=1)
        self.session_tree = ttk.Treeview(left_frame, columns=('unread',), show='tree', height=20)
        self.session_tree.heading('#0', text='会话')
        self.session_tree.column('#0', width=200)
        self.session_tree.column('unread', width=50, anchor='center')
        self.session_tree.pack(fill=tk.BOTH, expand=True)
        self.session_tree.bind('<<TreeviewSelect>>', self.on_session_select)
        self.session_tree.bind('<Button-3>', self._session_right_click)
        self.session_menu = tk.Menu(self.root, tearoff=False)
        self.categories = ['家人', '朋友', '爱人', '同学', '同事', '其他', '敌人']
        for cat in self.categories:
            self.session_menu.add_command(label=cat, command=lambda c=cat: self._set_session_category(c))

        # --- 中间：消息表格 ---
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)

        # --- 右侧：联系人列表（勾选=重要） ---
        contact_frame = ttk.Frame(main_paned, width=200)
        main_paned.add(contact_frame, weight=0)
        contact_header = tk.Frame(contact_frame, bg='#e8e8e8')
        contact_header.pack(fill=tk.X)
        tk.Label(contact_header, text='联系人（☐=重要）', font=('Microsoft YaHei', 10, 'bold'),
                 bg='#e8e8e8').pack(side=tk.LEFT, padx=4, pady=1)
        self.btn_refresh_contacts = tk.Button(contact_header, text='↻', command=self._refresh_contact_list,
                                               bg='#e8e8e8', relief=tk.FLAT, font=('Microsoft YaHei', 9))
        self.btn_refresh_contacts.pack(side=tk.RIGHT, padx=4)
        # 联系人搜索
        self.contact_search_var = tk.StringVar()
        search_entry = ttk.Entry(contact_frame, textvariable=self.contact_search_var, font=('Microsoft YaHei', 9))
        search_entry.pack(fill=tk.X, padx=2, pady=2)
        search_entry.bind('<KeyRelease>', lambda e: self._update_contact_display())
        self.contact_tree = ttk.Treeview(contact_frame, columns=('check',), show='tree', height=20)
        self.contact_tree.heading('#0', text='联系人')
        self.contact_tree.column('#0', width=140)
        self.contact_tree.column('check', width=40, anchor='center')
        self.contact_tree.pack(fill=tk.BOTH, expand=True)
        self.contact_tree.bind('<ButtonRelease-1>', self.on_contact_toggle)

        # 表头信息
        right_top = tk.Frame(right_frame, bg='#e8e8e8')
        right_top.pack(fill=tk.X)
        self.cur_session_label = tk.Label(right_top, text='所有消息',
                                           font=('Microsoft YaHei', 11, 'bold'), bg='#e8e8e8')
        self.cur_session_label.pack(side=tk.LEFT, padx=8, pady=4)
        self.msg_count_detail = tk.Label(right_top, text='', font=('Microsoft YaHei', 9), bg='#e8e8e8')
        self.msg_count_detail.pack(side=tk.RIGHT, padx=8, pady=4)

        columns = ('time', 'session', 'sender', 'message', 'suggestion')
        self.msg_tree = ttk.Treeview(right_frame, columns=columns, show='headings', height=20)
        self.msg_tree.heading('time', text='时间')
        self.msg_tree.heading('session', text='会话')
        self.msg_tree.heading('sender', text='发送者')
        self.msg_tree.heading('message', text='消息内容')
        self.msg_tree.heading('suggestion', text='★ 回复建议')
        self.msg_tree.column('time', width=75, anchor='center')
        self.msg_tree.column('session', width=110)
        self.msg_tree.column('sender', width=90)
        self.msg_tree.column('message', width=350)
        self.msg_tree.column('suggestion', width=350)

        scrollbar = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=self.msg_tree.yview)
        self.msg_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.msg_tree.pack(fill=tk.BOTH, expand=True)
        self.msg_tree.bind('<ButtonRelease-1>', self.copy_suggestion)

        # ═══ 底部状态栏 ═══
        bottom_frame = tk.Frame(self.root, bg='#ddd', height=26)
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)
        bottom_frame.pack_propagate(False)
        self.bottom_left = tk.Label(bottom_frame, text='就绪', bg='#ddd', font=('Microsoft YaHei', 9))
        self.bottom_left.pack(side=tk.LEFT, padx=10)
        self.bottom_right = tk.Label(bottom_frame, text='', bg='#ddd', font=('Microsoft YaHei', 9))
        self.bottom_right.pack(side=tk.RIGHT, padx=10)

    # ── 同步操作方法 ──

    def sync_contacts(self):
        self.btn_contacts.config(state=tk.DISABLED, text='同步中...')
        self.bottom_left.config(text='🔄 正在同步联系人...')
        def _work():
            ok, info = refresh_names()
            self.root.after(0, lambda: self._done_contacts(ok, info))
        threading.Thread(target=_work, daemon=True).start()

    def _done_contacts(self, ok, info):
        self.lbl_last_contact.config(text=f'联系人: {ts_str(last_contact_sync)} ({len(name_cache)} 个)')
        self.btn_contacts.config(state=tk.NORMAL, text='☰ 同步联系人')
        self.bottom_left.config(text=f'✓ 联系人同步完成: {len(name_cache)} 个')
        self._update_contact_display()

    def sync_sessions(self):
        self.btn_sessions.config(state=tk.DISABLED, text='同步中...')
        self.bottom_left.config(text='🔄 正在同步会话...')
        def _work():
            sr = api_get('/sessions', {'limit': MAX_SESSIONS, 'unreadOnly': False})
            global last_session_sync
            last_session_sync = int(time.time())
            self.root.after(0, lambda: self._done_sessions(sr))
        threading.Thread(target=_work, daemon=True).start()

    def _done_sessions(self, sr):
        if sr.get('success'):
            sessions = sr.get('data', {}).get('sessions', [])
            for s in sessions:
                sid = s.get('username', '')
                sname = s.get('displayName', '') or sid
                if sname != sid:
                    name_cache[sid] = sname
            self.lbl_last_session.config(text=f'会话: {ts_str(last_session_sync)} ({len(sessions)} 个)')
            self.refresh_session_list()
            self._update_contact_display()
            self.bottom_left.config(text=f'✓ 会话同步完成: {len(sessions)} 个活跃会话')
        else:
            self.bottom_left.config(text='✗ 会话同步失败')
        self.btn_sessions.config(state=tk.NORMAL, text='💬 同步会话')

    def sync_messages(self):
        if self.polling_busy:
            self.bottom_left.config(text='⏳ 正在同步中，请稍候...')
            return
        self.polling_busy = True
        self.btn_messages.config(state=tk.DISABLED, text='同步中...')
        self.bottom_left.config(text='🔄 正在同步消息...')
        def _work():
            try:
                sr = api_get('/sessions', {'limit': MAX_SESSIONS, 'unreadOnly': False})
                if sr.get('success'):
                    sessions = sr.get('data', {}).get('sessions', [])
                    all_active = []
                    for s in sessions:
                        sid = s.get('username', '')
                        sname = s.get('displayName', '') or sid
                        stype = s.get('sessionType', 'friend')
                        if sname != sid:
                            name_cache[sid] = sname
                        all_active.append((sid, sname, stype))
                    # 在线程中只拉数据
                    all_new, all_ctx = self._fetch_data(all_active)
                    # 回主线程更新 GUI
                    if all_new:
                        self.root.after(0, lambda: self._apply_suggestions(all_new, all_ctx))
                    self.root.after(0, self.refresh_session_list)
            finally:
                self.root.after(0, self._done_sync_msg)
        threading.Thread(target=_work, daemon=True).start()

    def _done_sync_msg(self):
        self.polling_busy = False
        self.btn_messages.config(state=tk.NORMAL, text='✉ 同步消息')

    def on_auto_sync_toggle(self):
        if self.auto_sync.get():
            self.bottom_left.config(text='▶ 推送监听已开启')
            self.schedule_push()
        else:
            self.bottom_left.config(text='⏸ 推送监听已暂停')
            self.lbl_countdown.config(text='⏳ --')
            if self.push_timer_id:
                self.root.after_cancel(self.push_timer_id)
                self.push_timer_id = None
            if self.countdown_timer_id:
                self.root.after_cancel(self.countdown_timer_id)
                self.countdown_timer_id = None

    def _on_interval_change(self):
        """同步间隔改变时更新调度并保存配置"""
        if self.push_timer_id:
            self.root.after_cancel(self.push_timer_id)
            self.push_timer_id = None
        if self.auto_sync.get():
            self.schedule_push()
        global llm_config
        llm_config['sync_interval'] = self.poll_interval.get()
        save_config(llm_config)

    # ── 联系人勾选（重要标记）──

    def on_contact_toggle(self, event):
        """点击联系人行切换重要标记"""
        sel = self.contact_tree.selection()
        if not sel:
            return
        cid = sel[0]
        if cid in IMPORTANT_CONTACTS:
            IMPORTANT_CONTACTS.discard(cid)
        else:
            IMPORTANT_CONTACTS.add(cid)
        self._update_contact_display()
        # 实时保存到配置
        cfg = dict(llm_config)
        save_config(cfg)

    def _refresh_contact_list(self):
        """刷新联系人列表显示"""
        # 确保 name_cache 有数据
        if not name_cache:
            threading.Thread(target=refresh_names, daemon=True).start()
        self._update_contact_display()

    def _update_contact_display(self):
        """更新联系人树形控件（按搜索关键字过滤）"""
        for row in self.contact_tree.get_children():
            self.contact_tree.delete(row)
        keyword = self.contact_search_var.get().strip().lower()
        all_contacts = sorted(name_cache.items(), key=lambda x: x[1])
        for uid, dname in all_contacts:
            if keyword and keyword not in dname.lower() and keyword not in uid.lower():
                continue
            checked = '☑' if uid in IMPORTANT_CONTACTS else '☐'
            self.contact_tree.insert('', tk.END, iid=uid, text=dname, values=(checked,))

    def schedule_push(self):
        """安排下一次推送检查"""
        if self.running and self.auto_sync.get():
            interval_ms = max(200, self.poll_interval.get() * 1000)
            self.push_timer_id = self.root.after(interval_ms, self.push_check)
            self._start_countdown()

    def push_check(self):
        """推送式监听：快速检查所有会话是否有新消息"""
        if not self.auto_sync.get() or self.polling_busy:
            self.schedule_push()
            return

        self.polling_busy = True
        try:
            sr = api_get('/sessions', {'limit': MAX_SESSIONS, 'unreadOnly': False})
            if not sr.get('success'):
                self.schedule_push()
                return

            sessions = sr.get('data', {}).get('sessions', [])
            if not sessions:
                self.schedule_push()
                return

            # 更新会话缓存
            for s in sessions:
                sid = s.get('username', '')
                sname = s.get('displayName', '') or sid
                if sname != sid:
                    name_cache[sid] = sname

            changed_ids = []
            now_ts = int(time.time())

            for s in sessions:
                sid = s.get('username', '')
                unread = int(s.get('unreadCount', 0))
                ts = int(s.get('sortTimestamp', 0))

                prev = self.last_unread.get(sid)
                # 检测变化：未读数增加 或 有新消息时间戳且之前没记录
                is_new = False
                if prev is None and unread > 0:
                    is_new = True
                elif prev is not None:
                    pu, pt = prev
                    if unread > pu or (unread > 0 and ts > pt):
                        is_new = True

                # 首次记录或消息在1分钟内
                if prev is None or is_new or (now_ts - ts) < 60:
                    self.last_unread[sid] = (unread, ts)
                    if is_new:
                        changed_ids.append((sid, s.get('displayName', '') or sid, s.get('sessionType', 'friend')))

                # 清理超过10分钟无活动的记录
                if prev and (now_ts - ts) > 600:
                    if sid in self.last_unread:
                        del self.last_unread[sid]

            # 有变化时才拉取消息并分析
            if changed_ids:
                all_new, all_ctx = self._fetch_data(changed_ids)
                self._apply_suggestions(all_new, all_ctx)

            active = len(self.last_unread)
            now_str = datetime.now(TZ).strftime('%H:%M:%S')
            self.update_status(
                f'✓ 监听中 | 检测 {len(sessions)} 个会话 | {len(changed_ids)} 个更新',
                f'共 {len(self.all_messages)} 条消息 | {active} 活跃 | {now_str}')
            self._update_stats()

        except Exception as e:
            self.update_status(f'✗ 错误: {e}')
        finally:
            self.polling_busy = False
            self.schedule_push()

    def _fetch_and_process(self, session_list):
        """拉取指定会话的消息并生成建议（仅限 GUI 线程调用）"""
        all_new, all_ctx = self._fetch_data(session_list)
        if all_new is not None:
            self._apply_suggestions(all_new, all_ctx)

    def _fetch_data(self, session_list):
        """纯数据拉取（可在后台线程调用），返回 (all_new, all_ctx)"""
        global last_msg_sync
        all_new = []
        all_ctx = {}
        for sid, sname, stype in session_list:
            items, ctx = analyze_session(sid, sname, stype) or (None, None)
            if items:
                all_new.extend(items)
                all_ctx[sid] = ctx
        last_msg_sync = int(time.time())
        return all_new, all_ctx

    def _apply_suggestions(self, all_new, all_ctx):
        """生成建议并更新 GUI（必须在 GUI 线程调用）"""
        self.lbl_last_message.config(text=f'消息: {ts_str(last_msg_sync)}')
        if not all_new:
            return
        all_new.sort(key=lambda x: x['time'])
        for item in all_new:
            sid = item['sessionId']
            content = item['content']
            kind = item['type']
            ctx = all_ctx.get(sid, [])
            stype = item['sessionType']
            sender = item['sender']
            sender_display = get_name(sender) if stype == 'group' else get_name(sid)
            category = SESSION_CATEGORIES.get(sid, '')
            # 重要联系人：始终使用 LLM 仔细回答
            if sid in IMPORTANT_CONTACTS:
                try:
                    llm_r = llm_suggest_important(content, ctx, sender_display, category)
                    suggestion = llm_r if llm_r else fallback_reply(content, kind)
                except Exception:
                    suggestion = fallback_reply(content, kind)
            else:
                suggestion = fallback_reply(content, kind)
                if kind == 'text' and content.strip():
                    try:
                        llm_r = llm_suggest(content, ctx, sender_display, category)
                        if llm_r:
                            suggestion = llm_r
                    except Exception:
                        pass
            self.add_message_to_gui(item, suggestion)
        self.refresh_message_display()
        self.refresh_session_list()

    # ── GUI 更新 ──

    def _session_right_click(self, event):
        """右键会话弹出分类菜单"""
        iid = self.session_tree.identify_row(event.y)
        if iid:
            self.session_tree.selection_set(iid)
            self._context_session_id = iid
            # 更新菜单项标记当前分类
            cur_cat = SESSION_CATEGORIES.get(iid, '')
            for i, cat in enumerate(self.categories):
                label = f'✓ {cat}' if cat == cur_cat else f'  {cat}'
                self.session_menu.entryconfig(i, label=label)
            self.session_menu.post(event.x_root, event.y_root)

    def _set_session_category(self, category):
        sid = self._context_session_id
        if not sid:
            return
        if category == '其他':
            SESSION_CATEGORIES.pop(sid, None)
        else:
            SESSION_CATEGORIES[sid] = category
        # 持久化
        global llm_config
        llm_config['session_categories'] = dict(SESSION_CATEGORIES)
        save_config(llm_config)

    def on_session_select(self, event):
        sel = self.session_tree.selection()
        if sel:
            self.selected_session = sel[0]
            self.cur_session_label.config(text=get_name(sel[0]))
        else:
            self.selected_session = None
            self.cur_session_label.config(text='所有消息')
        self.refresh_message_display()

    def add_message_to_gui(self, item, suggestion):
        sid = item['sessionId']
        sname = item['sessionName']
        stype = item['sessionType']
        sender = item['sender']
        content = item['content']
        t = ts_str(item['time'])
        sender_display = get_name(sender) if stype == 'group' else get_name(sid)
        msg_entry = {
            'time': t, 'timestamp': item['time'], 'session_name': sname,
            'session_id': sid, 'sender': sender_display, 'content': content,
            'type': item['type'], 'suggestion': suggestion,
        }
        idx = len(self.all_messages)
        self.all_messages.append(msg_entry)
        if sid not in self.session_messages:
            self.session_messages[sid] = []
        self.session_messages[sid].append(idx)

    def refresh_message_display(self):
        for row in self.msg_tree.get_children():
            self.msg_tree.delete(row)
        if self.selected_session:
            indices = self.session_messages.get(self.selected_session, [])
            display_msgs = [self.all_messages[i] for i in indices]
        else:
            display_msgs = self.all_messages
        # 按时间降序（最新消息在最上面）
        display_msgs = list(reversed(display_msgs))
        for m in display_msgs:
            self.msg_tree.insert('', tk.END, values=(
                m['time'], m['session_name'], m['sender'], m['content'], m['suggestion'],
            ))
        self.msg_count_detail.config(text=f'{len(display_msgs)} 条消息' if display_msgs else '')

    def refresh_session_list(self):
        # 记住当前选中的会话
        sel = self.session_tree.selection()
        prev_selection = sel[0] if sel else (self.selected_session or '')

        self.session_tree.delete(*self.session_tree.get_children())
        seen = set()
        ordered = []
        for m in reversed(self.all_messages):
            if m['session_id'] not in seen:
                seen.add(m['session_id'])
                ordered.append(m['session_id'])
        sr = api_get('/sessions', {'limit': MAX_SESSIONS, 'unreadOnly': False})
        if sr.get('success'):
            sessions = sr.get('data', {}).get('sessions', [])
            info = {}
            for s in sessions:
                sid = s.get('username', '')
                sname = s.get('displayName', '') or sid
                unread = s.get('unreadCount', '0')
                info[sid] = (sname, unread)
                if sid not in seen:
                    ordered.append(sid)
                seen.add(sid)
            for sid in ordered:
                if sid in info:
                    sname, unread = info[sid]
                else:
                    sname, unread = get_name(sid), '0'
                badge = f'  [{unread}]' if unread and int(unread) > 0 else ''
                cat_tag = SESSION_CATEGORIES.get(sid, '')
                cat_display = f' [{cat_tag}]' if cat_tag else ''
                self.session_tree.insert('', tk.END, iid=sid, text=f'{sname}{badge}{cat_display}', values=('',))

        # 恢复之前选中的会话，不自动切换
        if prev_selection and self.session_tree.exists(prev_selection):
            self.session_tree.selection_set(prev_selection)

    def _update_stats(self):
        self.lbl_msg_count.config(text=f'消息: {len(self.all_messages)}')
        self.lbl_session_count.config(text=f'会话: {len(self.session_messages)}')

    def _start_countdown(self):
        """每次推送检查后启动倒计时"""
        if self.countdown_timer_id:
            self.root.after_cancel(self.countdown_timer_id)
            self.countdown_timer_id = None
        self.countdown_remaining = self.poll_interval.get()
        self._tick_countdown()

    def _tick_countdown(self):
        if not self.auto_sync.get() or not self.running:
            self.lbl_countdown.config(text='⏳ --')
            return
        self.lbl_countdown.config(text=f'⏳ {self.countdown_remaining}s')
        if self.countdown_remaining > 0:
            self.countdown_remaining -= 1
            self.countdown_timer_id = self.root.after(1000, self._tick_countdown)

    def copy_suggestion(self, event):
        sel = self.msg_tree.selection()
        if sel:
            values = self.msg_tree.item(sel[0], 'values')
            if len(values) >= 5 and values[4]:
                self.root.clipboard_clear()
                self.root.clipboard_append(values[4])
                # 在顶部工具栏显示复制成功
                self.lbl_copy_status.config(text=f'✓ 已复制: {values[4][:25]}...')
                # 2秒后自动清除
                self.root.after(2000, lambda: self.lbl_copy_status.config(text=''))

    def update_status(self, text, detail=''):
        self.bottom_left.config(text=text)
        if detail:
            self.bottom_right.config(text=detail)

    def open_settings(self):
        SettingsDialog(self.root, self)

    def stop(self):
        self.running = False


class SettingsDialog:
    """大模型参数配置对话框"""

    def __init__(self, parent, app):
        self.app = app
        self.dialog = tk.Toplevel(parent)
        self.dialog.title('大模型参数配置')
        self.dialog.geometry('520x400')
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        frame = ttk.Frame(self.dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        # 字段
        row = 0
        fields = [
            ('API 接口地址:', 'endpoint', 60),
            ('模型名称:', 'model', 30),
            ('API Key:', 'api_key', 50),
            ('Max Tokens:', 'max_tokens', 10),
            ('Temperature:', 'temperature', 10),
        ]

        self.entries = {}
        for label, key, width in fields:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=6)
            if key == 'api_key':
                ent = ttk.Entry(frame, width=width, show='*')
            else:
                ent = ttk.Entry(frame, width=width)
            ent.insert(0, str(llm_config.get(key, '')))
            ent.grid(row=row, column=1, sticky='ew', padx=10)
            self.entries[key] = ent
            row += 1

        frame.columnconfigure(1, weight=1)

        # 状态/结果标签
        self.result_var = tk.StringVar()
        result_label = ttk.Label(frame, textvariable=self.result_var, font=('Microsoft YaHei', 9))
        result_label.grid(row=row, column=0, columnspan=2, pady=8)
        row += 1

        # 按钮行
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)

        ttk.Button(btn_frame, text='🧪 模型测试', command=self.test_model).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='💾 保存配置', command=self.save_cfg).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='取消', command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)

    def _get_values(self):
        vals = {}
        for k, ent in self.entries.items():
            v = ent.get().strip()
            if k == 'max_tokens':
                v = int(v) if v.isdigit() else 150
            elif k == 'temperature':
                v = float(v) if v.replace('.', '', 1).isdigit() else 0.7
            vals[k] = v
        return vals

    def test_model(self):
        global llm_config
        vals = self._get_values()
        self.result_var.set('⏳ 正在测试模型连接...')
        self.dialog.update()

        def _work():
            try:
                prompt = '请回复"连接成功"四个字（不要其他内容）'
                payload = json.dumps({
                    'model': vals['model'],
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': 30,
                    'temperature': 0.5,
                }).encode('utf-8')
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': vals['api_key'],
                    'anthropic-version': '2023-06-01',
                }
                req = Request(vals['endpoint'], data=payload, headers=headers, method='POST')
                with urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode('utf-8'))
                    for block in data.get('content', []):
                        if block.get('type') == 'text':
                            text = block['text'].strip()
                            self.dialog.after(0, lambda t=text: self.result_var.set(f'✅ 测试成功: {t[:50]}'))
                            return
                    choice = data.get('choices', [{}])[0]
                    msg = choice.get('message', {}).get('content', '') or choice.get('text', '')
                    self.dialog.after(0, lambda m=msg: self.result_var.set(f'✅ 测试成功: {m.strip()[:50]}'))
            except Exception as e:
                self.dialog.after(0, lambda: self.result_var.set(f'❌ 测试失败: {e}'))

        threading.Thread(target=_work, daemon=True).start()

    def save_cfg(self):
        global llm_config
        vals = self._get_values()
        llm_config = vals
        save_config(vals)
        self.result_var.set('✅ 配置已保存并生效')
        self.dialog.after(1500, self.dialog.destroy)


def main():
    root = tk.Tk()
    app = WeChatMonitorGUI(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()

if __name__ == '__main__':
    main()
