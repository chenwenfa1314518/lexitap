"""LexiTap: a PDF reader with sentence translation.

Drag to select words (across lines and pages). On release the selection grows to whole
sentences, which are translated into Chinese in the right-hand panel. Translation uses any
OpenAI-compatible chat API: DeepSeek by default, or Qwen, Kimi, GLM, OpenAI, Ollama, ...

Requires: pip install pymupdf
Run:      python lexitap.py [file.pdf ...]
"""
import bisect, json, os, queue, re, socket, sys, threading, time, unicodedata, zlib
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk, font as tkfont

try:
    import pymupdf
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror('缺少依赖', '请先在命令行运行：\npip install -U pymupdf')
    sys.exit(1)

__version__ = '1.2.0'
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
RES = getattr(sys, '_MEIPASS', HERE)  # bundled files (the icon) in a packaged app


def data_dir():
    """Settings stay next to the program if a config.json is already there (portable use), else
    they go to the user's config folder, so installed copies work too. LEXITAP_HOME overrides."""
    d = os.environ.get('LEXITAP_HOME') or (HERE if os.path.exists(os.path.join(HERE, 'config.json')) else None)
    if not d:
        base = os.environ.get('APPDATA') or os.path.expanduser(
            '~/Library/Application Support' if sys.platform == 'darwin' else os.environ.get('XDG_CONFIG_HOME', '~/.config'))
        d = os.path.join(base, 'lexitap')
    os.makedirs(d, exist_ok=True)
    return d


DATA = data_dir()
CFG_FILE, CACHE_FILE = os.path.join(DATA, 'config.json'), os.path.join(DATA, 'translations.json')
PORT = 20000 + zlib.crc32(os.path.normcase(DATA).encode()) % 40000  # one window per settings folder
INBOX = queue.Queue()  # lists of files for the window to open, handed over by later launches of the app
DIRECT = {}            # API host -> can it be reached without the system proxy? (see send)
DEFAULTS = {
    'base_url': 'https://api.deepseek.com',
    'api_key': '',
    'model': 'deepseek-flash',
    'prompt': '你是专业翻译。把用户发来的文本翻译成简体中文，只输出译文，不要任何解释。'
              '文本摘自 PDF，可能夹杂断行连字符、页眉页脚、公式碎片，请忽略这些杂质；人名、引用编号、公式保持原样。',
    'glossary': '',  # one term per line: "moiré superlattice = 莫尔超晶格"
    'extra': {},  # extra request-body fields, e.g. {"temperature": 1.3}
}
API_URLS = ['https://api.deepseek.com', 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'https://api.moonshot.cn/v1', 'https://open.bigmodel.cn/api/paas/v4',
            'https://api.siliconflow.cn/v1', 'https://api.openai.com/v1', 'http://localhost:11434/v1']

GAP = 12              # px between pages
TILE = 1536, 256      # px: pages are drawn in strips of this size, only around the view
MARGIN = 0.08         # text blocks lying entirely in an outer 8% strip of a page: headers, footers, stamps
SIZE_TOL = 0.07       # blocks whose main font sizes differ by more than 7% are different kinds of text
# sentence end: full stop, optional closing quotes/brackets, optional citation glued on ("stacking.18")
END = re.compile(r'([.!?。！？])["\'”’)\]]*(\[?\d+(?:[,–−-]\d+)*\]?)?$')
CAPTION = re.compile(r'(Fig(ure)?s?|FIG(URE)?S?|Table|TABLE|Scheme|SCHEME|Chart|CHART)\.?\s*(\d+|[IVX]+)\s*[.:|]')
# footnote-like starts of blocks in the lower part of a page (author notes, e-mail, DOI, dates)
NOTE = re.compile(r'[*†‡§¶]|E-?mail|Corresponding|https?://|doi|DOI|Received|Accepted|Published|Available online|©|Copyright')
ABBR = {'e.g.', 'i.e.', 'al.', 'fig.', 'figs.', 'eq.', 'eqs.', 'tab.', 'sec.', 'ref.', 'refs.', 'no.',
        'vol.', 'pp.', 'vs.', 'cf.', 'dr.', 'mr.', 'mrs.', 'ms.', 'prof.', 'st.', 'approx.', 'resp.'}
WORD_FLAGS = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_PRESERVE_LIGATURES  # turn "ﬁ" into "fi"
# 1x1 translucent blue PNG, stretched over selected lines
SEL_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgqLgeAAADawGgJ3DJNAAAAABJRU5ErkJggg=='
NO_START = set('，。、；：？！）】》」』”’…,.;:?!)]}%')  # a line may not begin with these
NO_END = set('（【《「『“‘([{')                       # ...or end with these
MATH = re.compile('[=∑∫∏∂∇±×÷≈≠≤≥∞√⟨⟩∥∈∝]')  # symbols that mark a short block as a formula
# what each tab remembers about its document (the translation panel and its cache are shared)
TAB_STATE = ('doc', 'path', 'zoom', 'fit', 'rects', 'wcache', 'binfo', 'hcache', 'lcache', 'sel', 'anchor', 'cls', 'back')
TAB_BAR, TAB_OFF, TAB_HOVER, TAB_ON, TAB_LINE = '#d3d6da', '#e4e6e9', '#eef0f2', '#ffffff', '#0a64c8'  # tab colours
RIGHT = '<Button-2>' if sys.platform == 'darwin' else '<Button-3>'  # the right mouse button
SAVE_PDF = '另存为 PDF'  # the last entry in the print window's list of printers


def blank_tab():
    return dict(doc=None, path='', zoom=1.0, fit=True, rects=[], wcache={}, binfo={}, hcache={}, lcache={},
                sel=None, anchor=None, cls=None, back=[], view=None)


HINT = ['在左侧拖动选择文字（可跨行、跨页），松开后自动识别整句并翻译到这里。', '双击：翻译所在的句子',
        '→ / ←：翻译下一句 / 上一句', 'Shift+单击：把选区延伸到此处', 'Ctrl+F：搜索　Ctrl+C：复制选中的文字',
        '鼠标停在引用编号或图号上可预览，单击跳转，Alt+← 返回', 'Ctrl+T：打开或关闭翻译面板',
        '右键：复制、翻译、搜索选中的文字　Ctrl+P：打印']


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(path + '.tmp', path)  # in one step: a crash while writing can't leave half a file (your API key)


def load_cfg():
    return dict(DEFAULTS, **load_json(CFG_FILE, {}))


def save_cfg(cfg):
    save_json(CFG_FILE, cfg)


def send(req):
    """Open the request to the API. If the system has a proxy set, try without it first: a service
    in the same country then answers several times sooner. If the host is out of reach that way,
    or refuses where the request comes from (HTTP 400, 403, 451), go through the proxy as usual."""
    import urllib.request
    host, err = req.host, None
    if DIRECT.get(host, True) and urllib.request.getproxies().get(req.type) and not urllib.request.proxy_bypass(host):
        try:
            if host not in DIRECT:  # within reach? Asked once, with a short wait: a blocked host is slow to fail
                u = urllib.parse.urlsplit(req.full_url)
                socket.create_connection((u.hostname, u.port or (443 if req.type == 'https' else 80)), timeout=2).close()
                DIRECT[host] = True
            return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code not in (400, 403, 451):
                raise  # an answer that another route would not change: wrong key, no credit, too many requests
            err = e
    try:
        resp = urllib.request.build_opener().open(req, timeout=60)  # built now: the proxy settings may have changed
    except Exception as e:  # of two failures, report the server's answer rather than a lost connection
        raise err if isinstance(err, urllib.error.HTTPError) and not isinstance(e, urllib.error.HTTPError) else e
    if err and not isinstance(err, urllib.error.HTTPError):
        DIRECT[host] = False  # only the proxy leads there: don't try again
    return resp


def llm_stream(cfg, text):
    """Call an OpenAI-compatible /chat/completions endpoint and yield the reply piece by piece."""
    import urllib.request  # loaded on first use: at every start of the app it would cost 0.1 s
    url = cfg['base_url'].rstrip('/')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'
    prompt = cfg.get('prompt') or ''
    if cfg.get('title'):  # context, so the model picks the field's usual terms
        prompt += f'\n原文出自论文《{cfg["title"]}》，请按该领域的规范译名翻译术语。'
    terms = [g.strip() for g in (cfg.get('glossary') or '').splitlines()
             if (src := re.split(r'[=＝:：→\t]', g, 1)[0].strip().lower()) and src in text.lower()]
    if terms:  # only the glossary entries that occur in this text
        prompt += '\n以下术语必须这样翻译：\n' + '\n'.join(terms)
    if '\n' in text:  # the panel pairs each source paragraph with a translated one
        prompt += '\n原文分为多段，段与段之间空一行。译文保持同样的分段和顺序，段与段之间也空一行。'
    prompt = prompt.strip()
    msgs = [{'role': 'system', 'content': prompt}] if prompt else []
    body = {'model': cfg['model'], 'stream': True, 'messages': msgs + [{'role': 'user', 'content': text}]}
    if 'deepseek.com' in url:
        body['thinking'] = {'type': 'disabled'}  # on by default; translation is faster and cheaper without it
    body.update(cfg.get('extra') or {})
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg['api_key'],
        'User-Agent': 'lexitap/' + __version__})
    try:
        resp = send(req)
    except urllib.error.HTTPError as e:
        msg = e.read().decode('utf-8', 'replace')
        try:
            msg = json.loads(msg)['error']['message']
        except Exception:
            pass
        raise RuntimeError(f'HTTP {e.code}：{msg}') from None
    with resp:
        streamed, raw = False, b''
        for line in resp:
            if not line.startswith(b'data:'):
                raw += b'' if streamed else line
                continue
            streamed, data = True, line[5:].strip()
            if data == b'[DONE]':
                break
            if not data:
                continue
            chunk = json.loads(data)
            if 'error' in chunk:
                raise RuntimeError(json.dumps(chunk['error'], ensure_ascii=False))
            piece = ((chunk.get('choices') or [{}])[0].get('delta') or {}).get('content')
            if piece:
                yield piece
    if not streamed and raw.strip():  # server ignored "stream" and sent one JSON body
        yield json.loads(raw)['choices'][0]['message']['content']


def printers():
    """Names of the system's printers, the default one first."""
    try:
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes as wt

            class Info(ctypes.Structure):  # PRINTER_INFO_4
                _fields_ = [('name', wt.LPWSTR), ('server', wt.LPWSTR), ('attributes', wt.DWORD)]
            spool, n, count, name = ctypes.WinDLL('winspool.drv'), wt.DWORD(), wt.DWORD(), ctypes.create_unicode_buffer(256)
            spool.EnumPrintersW(6, None, 4, None, 0, ctypes.byref(n), ctypes.byref(count))  # 6: local and connected ones
            buf = ctypes.create_string_buffer(n.value)
            spool.EnumPrintersW(6, None, 4, buf, n, ctypes.byref(n), ctypes.byref(count))
            names = [i.name for i in (Info * count.value).from_buffer(buf)]
            spool.GetDefaultPrinterW(name, ctypes.byref(wt.DWORD(256)))
            default = name.value
        else:
            import subprocess
            ask = lambda opt: subprocess.run(['lpstat', opt], capture_output=True, text=True, timeout=5).stdout
            names, default = ask('-e').split(), ask('-d').strip().rpartition(': ')[2]
    except Exception:  # no printing system
        return []
    return sorted(names, key=lambda s: s != default)


def spool(doc, printer, pages, gray, title, out=None):
    """Windows: print the pages, each as large as fits the paper (turned if the paper lies the other way),
    drawn at up to 600 dpi in strips of 4 million dots, which keeps the memory small. Yields the count after
    each page; closing the generator before the end cancels the job. out: a file that takes the printer's
    output (for the tests)."""
    import ctypes, struct
    from ctypes import wintypes as wt

    class DocInfo(ctypes.Structure):
        _fields_ = [('size', ctypes.c_int), ('name', wt.LPCWSTR), ('out', wt.LPCWSTR), ('type', wt.LPCWSTR), ('flags', wt.DWORD)]
    gdi = ctypes.WinDLL('gdi32')
    gdi.CreateDCW.restype = ctypes.c_void_p
    dc = ctypes.c_void_p(gdi.CreateDCW('WINSPOOL', printer, None, None))
    end = gdi.AbortDoc
    try:
        if gdi.StartDocW(dc, ctypes.byref(DocInfo(ctypes.sizeof(DocInfo), title, out))) <= 0:
            raise OSError('打印机没有响应')
        W, H, dpi = (gdi.GetDeviceCaps(dc, i) for i in (8, 10, 88))  # the printable area in dots, dots per inch
        k = max(dpi / 600, 1)  # dots per drawn pixel
        for n, p in enumerate(pages, 1):
            r = doc[p].rect
            turn = (r.width > r.height) != (W > H)
            z = min(W / (r.height if turn else r.width), H / (r.width if turn else r.height)) / k
            m = pymupdf.Matrix(z, z).prerotate(90 * turn)
            box, dl, rows = r * m, doc[p].get_displaylist(), max(int(4e6 / (r.width * z)), 1)
            ox, oy = (W - box.width * k) / 2 - box.x0 * k, (H - box.height * k) / 2 - box.y0 * k  # centred
            gdi.StartPage(dc)
            for y in range(0, int(r.height * z - 1e-6) + 1, rows):
                pix = dl.get_pixmap(matrix=m, colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB, alpha=False,
                                    clip=pymupdf.Rect(0, y / z, r.width, (y + rows) / z))
                if gray:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                bits = bytearray(pymupdf.Pixmap(pix, 1).samples)  # 4 bytes per dot: rows need no padding
                bits[0::4], bits[2::4] = bits[2::4], bits[0::4]   # Windows wants blue first
                x0, y0, x1, y1 = (round(o + v * k) for o, v in ((ox, pix.x), (oy, pix.y), (ox, pix.x + pix.width),
                                                                (oy, pix.y + pix.height)))
                if not gdi.StretchDIBits(dc, x0, y0, x1 - x0, y1 - y0, 0, 0, pix.width, pix.height,
                                         (ctypes.c_char * len(bits)).from_buffer(bits),
                                         struct.pack('<IiiHHI20x', 40, pix.width, -pix.height, 1, 32, 0), 0, 0xCC0020):
                    raise OSError('打印机不接受页面')
            gdi.EndPage(dc)
            if n == len(pages):
                end = gdi.EndDoc
            yield n
    finally:
        end(dc)
        gdi.DeleteDC(dc)


class Reader:
    def __init__(self, root, paths=(), restore=False):
        self.root, self.cfg = root, load_cfg()
        self.doc, self.zoom, self.fit = None, 1.0, True
        self.rects, self.tops, self.lefts, self.total_h = [], [], [], 1
        self.tiles, self.dls, self.vis = {}, {}, None  # drawn tiles, pages' contents, pages in view (see render)
        self.wcache, self.binfo, self.hcache = {}, {}, {}  # per page: words, block facts, headings
        self.sel = self.anchor = self.drag = self.press = None  # positions are (page, word index)
        self.cls = None    # text class of the word the selection started on (see words())
        self.lcache, self.back = {}, []    # per page: links; places to go back to after following a link
        self.sel_imgs, self.jobs, self.q = [], 0, queue.Queue()
        self.cache = load_json(CACHE_FILE, {})  # finished translations, kept between runs
        self.fq, self.fhits, self.fcur = None, {}, None  # search: text, matches per page, current match
        self.hover = self.tip = self._tipjob = self.press_link = None  # link under the mouse, its preview
        self.entries = {}  # job id -> [source paragraphs, reply, failed, done, (file, selection, class)]
        self.spacers, self.widths = {}, {}  # spacer images by width; text widths by (font, text)
        self._render = self._layout = self._auto = self._relayout = self.sash = None
        self._pw = self._w = 0             # widths the panel / the pages were last laid out for
        self.hl = tk.PhotoImage(data=SEL_PNG)
        self.dpi = root.winfo_fpixels('1i')
        self.tabs, self.cur, self.path, self._tabw = [], None, '', 0  # open documents, index of the shown one
        self.build_ui()
        self.poll()
        root.wait_visibility(self.canvas)  # until the window has its real size: the pages are laid out only once
        self.canvas.bind('<Configure>', self.on_resize)  # (resizing counts from here on)
        try:
            if restore:
                self.restore()
            for path in [paths] if isinstance(paths, str) else paths:
                self.open(path, False)
        except Exception as e:  # e.g. a damaged file in the saved session: start without it
            messagebox.showerror('无法打开', str(e))
        self.show_tab()

    # ---------- UI ----------
    def build_ui(self):
        root = self.root
        root.title('LexiTap')
        bar = ttk.Frame(root, padding=(6, 4))
        bar.pack(fill='x')
        self.tabfont = tkfont.Font(family='Microsoft YaHei UI', size=9)
        self.tabh = self.tabfont.metrics('linespace') + 12
        self.tabbar = tk.Frame(root, bg=TAB_BAR, height=self.tabh + 5)  # browser-style tab strip, below the toolbar
        self.tabbar.pack_propagate(False)
        self.tabbar.pack(fill='x')
        self.tabbar.bind('<Configure>', self.on_tabbar_resize)
        ttk.Button(bar, text='打开', command=self.ask_open).pack(side='left', padx=2)
        ttk.Button(bar, text='打印', command=self.print_dialog).pack(side='left', padx=2)
        self.toc = ttk.Menubutton(bar, text='目录', state='disabled')
        self.toc.pack(side='left', padx=2)
        ttk.Button(bar, text='－', width=3, command=lambda: self.set_zoom(self.zoom / 1.2)).pack(side='left', padx=(12, 0))
        self.zlabel = ttk.Label(bar, width=6, anchor='center')
        self.zlabel.pack(side='left')
        ttk.Button(bar, text='＋', width=3, command=lambda: self.set_zoom(self.zoom * 1.2)).pack(side='left')
        ttk.Button(bar, text='适应宽度', command=lambda: self.set_zoom(None)).pack(side='left', padx=2)
        self.pvar = tk.StringVar()
        self.pentry = ttk.Entry(bar, textvariable=self.pvar, width=5, justify='center')
        self.pentry.pack(side='left', padx=(12, 2))
        self.pentry.bind('<Return>', self.on_page_entry)
        self.plabel = ttk.Label(bar, text='/ 0')
        self.plabel.pack(side='left')
        self.whole = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text='自动扩展为整句', variable=self.whole).pack(side='left', padx=12)
        ttk.Label(bar, text='搜索').pack(side='left', padx=(6, 2))
        self.fvar = tk.StringVar()
        fe = self.fentry = ttk.Entry(bar, textvariable=self.fvar, width=18)
        fe.pack(side='left')
        self.fstat = ttk.Label(bar, width=9, foreground='#777')
        self.fstat.pack(side='left', padx=4)
        fe.bind('<Return>', lambda e: self.find(1))
        fe.bind('<Shift-Return>', lambda e: self.find(-1))
        fe.bind('<Escape>', self.end_find)
        ttk.Button(bar, text='设置', command=self.settings).pack(side='right', padx=2)
        ttk.Button(bar, text='清空译文', command=self.clear).pack(side='right', padx=2)
        self.panel = tk.BooleanVar(value=False)  # closed at the start: the PDF has the whole window
        ttk.Checkbutton(bar, text='翻译面板', variable=self.panel, command=self.toggle_panel).pack(side='right', padx=8)

        pw = self.pw = ttk.PanedWindow(root, orient='horizontal')
        pw.pack(fill='both', expand=True)
        left, right = ttk.Frame(pw, width=600, height=600), ttk.Frame(pw, width=400, height=600)
        self.right = right
        for pane in left, right:  # with these sizes and the weights 3 : 2, the panel opens at 40% of the window
            pane.pack_propagate(False)
        pw.add(left, weight=3)

        c = self.canvas = tk.Canvas(left, bg='#e6e6e6', highlightthickness=0)
        ys = ttk.Scrollbar(left, command=lambda *a: (self.schedule_render(), c.yview(*a)))
        xs = ttk.Scrollbar(left, orient='horizontal', command=lambda *a: (self.schedule_render(), c.xview(*a)))
        c.configure(yscrollcommand=lambda *a: (ys.set(*a), self.schedule_render()),
                    xscrollcommand=lambda *a: (xs.set(*a), self.schedule_render()))
        ys.pack(side='right', fill='y')
        xs.pack(side='bottom', fill='x')
        c.pack(fill='both', expand=True)
        for seq, fn in [('<ButtonPress-1>', self.on_press), ('<B1-Motion>', self.on_motion),
                        ('<ButtonRelease-1>', self.on_release), ('<Double-Button-1>', self.on_double),
                        ('<MouseWheel>', self.on_wheel),
                        ('<Motion>', self.on_hover), ('<Control-c>', self.copy),
                        ('<Prior>', lambda e: self.scroll_px(-c.winfo_height() * .9)),
                        ('<Next>', lambda e: self.scroll_px(c.winfo_height() * .9)),
                        ('<Up>', lambda e: self.scroll_px(-60)), ('<Down>', lambda e: self.scroll_px(60)),
                        ('<Home>', lambda e: self.moveto(0)), ('<End>', lambda e: self.moveto(self.total_h)),
                        ('<Right>', lambda e: self.next_sentence(1)), ('<Left>', lambda e: self.next_sentence(-1)),
                        ('<Alt-Left>', lambda e: self.go_back()), ('<Leave>', lambda e: self.set_hover(None)),
                        ('<Control-Prior>', lambda e: self.next_tab(-1)), ('<Control-Next>', lambda e: self.next_tab(1))]:
            c.bind(seq, fn)
        m = self.menu = tk.Menu(root, tearoff=0)  # right-click on the pages: the first four entries need a selection
        for label, key, fn in [('复制', 'Ctrl+C', self.copy), ('翻译', '', self.translate_sel), ('在文中搜索', '', self.find_sel),
                               ('在网上搜索', '', self.web_search), ('打印…', 'Ctrl+P', self.print_dialog)]:
            m.add_command(label=label, accelerator=key, command=fn)
        m.insert_separator(4)
        c.bind(RIGHT, self.on_menu)
        root.bind('<Control-p>', self.print_dialog)
        root.bind('<Control-f>', lambda e: (self.fentry.focus_set(), self.fentry.select_range(0, 'end'), 'break')[2])
        root.bind('<F3>', lambda e: self.find(1))
        root.bind('<Shift-F3>', lambda e: self.find(-1))
        root.bind('<Control-o>', lambda e: self.ask_open())
        root.bind('<Control-w>', lambda e: self.close_tab(self.cur) if self.tabs else None)
        root.bind('<Control-Tab>', lambda e: self.next_tab(-1 if e.state & 1 else 1))  # Ctrl+Shift+Tab: back
        root.bind('<Control-Prior>', lambda e: self.next_tab(-1))
        root.bind('<Control-Next>', lambda e: self.next_tab(1))
        root.bind('<Control-t>', lambda e: (self.panel.set(not self.panel.get()), self.toggle_panel()))

        # Translation panel: read-only Text with no wrapping of its own; para() lays out justified lines
        self.fonts = {'src': tkfont.Font(family='Segoe UI', size=10),
                      'dst': tkfont.Font(family='Microsoft YaHei UI', size=11)}
        t = self.out = tk.Text(right, wrap='none', width=40, relief='flat', padx=14, pady=10, spacing1=2,
                               spacing3=2, font=self.fonts['dst'], state='disabled')
        ts = ttk.Scrollbar(right, command=t.yview)
        t.configure(yscrollcommand=ts.set)
        ts.pack(side='right', fill='y')
        t.pack(fill='both', expand=True)
        t.tag_configure('src', foreground='#8a8a8a', font=self.fonts['src'])
        t.tag_configure('dst', spacing1=3, spacing3=3)
        t.tag_configure('err', foreground='#c0392b')
        t.tag_configure('end_dst', spacing3=12)  # space after each translated paragraph
        t.tag_configure('end_err', spacing3=12)
        t.tag_configure('link', foreground='#2a6fb0', font=self.fonts['src'], spacing1=10)  # "file · page" line
        t.tag_bind('link', '<Button-1>', self.on_entry_link)
        t.tag_bind('link', '<Enter>', lambda e: t.config(cursor='hand2'))
        t.tag_bind('link', '<Leave>', lambda e: t.config(cursor='xterm'))
        t.bind('<1>', lambda e: t.focus_set())  # a disabled Text doesn't take focus by itself
        t.bind('<<Copy>>', self.copy_panel)
        t.bind('<Configure>', self.on_panel_resize)
        pm = tk.Menu(root, tearoff=0)  # right-click on the translations
        pm.add_command(label='复制', accelerator='Ctrl+C', command=self.copy_panel)
        pm.add_command(label='清空译文', command=self.clear)
        t.bind(RIGHT, lambda e: (pm.entryconfigure(0, state='normal' if t.tag_ranges('sel') else 'disabled'),
                                 pm.tk_popup(e.x_root, e.y_root)))

    def settings(self):
        w = tk.Toplevel(self.root)
        w.title('翻译接口设置')
        w.transient(self.root)
        w.resizable(False, False)
        w.geometry(f'+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 80}')
        vs = {}
        for r, (k, label) in enumerate([('base_url', 'API 地址'), ('api_key', 'API Key'),
                                         ('model', '模型'), ('prompt', '提示词')]):
            ttk.Label(w, text=label).grid(row=r, column=0, sticky='e', padx=(12, 6), pady=6)
            vs[k] = tk.StringVar(w, self.cfg.get(k, ''))
            box = ttk.Combobox(w, values=API_URLS) if k == 'base_url' else ttk.Entry(w, show='*' if k == 'api_key' else '')
            box.configure(textvariable=vs[k], width=70)
            box.grid(row=r, column=1, padx=(0, 12), pady=6, sticky='ew')
        ttk.Label(w, text='术语表').grid(row=4, column=0, sticky='ne', padx=(12, 6), pady=6)
        g = tk.Text(w, height=6, width=70, font=self.fonts['src'], relief='solid', bd=1)
        g.insert('1.0', self.cfg.get('glossary', ''))
        g.grid(row=4, column=1, padx=(0, 12), pady=6, sticky='ew')
        g.bind('<Return>', lambda e: (g.insert('insert', '\n'), 'break')[1])  # Enter adds a line here, doesn't save
        ttk.Label(w, foreground='#777', text='术语表每行一条，如 moiré superlattice = 莫尔超晶格；翻译时统一用这些译名，并参考论文标题。\n'
                                             '支持任意 OpenAI 兼容接口：DeepSeek、通义千问、Kimi、智谱、硅基流动、OpenAI、'
                                             '本地 Ollama 等。\nDeepSeek 推荐模型 deepseek-flash（便宜、快）。').grid(row=5, column=1, sticky='w')

        def save(_=None):
            self.cfg.update({k: v.get().strip() for k, v in vs.items()}, glossary=g.get('1.0', 'end').strip())
            save_cfg(self.cfg)
            w.destroy()
        ttk.Button(w, text='保存', command=save).grid(row=6, column=1, sticky='e', padx=12, pady=(6, 12))
        w.bind('<Return>', save)
        w.bind('<Escape>', lambda e: w.destroy())
        w.grab_set()
        w.wait_window()

    # ---------- document & rendering ----------
    def ask_open(self):
        for path in filedialog.askopenfilenames(filetypes=[('PDF', '*.pdf'), ('所有文件', '*.*')]):
            self.open(path)

    def open(self, path, show=True):
        """Open a PDF in a new tab (or switch to its tab if it is already open). At start-up
        show=False only adds the tabs; the current one is shown once, at the end."""
        path = os.path.abspath(path)
        for i, tab in enumerate(self.tabs):
            if os.path.normcase(tab['path']) == os.path.normcase(path):
                return self.switch(i)
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            return messagebox.showerror('无法打开', str(e))
        if doc.needs_pass:
            pw = simpledialog.askstring('需要密码', '这个 PDF 已加密，请输入密码：', show='*', parent=self.root)
            if not pw or not doc.authenticate(pw):
                return messagebox.showerror('无法打开', '密码不正确')
        if not doc.page_count:
            return messagebox.showerror('无法打开', '文档没有页面')
        self.stash()
        self.tabs.append(dict(blank_tab(), doc=doc, path=path, rects=[p.rect for p in doc]))
        self.cur = len(self.tabs) - 1
        if show:
            self.show_tab()

    def restore(self):
        """Add the tabs that were open when the app was last closed, at the same places."""
        s = self.cfg.get('session') or {}
        for t in s.get('tabs', []):
            n = len(self.tabs)
            if os.path.exists(t['path']):
                self.open(t['path'], False)
            if len(self.tabs) > n:
                self.tabs[-1].update(view=t['view'], zoom=t['zoom'], fit=t['fit'])
        if self.tabs:
            self.cur = min(s.get('current') or 0, len(self.tabs) - 1)

    def save_session(self):
        self.stash()
        self.cfg['session'] = {'tabs': [{k: t[k] for k in ('path', 'view', 'zoom', 'fit')} for t in self.tabs],
                               'current': self.cur}
        save_cfg(self.cfg)

    def close(self):
        """Close the window. With several files open it asks first, as browsers do for their tabs."""
        if len(self.tabs) > 1 and not messagebox.askokcancel(
                '关闭 LexiTap', f'要关闭全部 {len(self.tabs)} 个文件吗？\n下次启动时会重新打开它们。', parent=self.root):
            return
        try:
            self.save_session()
        finally:  # the window closes even if the settings folder can't be written
            self.root.destroy()

    def paper_title(self):
        """The paper's title, as context for translation: the PDF's metadata, else the largest
        text on the first page."""
        t = (self.doc.metadata or {}).get('title') or ''
        if len(t.split()) >= 3 and not re.search(r'\.(docx?|pdf|tex)\b|^Microsoft', t):
            return t.strip()
        best = max(((b[4][1], n) for n, b in self.blocks(0).items() if b[4][2] != 'margin' and b[2] >= 3), default=None)
        return unicodedata.normalize('NFC', ' '.join(w[4] for w in self.words(0) if w[5] == best[1]))[:200] if best else ''

    # ---------- tabs ----------
    def view(self):
        """Where the view is: (page, points below the page top)."""
        y = self.canvas.canvasy(0)
        p = self.page_at(y)
        return p, (y - self.tops[p]) / self.zoom

    def moveto(self, y):
        """Scroll canvas point y to the top of the view."""
        self.schedule_render()  # asked for first, so the tiles are drawn before the view is displayed, not after
        self.canvas.yview_moveto(y / self.total_h)

    def scroll_to(self, p, off):
        self.moveto(self.tops[min(p, len(self.tops) - 1)] + off * self.zoom)  # the file may have lost pages since

    def stash(self):
        """Save the shown document's state (and where it is scrolled to) into its tab."""
        if self.tabs and self.doc:
            tab = self.tabs[self.cur]
            for k in TAB_STATE:
                tab[k] = getattr(self, k)
            tab['view'] = self.view()

    def show_tab(self):
        """Show the current tab's document, scrolled to where it was (or the start screen)."""
        tab = self.tabs[self.cur] if self.tabs else blank_tab()
        for k in TAB_STATE:
            setattr(self, k, tab[k])
        doc = self.doc
        self.root.title(f'{os.path.basename(self.path)} - LexiTap' if doc else 'LexiTap')
        self.plabel.config(text=f'/ {doc.page_count if doc else 0}')
        self.pvar.set('')
        self.zlabel.config(text='')
        menu = tk.Menu(self.toc, tearoff=False)
        for lvl, title, page in doc.get_toc()[:400] if doc else ():
            if lvl <= 2 and page > 0:
                menu.add_command(label='　' * (lvl - 1) + title, command=lambda p=page - 1: self.goto(p))
        self.toc.configure(menu=menu, state='normal' if menu.index('end') is not None else 'disabled')
        self.tops, self.fq, self.dls = [], None, {}
        self.fstat.config(text='')
        self.layout(tab['view'] or (0, -10 * GAP))  # a new document starts at its top
        self.draw_tabs()
        self.canvas.focus_set()

    def switch(self, i):
        if self.tabs and i != self.cur:
            self.stash()
            self.cur = i
            self.show_tab()

    def next_tab(self, d):
        if self.tabs:
            self.switch((self.cur + d) % len(self.tabs))
        return 'break'

    def close_tab(self, i):
        if i == self.cur:  # the neighbour takes its place
            tab = self.tabs.pop(i)
            self.cur = min(i, len(self.tabs) - 1) if self.tabs else None
            self.show_tab()
        else:
            tab = self.tabs.pop(i)
            self.cur -= i < self.cur
            self.draw_tabs()
        tab['doc'].close()
        return 'break'

    def on_tabbar_resize(self, e):
        if e.width != self._tabw:
            self._tabw = e.width
            self.draw_tabs()

    def draw_tabs(self):
        """Browser-style strip with one tab per open PDF: click to switch, × or middle-click to
        close, + to open more. Tabs shrink as more are opened."""
        bar, f, h = self.tabbar, self.tabfont, self.tabh
        for w in bar.winfo_children():
            w.destroy()
        width = max(90, min(240, (bar.winfo_width() - 50) // max(len(self.tabs), 1)))
        for i, tab in enumerate(self.tabs):
            bg = TAB_ON if i == self.cur else TAB_OFF
            t = tk.Frame(bar, bg=bg, width=width, height=h)
            t.pack_propagate(False)
            t.pack(side='left', padx=(6 if i == 0 else 0, 1), pady=(5, 0))
            x = tk.Label(t, text='×', bg=bg, fg='#777', font=f, padx=7)
            x.pack(side='right')
            name = tk.Label(t, text=self.ellipsize(os.path.basename(tab['path']), width - 44), bg=bg, font=f,
                            anchor='w', padx=10)
            name.pack(side='left', fill='both', expand=True)
            for w in (t, name, x):
                if i != self.cur:
                    w.bind('<Enter>', lambda e, ws=(t, name, x): [v.config(bg=TAB_HOVER) for v in ws])
                    w.bind('<Leave>', lambda e, ws=(t, name, x): [v.config(bg=TAB_OFF) for v in ws])
                w.bind('<Button-2>', lambda e, i=i: self.close_tab(i))  # middle click closes
            for w in (t, name):
                w.bind('<Button-1>', lambda e, i=i: self.switch(i))
            x.bind('<Button-1>', lambda e, i=i: self.close_tab(i))
            x.bind('<Enter>', lambda e: e.widget.config(fg='#c0392b'), add=True)
            x.bind('<Leave>', lambda e: e.widget.config(fg='#777'), add=True)
            if i == self.cur:  # a coloured line along the top of the open tab
                tk.Frame(t, bg=TAB_LINE, height=2).place(x=0, y=0, relwidth=1)
        plus = tk.Label(bar, text='＋', bg=TAB_BAR, fg='#444', font=f, padx=10)
        plus.pack(side='left', fill='y', pady=(5, 0))
        plus.bind('<Button-1>', lambda e: self.ask_open())

    def ellipsize(self, s, px):
        """Shorten s with '…' to fit px pixels in the tab font."""
        if self.tabfont.measure(s) <= px:
            return s
        while s and self.tabfont.measure(s + '…') > px:
            s = s[:-1]
        return s + '…'

    def layout(self, at=None):
        """Stack all pages vertically at the current zoom, as white placeholders, and show the same
        place as before, or the place at: (page, points below its top)."""
        c = self.canvas
        if self._layout:  # no need for the re-layout that was waiting for the window to stop resizing
            self.root.after_cancel(self._layout)
            self._layout = None
        at = at or self.tops and self.view()
        self.schedule_render()  # asked for before the canvas changes: the tiles are drawn before it is displayed
        c.delete('all')
        self.tiles.clear()
        W = self._w = c.winfo_width()
        if not self.doc:
            c.create_text(W / 2, c.winfo_height() / 2, text='点「打开」或按 Ctrl+O 打开 PDF',
                          fill='#888', font=('Microsoft YaHei UI', 14))
            return
        widest = max(r.width for r in self.rects)
        if self.fit:
            self.zoom = max((W - 2 * GAP) / widest, 0.1)
        z, y = self.zoom, GAP
        self.tops, self.lefts, self.vis = [], [], None
        for r in self.rects:
            x = max(GAP, int((W - r.width * z) / 2))
            self.tops.append(y)
            self.lefts.append(x)
            c.create_rectangle(x, y, x + r.width * z, y + r.height * z, fill='white', outline='#cfcfcf')
            y += int(r.height * z) + GAP
        self.total_h = y
        c.configure(scrollregion=(0, 0, max(W, widest * z + 2 * GAP), y))
        if at:
            self.scroll_to(*at)
        self.zlabel.config(text=f'{z * 72 / self.dpi:.0%}')

    def set_zoom(self, z):
        """z=None means fit page width."""
        if self.doc:
            at = self.view()
            self.fit = z is None
            if z:
                self.zoom = min(max(z, 0.2), 8.0)
            self.layout(at)

    def schedule_render(self):
        if self.doc and not self._render:
            self._render = self.root.after_idle(self.render)

    def tile(self, p, j, i):
        """Row j, column i of page p's grid of TILE-sized strips, drawn at the current zoom."""
        if p not in self.dls:
            self.dls[p] = self.doc[p].get_displaylist()  # the page's content, read once for all its tiles
        z, (w, h) = self.zoom, TILE
        return self.dls[p].get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False,
                                      clip=pymupdf.Rect(i * w / z, j * h / z, (i + 1) * w / z, (j + 1) * h / z))

    def render(self):
        """Draw the missing tiles of the pages. Those in view come first, for up to 30 ms at a
        time; then, each time the app is idle, the one nearest the view, up to a screen above and
        below it, so that scrolling finds them ready. Tiles farther away are dropped."""
        self._render = None
        if not self.tops:
            return
        c, z, (tw, th) = self.canvas, self.zoom, TILE
        x0, y0, w, h = c.canvasx(0), c.canvasy(0), c.winfo_width(), c.winfo_height()
        near, want = range(self.page_at(y0 - h), self.page_at(y0 + 2 * h) + 1), {}
        for p in near:  # tile -> px between it and the view (0: in view)
            x, y, r = self.lefts[p], self.tops[p], self.rects[p]
            for j in range(max(int(y0 - h - y) // th, 0), (min(int(y0 + 2 * h - y), int(r.height * z)) - 1) // th + 1):
                for i in range(max(int(x0 - x) // tw, 0), (min(int(x0 + w - x), int(r.width * z)) - 1) // tw + 1):
                    want[p, j, i] = max(y + j * th - y0 - h, y0 - y - (j + 1) * th, 0)
        for k in [k for k in self.tiles if k not in want]:
            c.delete(self.tiles.pop(k)[1])
        self.dls = {p: dl for p, dl in self.dls.items() if p in near}
        t0 = time.perf_counter()
        for n, k in enumerate(sorted((k for k in want if k not in self.tiles), key=want.get)):
            if n and (want[k] or time.perf_counter() - t0 > 0.03):
                self._render = self.root.after_idle(self.render)  # the rest when the app is idle again
                break
            p, j, i = k
            try:
                img = tk.PhotoImage(data=self.tile(p, j, i).tobytes('ppm'))
                self.tiles[k] = img, c.create_image(self.lefts[p] + i * tw, self.tops[p] + j * th, image=img, anchor='nw')
            except Exception:  # a damaged page stays white
                self.tiles[k] = None, 0
        vis = self.page_at(y0), self.page_at(y0 + h)
        if vis == self.vis:
            c.tag_raise('sel')  # the selection stays above the new tiles
        else:  # other pages are in view now: draw their part of the selection and of the search matches
            self.vis = vis
            self.draw_sel()
        if str(self.root.tk.call('focus')) != str(self.pentry):
            self.pvar.set(self.page_at(y0 + h / 3) + 1)

    def page_at(self, y):
        return min(max(bisect.bisect_right(self.tops, y) - 1, 0), len(self.tops) - 1)

    def scroll_px(self, dy):
        self.moveto(self.canvas.canvasy(0) + dy)

    def goto(self, p):
        if self.doc:
            self.moveto(self.tops[min(max(p, 0), len(self.tops) - 1)] - GAP / 2)

    def on_page_entry(self, e):
        try:
            self.goto(int(self.pvar.get()) - 1)
        except ValueError:
            pass
        self.canvas.focus_set()

    def on_resize(self, e):
        if self.doc and e.width == self._w:  # only the height changed: the pages stay where they are
            return self.schedule_render()
        if self._layout:
            self.root.after_cancel(self._layout)
        self._layout = self.root.after(120, self.layout)

    def on_wheel(self, e):
        self.set_hover(None)
        if not self.doc:
            return
        if e.state & 4:  # Ctrl+wheel: zoom
            return self.set_zoom(self.zoom * (1.1 if e.delta > 0 else 1 / 1.1))
        if e.state & 1:  # Shift+wheel: horizontal scroll
            self.schedule_render()
            return self.canvas.xview_scroll(-1 if e.delta > 0 else 1, 'units')
        self.scroll_px(-e.delta)
        if self.drag:
            self.update_drag()

    def on_hover(self, e):
        """Hand cursor and a preview over links, I-beam over text, arrow elsewhere."""
        over = link = None
        if self.doc:
            p, x, y = self.point(e.x, e.y)
            link = self.link_at(e.x, e.y)
            over = any(w[0] - 3 <= x <= w[2] + 3 and w[1] - 2 <= y <= w[3] + 2 for w in self.words(p))
        cursor = 'hand2' if link else 'xterm' if over else ''
        if self.canvas.cget('cursor') != cursor:
            self.canvas.configure(cursor=cursor)
        self.set_hover(link)

    # ---------- links: citations, figure references, web addresses ----------
    def links(self, p):
        if p not in self.lcache:  # their rectangles already match the rendered page
            self.lcache[p] = self.doc[p].get_links()
        return self.lcache[p]

    def link_at(self, x, y):
        """The link under window point (x, y), as (page, link), or None."""
        p, px, py = self.point(x, y)
        return next(((p, l) for l in self.links(p) if pymupdf.Point(px, py) in l['from']), None)

    def target(self, p, link):
        """Where an internal link leads: (page, clip), the clip being the cited entry of the
        reference list for a citation number, or None for the whole page (a figure, a section)."""
        q = link.get('page')
        if link.get('kind') not in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) or q is None or q < 0:
            return None
        text = self.doc[p].get_textbox(link['from'])
        m = re.search(r'\d+', text)
        if m and not re.search(r'fig|tab|sch', f"{link.get('nameddest', '')}{text}", re.I):
            for w in self.words(q):  # the line in the reference list that starts with this number
                if w[7] == 0 and w[4].strip('.()[]') == m.group():
                    b = self.blocks(q)[w[5]][0]
                    return q, pymupdf.Rect(b.x0 - 3, w[1] - 3, b.x1 + 3, w[1] + 70)
        return q, None

    def set_hover(self, link):
        """Preview where a link leads once the mouse rests on it for a moment."""
        if link != self.hover:
            self.hover = link
            self.hide_tip()
            if self._tipjob:
                self.root.after_cancel(self._tipjob)
            self._tipjob = link and self.root.after(400, lambda: self.preview(*link))

    def preview(self, p, link):
        self._tipjob = None
        if link.get('kind') == pymupdf.LINK_URI:
            return self.show_tip(text=link.get('uri'))
        t = self.target(p, link)
        if t:
            page = self.doc[t[0]]
            z = 1.8 if t[1] else 440 / page.rect.height  # a reference entry, or the whole target page
            pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=t[1], alpha=False)
            self.show_tip(image=tk.PhotoImage(data=pix.tobytes('ppm')))

    def show_tip(self, image=None, text=None):
        """A small borderless window next to the mouse, kept on screen."""
        self.hide_tip()
        tip = self.tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tk.Label(tip, image=image, text=text, bg='#fffff4', bd=1, relief='solid', padx=4, pady=3).pack()
        tip.image = image
        tip.update_idletasks()
        x, y = self.root.winfo_pointerxy()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        tip.geometry(f'+{max(min(x + 16, sw - w), 0)}+{y + 18 if y + 18 + h < sh else max(y - h - 12, 0)}')

    def hide_tip(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def follow(self, p, link):
        """Open a web link in the browser, or jump to an internal target (Alt+← comes back)."""
        self.set_hover(None)
        if link.get('kind') == pymupdf.LINK_URI:
            import webbrowser  # loaded on first use
            return webbrowser.open(link['uri'])
        t = self.target(p, link)
        if t:
            self.back.append(self.view())
            self.scroll_to(t[0], t[1].y0 - 30 if t[1] else -GAP / 2)

    def go_back(self):
        if self.back:
            self.scroll_to(*self.back.pop())
        return 'break'

    # ---------- words & sentences ----------
    def words(self, p):
        """Words of page p as (x0, y0, x1, y1, text, block, line, word_no, cls) in the PDF's stored
        order, with coordinates matching the rendered page. cls describes the word's text block:
        (direction, main font size, kind, main font), kind being 'margin' (header, footer, side
        stamp), 'caption' or 'body'. PDFs store captions, headings, footers and watermarks at
        arbitrary points among the body text, so a selection only takes words of one class."""
        ws = self.wcache.get(p)
        if ws is None:
            if len(self.wcache) > 80:
                self.wcache, self.binfo, self.hcache = {}, {}, {}
            page = self.doc[p]
            tp = page.get_textpage(flags=WORD_FLAGS)
            m, r = page.rotation_matrix, page.rect  # blocks reaching < 3 pt into the inner area are margin
            inner = pymupdf.Rect(r.width * MARGIN + 3, r.height * MARGIN + 3,
                                 r.width * (1 - MARGIN) - 3, r.height * (1 - MARGIN) - 3)
            cls, boxes, nchars, texts = {}, {}, {}, {}  # per block: class, bbox, characters, text
            flow = {}  # class -> (bbox, characters, number) of its last block in the text flow
            for b in page.get_text('dict', textpage=tp)['blocks']:
                sizes, fonts = {}, {}  # characters per font size / per font
                for line in b.get('lines', ()):
                    for s in line['spans']:
                        sizes[round(s['size'], 1)] = sizes.get(round(s['size'], 1), 0) + len(s['text'])
                        f = s['font'].split('+')[-1]  # drop the subset tag, as in "ABCDEF+Times"
                        fonts[f] = fonts.get(f, 0) + len(s['text'])
                if sizes:
                    first, box = b['lines'][0], pymupdf.Rect(b['bbox']) * m
                    boxes[b['number']] = box
                    text = ''.join(s['text'] for line in b['lines'] for s in line['spans']).lstrip()
                    kind = ('caption' if CAPTION.match(text) else
                            'note' if box.y0 > r.height * 0.6 and NOTE.match(text) else 'body')
                    texts[b['number']] = kind, re.sub(r'[\d\s]', '', text), b['bbox'][1]  # its kind outside the margin strip
                    kind = kind if box.intersects(inner) else 'margin'
                    c = tuple(round(v, 1) for v in first['dir']), max(sizes, key=sizes.get), kind, max(fonts, key=fonts.get)
                    cls[b['number']], n, prev = c, sum(sizes.values()), flow.get(c)
                    nchars[b['number']] = n
                    # Text flows down a column or right into the next one. Of two blocks of a class that
                    # break that order, the smaller is an inserted box (a journal banner stored after the
                    # body, a page number stored before it), which gets a kind of its own.
                    if kind == 'body' and c[0] == (1.0, 0.0) and prev and box.y0 < prev[0].y0 - 3 and box.x0 < prev[0].x1 - 3:
                        if n <= prev[1]:
                            cls[b['number']] = c[:2] + ('box',) + c[3:]
                            continue
                        cls[prev[2]] = c[:2] + ('box',) + c[3:]
                    flow[c] = box, n, b['number']
            # MuPDF gives each paragraph a block of its own, so the first or last paragraph of a column can lie
            # wholly in the margin strip. It is text, not a header or footer, if it continues the text right
            # below or above it (same font and size, less than a line and a half away, overlapping it sideways)
            # and nothing like it stands at that height on the pages around (as page numbers and headers do).
            near = None  # (top, text without digits) of the blocks on the two pages before and after
            for _ in range(3):
                for k in [k for k in boxes if cls[k][2] == 'margin']:
                    a, c = boxes[k], cls[k]
                    j = next((j for j, b in boxes.items() if cls[j][2] != 'margin' and cls[j][:2] + cls[j][3:] == c[:2] + c[3:]
                              and max(a.y0 - b.y1, b.y0 - a.y1) < 1.5 * c[1]
                              and min(a.x1, b.x1) - max(a.x0, b.x0) > 0.5 * min(a.width, b.width)), None)
                    if j is None:
                        continue
                    if near is None:
                        near = [(b[1], re.sub(r'[\d\s]', '', b[4])) for q in (p - 2, p - 1, p + 1, p + 2)
                                if 0 <= q < self.doc.page_count for b in self.doc[q].get_text('blocks', flags=WORD_FLAGS)]
                    if not any(abs(y - texts[k][2]) < 4 and t == texts[k][1] for y, t in near):
                        cls[k] = cls[j] if texts[k][0] == 'body' else c[:2] + (texts[k][0],) + c[3:]
            # MuPDF splits lines into separate blocks at formulas, accents and sub/superscripts. A short
            # block on the same line as a bigger one, touching it or inside its outline, joins its text
            # (a caption first). Repeated so chains of pieces resolve; tall blocks never change.
            for _ in range(3):
                for k, a in boxes.items():
                    best = None
                    for j, b in boxes.items():
                        s = cls[j][1]
                        if (nchars[j] > nchars[k] and cls[j][2] != 'margin' != cls[k][2] and a.height <= 2.5 * s
                                and min(a.y1, b.y1) - max(a.y0, b.y0) >= 0.5 * a.height
                                and a.x0 <= b.x1 + 0.6 * s and b.x0 <= a.x1 + 0.6 * s
                                and (best is None or (cls[j][2] == 'caption', nchars[j]) > (cls[best][2] == 'caption', nchars[best]))):
                            best = j
                    if best is not None:
                        cls[k] = cls[best]
            # MuPDF may also split a caption into stacked blocks: those right below it, in its font, continue it
            for k0 in sorted((k for k, c in cls.items() if c[2] == 'caption'), key=lambda k: boxes[k].y0):
                cap, base = pymupdf.Rect(boxes[k0]), cls[k0][:2] + cls[k0][3:]
                for k in sorted(boxes, key=lambda k: boxes[k].y0):
                    c, b = cls[k], boxes[k]
                    if (c[2] in ('body', 'box') and c[:2] + c[3:] == base and cap.y0 <= b.y0 < cap.y1 + c[1]
                            and b.x0 < cap.x1 and cap.x0 < b.x1):
                        cls[k] = c[:2] + ('caption',) + c[3:]
                        cap |= b
            ws = self.wcache[p] = [(*(pymupdf.Rect(w[:4]) * m), *w[4:8], cls[w[5]])
                                   for w in page.get_text('words', textpage=tp) if w[5] in cls]
            info = self.binfo[p] = {}  # block -> [bbox, {line: x of its first word}, words, letters, cls, text]
            for w in ws:
                b = info.setdefault(w[5], [pymupdf.Rect(w[:4]), {}, 0, 0, w[8], ''])
                b[0] |= pymupdf.Rect(w[:4])
                b[1].setdefault(w[6], w[0])
                b[2] += 1
                b[3] += sum(ch.isalpha() for ch in w[4])
                b[5] += w[4]
        return ws

    def word(self, pos):
        return self.words(pos[0])[pos[1]]

    def blocks(self, p):
        self.words(p)
        return self.binfo[p]

    def heads(self, p):
        """Headings on page p for the selection's text: short blocks in another font, not much
        smaller than the text, sitting right above a block of it and within its width. They are
        mostly letters, don't start in lower case or end a sentence (unlike equations, code, caption
        pieces), and are not repeated at the same place on a neighbouring page (running headers)."""
        key = p, self.cls
        if key not in self.hcache:
            c, info, hs = self.cls, self.blocks(p), set()
            if c:
                text = [b[0] for b in info.values() if self.same(b[4])]
                caps = {(b[4][1], b[4][3]) for b in info.values() if b[4][2] == 'caption'}  # caption fonts
                for n, (r, lines, nw, letters, k, s) in info.items():
                    if (k[0] == (1.0, 0.0) and k[2] in ('body', 'box') and not self.same(k) and len(lines) <= 3
                            and nw <= 20 and letters >= max(3, 0.6 * len(s)) and k[1] >= 0.85 * c[1]
                            and (k[1], k[3]) not in caps and not s[:1].islower() and not END.search(s)
                            and '=' not in s and not re.search(r'\([\d.:]+[a-z]?\)$', s)
                            and any(-2 <= t.y0 - r.y1 <= 3 * r.height / len(lines) and t.x0 - 5 <= r.x0 and r.x1 <= t.x1 + 5
                                    for t in text)
                            and not any(re.sub(r'\d', '', b[5]) == re.sub(r'\d', '', s) and abs(b[0].y0 - r.y0) < 5
                                        for q in (p - 1, p + 1) if 0 <= q < self.doc.page_count
                                        for b in self.blocks(q).values())):
                        hs.add(n)
            self.hcache[key] = hs
        return self.hcache[key]

    def same(self, c):
        """Does text class c belong to the selection's class?"""
        a = self.cls
        return a is None or c[0] == a[0] and c[2:] == a[2:] and abs(c[1] - a[1]) <= SIZE_TOL * a[1]

    def pick(self, a, b):
        """The selection's class: the one where the drag started (the anchor), unless that was a
        small block (a heading, a label) and the drag ended in other text, e.g. a drag from a
        heading into the paragraphs below it."""
        start = self.anchor
        end = b if a == start else a
        self.cls = self.word(start)[8]
        blk = self.blocks(start[0])[self.word(start)[5]]
        if not self.same(self.word(end)[8]) and len(blk[1]) <= 3 and blk[2] <= 20:
            self.cls = self.word(end)[8]

    def in_flow(self, pos):
        """Is the word at pos part of the selection's text (its class, or a heading of it)?"""
        w = self.word(pos)
        return self.same(w[8]) or w[5] in self.heads(pos[0])

    def step(self, pos, d):
        """The next (d=1) or previous (d=-1) word of the selection's text, or None. Only body
        text continues onto other pages (skipping at most 3 pages without any)."""
        p, i = pos[0], pos[1] + d
        reach = 3 if (self.cls or self.word(pos)[8])[2] == 'body' else 0
        while 0 <= p < self.doc.page_count and abs(p - pos[0]) <= reach:
            ws, hs = self.words(p), self.heads(p)
            i = (0 if d > 0 else len(ws) - 1) if i is None else i
            while 0 <= i < len(ws):
                if self.same(ws[i][8]) or ws[i][5] in hs:
                    return p, i
                i += d
            p, i = p + d, None
        return None

    def span(self, a, b):
        """Positions of the words from a to b (inclusive) that belong to the selection's text."""
        for p in range(a[0], b[0] + 1):
            ws, hs = self.words(p), self.heads(p)
            for i in range(a[1] if p == a[0] else 0, min(b[1] + 1, len(ws)) if p == b[0] else len(ws)):
                if self.same(ws[i][8]) or ws[i][5] in hs:
                    yield p, i

    def para_break(self, pos, nxt):
        """Does a new paragraph start between the words at pos and nxt (neighbours in the selection's
        text)? Headings stand alone; otherwise look for a bullet, a first-line indent, or a sentence
        end on a short line or before extra space."""
        (p, i), (q, j) = pos, nxt
        w, n = self.words(p)[i], self.words(q)[j]
        h, cap = w[3] - w[1], n[4][:1].isupper() or n[4][:1].isdigit()
        if (p, w[5], w[6]) == (q, n[5], n[6]) or p == q and n[0] >= w[2] - 1 and min(w[3], n[3]) - max(w[1], n[1]) > 0.3 * h:
            return False  # same line, or a sub/superscript "line" beside it
        if w[8][2] == n[8][2] == 'caption':  # a caption is one paragraph, until the next "Figure N."
            return (p, w[5]) != (q, n[5]) and bool(CAPTION.match(self.blocks(q)[n[5]][5]))
        bw, bn = self.blocks(p)[w[5]], self.blocks(q)[n[5]]
        if (p, w[5]) != (q, n[5]) and (w[5] in self.heads(p) or n[5] in self.heads(q) or
                                       p == q and len(bw[1]) == 1 and (w[4].isupper() or cap)):
            return True  # a heading, or a one-line block that looks like one
        ends = bool(END.search(w[4])) and cap
        return (n[4][:1] in '•●▪■◦'
                or bn[0].x0 + 0.6 * h < n[0] < bn[0].x0 + 4 * h and bw[1][w[6]] < bw[0].x0 + 0.3 * h  # indent
                or ends and w[2] < bw[0].x1 - 0.15 * bw[0].width  # short last line
                or ends and p == q and n[1] > w[3] + 0.8 * h and n[0] < bw[0].x1 and bw[0].x0 < n[2])  # space below

    def is_end(self, pos):
        """True if a sentence ends right after the word at pos."""
        nxt = self.step(pos, 1)
        if nxt is None or self.para_break(pos, nxt):
            return True
        w, n = self.word(pos), self.word(nxt)
        m = END.search(w[4])
        if m and not (m.group(2) and w[4][m.start() - 1:m.start()].isdigit()):  # "3.14" is no end
            t = w[4][:m.end(1)]
            if m.group(1) != '.':
                return True
            if t.lower().lstrip('(["\'“‘') in ABBR or re.fullmatch(r'\(?[A-Z]\.', t):
                return False  # abbreviation or initial
            # "x. lowercase" is rarely a sentence end, unless the lowercase word is a link
            return not n[4][:1].islower() or n[4].startswith(('http', 'www.'))
        return False

    def expand(self, a, b):
        """Grow the selection a..b outwards to sentence boundaries."""
        for _ in range(150):
            p = self.step(a, -1)
            if p is None or self.is_end(p):
                break
            a = p
        for _ in range(150):
            if self.is_end(b):
                break
            b = self.step(b, 1)
        return a, b

    def is_math(self, p, n):
        """Is block n of page p a formula: a few lines of mostly symbols and digits?"""
        r, lines, nw, letters, k, s = self.blocks(p)[n]
        return len(lines) <= 4 and (letters < 0.4 * len(s) or letters < 0.75 * len(s) and bool(MATH.search(s)))

    def text(self, a, b, formulas=False):
        """Text from a to b, one paragraph per line. Re-joins words hyphenated at line ends; no
        space across CJK line breaks. formulas=True writes [公式] for each formula, whose extracted
        text is garbled (used for translation)."""
        out, last, prev = '', None, None
        for pos in self.span(a, b):
            w = self.word(pos)
            s, key = w[4], (pos[0], w[5], w[6])
            if '⎯' in s:  # a piece of a drawn arrow or brace over a formula, not text
                continue
            if formulas and self.is_math(pos[0], w[5]):
                if out.endswith('[公式]'):
                    last, prev = key, pos
                    continue
                s = '[公式]'
            if out:
                if self.para_break(prev, pos):
                    out += '\n'
                elif key != last and out.endswith(('-', '\xad')) and s[:1].islower():
                    out = out[:-1]
                elif key == last or not (out[-1] > '⺀' and s[0] > '⺀'):
                    out += ' '
            out += s
            last, prev = key, pos
        return unicodedata.normalize('NFC', out)  # "e" + combining accent -> "é"

    # ---------- selection ----------
    def point(self, x, y):
        """Window coordinates -> (page, x, y in page points)."""
        c = self.canvas
        cx, cy = c.canvasx(x), c.canvasy(y)
        p = self.page_at(cy)
        return p, (cx - self.lefts[p]) / self.zoom, (cy - self.tops[p]) / self.zoom

    def pos_at(self, x, y, flow=False):
        """Window coordinates -> nearest word position (flow=True: nearest word of the selection's text)."""
        p, px, py = self.point(x, y)
        ws = self.words(p)
        idx = [i for i in range(len(ws)) if not flow or self.in_flow((p, i))]
        if not idx:
            return None
        return p, min(idx, key=lambda i: max(ws[i][0] - px, 0, px - ws[i][2])
                      + 3 * max(ws[i][1] - py, 0, py - ws[i][3]))

    def extend(self, x, y):
        """Select from the anchor to the point (x, y): choose the class, then end the selection at
        the nearest word of it, so dragging past the end of a caption stops at its last line."""
        pos = self.pos_at(x, y)
        if pos:
            self.pick(*sorted((self.anchor, pos)))
            self.sel = tuple(sorted((self.anchor, self.pos_at(x, y, True) or pos)))

    def on_press(self, e):
        self.canvas.focus_set()
        self.set_hover(None)
        pos = self.doc and self.pos_at(e.x, e.y)
        if not pos:
            return
        self.press_link = self.link_at(e.x, e.y)
        if e.state & 1 and self.anchor:  # Shift+click extends the selection
            self.extend(e.x, e.y)
            return self.finish()
        self.anchor, self.sel, self.drag, self.press = pos, None, (e.x, e.y), (e.x, e.y)
        self.cls = self.word(pos)[8]
        self.draw_sel()

    def on_motion(self, e):
        if not self.drag:
            return
        self.drag = (e.x, e.y)
        self.update_drag()
        if not self._auto and not 0 <= e.y < self.canvas.winfo_height():
            self._auto = self.root.after(30, self.autoscroll)

    def update_drag(self):
        self.extend(*self.drag)
        if self.sel:
            self.draw_sel()

    def autoscroll(self):
        """Keep scrolling while the mouse is dragged above/below the view (for cross-page selection)."""
        self._auto = None
        if self.drag:
            y, h = self.drag[1], self.canvas.winfo_height()
            d = y if y < 0 else y - h if y >= h else 0
            if d:
                self.scroll_px(max(-80, min(80, d)))
                self.update_drag()
                self._auto = self.root.after(30, self.autoscroll)

    def on_release(self, e):
        if not self.drag:
            return
        self.drag = None
        moved = abs(e.x - self.press[0]) + abs(e.y - self.press[1]) > 3
        if self.sel and (moved or self.sel[0] != self.sel[1]):
            self.finish()
        else:  # a plain click clears the selection, or follows a link
            self.sel = None
            self.draw_sel()
            if self.press_link:
                self.follow(*self.press_link)

    def on_double(self, e):
        pos = self.doc and self.pos_at(e.x, e.y)
        if pos:
            self.drag, self.anchor, self.sel, self.cls = None, pos, (pos, pos), self.word(pos)[8]
            self.finish()

    def finish(self):
        a, b = self.sel  # trim both ends to words of the selection's text
        a = a if self.in_flow(a) else self.step(a, 1) or a
        b = b if self.in_flow(b) else self.step(b, -1) or b
        on = self.panel.get()  # with the panel closed, selecting only selects
        if on and self.whole.get():
            a, b = self.expand(a, b)
        self.sel = a, b
        self.draw_sel()
        text = self.text(a, b, formulas=True)
        if on and any(ch.isalpha() for ch in text):
            self.translate(text)

    def next_sentence(self, d):
        """→ / ←: select and translate the sentence after (before) the selection."""
        if not self.doc:
            return 'break'
        if self.sel:
            pos = self.step(self.sel[1], 1) if d > 0 else self.step(self.sel[0], -1)
        else:  # nothing selected yet: start with the main text at the top of the view
            n = {}
            for w in self.words(self.view()[0]):
                if w[8][2] == 'body':
                    n[w[8]] = n.get(w[8], 0) + len(w[4])
            self.cls = max(n, key=n.get) if n else None
            pos = self.cls and self.pos_at(0, 0, True)
        if pos:
            self.anchor, self.sel = pos, (pos, pos)
            self.finish()
            self.reveal(self.sel[0])
        return 'break'

    def reveal(self, pos):
        """Scroll the word at pos into view (a third from the top) if it is not visible."""
        c, y = self.canvas, self.tops[pos[0]] + self.word(pos)[1] * self.zoom
        if not c.canvasy(0) + 10 < y < c.canvasy(c.winfo_height()) - 30:
            self.moveto(y - c.winfo_height() / 3)

    def draw_sel(self):
        """Cover each selected line (on visible pages) with a stretched translucent image, and box
        the search matches there."""
        c = self.canvas
        c.delete('sel')
        self.sel_imgs = []
        if not self.tops:
            return
        top, z = c.canvasy(0), self.zoom
        v0 = max(self.page_at(top) - 1, 0)
        v1 = min(self.page_at(top + c.winfo_height()) + 1, len(self.tops) - 1)
        for p in range(v0, v1 + 1) if self.fq else ():
            for j, r in enumerate(self.hits(p)):
                on = (p, j) == self.fcur
                c.create_rectangle(self.lefts[p] + r.x0 * z - 2, self.tops[p] + r.y0 * z - 1, self.lefts[p] + r.x1 * z + 2,
                                   self.tops[p] + r.y1 * z + 1, outline='#e8590c' if on else '#f0ad4e',
                                   width=3 if on else 1, tags='sel')
        if not self.sel:
            return
        a, b = max(self.sel[0], (v0, 0)), min(self.sel[1], (v1, 1 << 30))
        lines = {}
        for p, i in (self.span(a, b) if a <= b else ()):
            w = self.words(p)[i]
            k, r = (p, w[5], w[6]), lines.get((p, w[5], w[6]))
            lines[k] = w[:4] if r is None else (min(r[0], w[0]), min(r[1], w[1]), max(r[2], w[2]), max(r[3], w[3]))
        z = self.zoom
        for (p, _, _), (x0, y0, x1, y1) in lines.items():
            img = self.hl.zoom(max(1, round((x1 - x0) * z)), max(1, round((y1 - y0) * z)))
            c.create_image(self.lefts[p] + x0 * z, self.tops[p] + y0 * z, image=img, anchor='nw', tags='sel')
            self.sel_imgs.append(img)

    def copy(self, e=None):
        if self.sel:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.text(*self.sel))

    def on_menu(self, e):
        """Right-click on the pages: what can be done with the selected text, and printing."""
        self.set_hover(None)
        for i in range(4):
            self.menu.entryconfigure(i, state='normal' if self.sel else 'disabled')
        self.menu.entryconfigure(5, state='normal' if self.doc else 'disabled')
        self.menu.tk_popup(e.x_root, e.y_root)

    def translate_sel(self):
        if not self.panel.get():  # the translation needs its panel
            self.panel.set(True)
            self.toggle_panel()
        self.finish()
        self.reveal(self.sel[0])

    def find_sel(self):
        self.fvar.set(self.text(*self.sel).split('\n')[0][:60])
        self.find(1)

    def web_search(self):
        import urllib.parse, webbrowser  # loaded on first use
        webbrowser.open('https://www.bing.com/search?q=' + urllib.parse.quote(self.text(*self.sel)[:300]))

    # ---------- printing ----------
    def print_dialog(self, e=None):
        """Ctrl+P: a preview of the pages beside the choice of printer, pages, copies and colour, as in a
        browser. Returns the window (the tests drive it)."""
        if not self.doc:
            return
        w = tk.Toplevel(self.root)
        w.title('打印')
        w.transient(self.root)
        w.resizable(False, False)
        w.geometry(f'+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 60}')
        n, cur, s, at, job = self.doc.page_count, self.view()[0], self.dpi / 96, [0], []
        names = printers() + [SAVE_PDF]
        w.dest, w.spec, w.copies, gray = tk.StringVar(w, names[0]), tk.StringVar(w, '全部'), tk.StringVar(w, '1'), tk.BooleanVar(w)
        left = tk.Frame(w, bg='#e6e6e6', width=int(450 * s), height=int(600 * s))
        left.pack_propagate(False)
        left.grid(row=0, column=0, padx=12, pady=(12, 6))
        view = tk.Label(left, bg='#e6e6e6')
        view.pack(expand=True)
        nav = ttk.Frame(w)
        nav.grid(row=1, column=0, pady=(0, 10))
        ttk.Button(nav, text='◀', width=3, command=lambda: turn(-1)).pack(side='left')
        where = ttk.Label(nav, width=22, anchor='center')
        where.pack(side='left')
        ttk.Button(nav, text='▶', width=3, command=lambda: turn(1)).pack(side='left')
        form = ttk.Frame(w, padding=(4, 12, 14, 0))
        form.grid(row=0, column=1, sticky='n')
        fields = [('目标打印机', ttk.Combobox(form, textvariable=w.dest, values=names, state='readonly', width=34)),
                  ('页面', ttk.Combobox(form, textvariable=w.spec, values=['全部', '当前页', '奇数页', '偶数页'], width=34)),
                  ('份数', ttk.Spinbox(form, textvariable=w.copies, from_=1, to=99, width=6)),
                  ('颜色', ttk.Checkbutton(form, text='黑白', variable=gray))]
        for i, (label, f) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky='e', padx=(0, 8), pady=7)
            f.grid(row=i, column=1, sticky='w', pady=7)
        ttk.Label(form, foreground='#777', text='“页面”里也可以填页码，例如 1-5, 8').grid(row=4, column=1, sticky='w')

        def pages():
            """The chosen pages, counted from 0."""
            sets = {'全部': range(n), '当前页': [cur], '奇数页': range(0, n, 2), '偶数页': range(1, n, 2)}
            return list(sets.get(w.spec.get().strip()) or (p for a, b in re.findall(r'(\d+)(?:\s*[-–—~～－]\s*(\d+))?', w.spec.get())
                                                           for p in range(int(a) - 1, int(b or a)) if 0 <= p < n))

        def show(*_):
            ps, pdf = pages(), w.dest.get() == SAVE_PDF
            at[0] = max(min(at[0], len(ps) - 1), 0)
            for f in fields[2][1], fields[3][1]:  # a PDF file is written once and keeps its colours
                f.state(['disabled' if pdf else '!disabled'])
            if not job:
                w.ok.config(text='保存' if pdf else '打印', state='normal' if ps else 'disabled')
            where.config(text=f'第 {ps[at[0]] + 1} 页（{at[0] + 1} / {len(ps)}）' if ps else '没有这样的页')
            if ps:
                page = self.doc[ps[at[0]]]
                z = min(440 / page.rect.width, 590 / page.rect.height) * s
                pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False,
                                      colorspace=pymupdf.csGRAY if gray.get() and not pdf else pymupdf.csRGB)
                view.image = tk.PhotoImage(data=pix.tobytes('ppm'))
            view.config(image=view.image if ps else '')

        def turn(d):
            at[0] += d
            show()

        def step():  # Windows: a page at a time, so that the window stays alive; closing it cancels the job
            try:
                if not w.winfo_exists():
                    return job[0].close()
                w.ok.config(text=f'{next(job[0])} / {job[1]}')
                self.root.after(1, step)
            except StopIteration:
                w.destroy()
            except Exception as e:
                messagebox.showerror('无法打印', str(e), parent=w)
                w.destroy()

        def go():
            ps, name, title = pages(), w.dest.get(), os.path.basename(self.path)
            k = max(int(w.copies.get()), 1) if w.copies.get().isdigit() else 1
            try:
                if name == SAVE_PDF:
                    path = filedialog.asksaveasfilename(parent=w, defaultextension='.pdf', filetypes=[('PDF', '*.pdf')],
                                                        initialfile=os.path.splitext(title)[0] + '-选页.pdf')
                    if not path:
                        return
                    if any(os.path.normcase(os.path.abspath(path)) == os.path.normcase(t['path']) for t in self.tabs):
                        raise OSError('这个文件正开着，不能覆盖，请换一个文件名。')  # its pages are still being read from it
                    self.save_pages(ps, path)
                elif sys.platform == 'win32':
                    job[:] = spool(self.doc, name, ps * k, gray.get(), title), len(ps) * k
                    w.ok.config(state='disabled')
                    return step()
                else:  # macOS, Linux: their print service takes PDF files
                    import subprocess, tempfile
                    with tempfile.TemporaryDirectory() as d:
                        self.save_pages(ps, os.path.join(d, title))
                        done = subprocess.run(['lp', '-d', name, '-n', str(k), *(['-o', 'print-color-mode=monochrome'] * gray.get()),
                                               os.path.join(d, title)], capture_output=True, text=True, timeout=60)
                    if done.returncode:
                        raise OSError(done.stderr.strip())
                w.destroy()
            except Exception as e:
                messagebox.showerror('无法打印', str(e), parent=w)
        ends = ttk.Frame(w)
        ends.grid(row=1, column=1, sticky='e', padx=14, pady=(0, 10))
        w.ok = ttk.Button(ends, command=go)
        w.ok.pack(side='left', padx=8)
        ttk.Button(ends, text='取消', command=w.destroy).pack(side='left')
        for v in w.dest, w.spec, gray:
            v.trace_add('write', show)
        w.bind('<MouseWheel>', lambda e: turn(-1 if e.delta > 0 else 1))
        w.bind('<Return>', lambda e: w.ok.invoke())
        w.bind('<Escape>', lambda e: w.destroy())
        w.pages = pages
        show()
        w.grab_set()
        w.ok.focus_set()
        return w

    def save_pages(self, pages, path):
        """Write the chosen pages to a new PDF file."""
        new = pymupdf.open()
        new.insert_pdf(self.doc if self.doc.is_pdf else pymupdf.open('pdf', self.doc.convert_to_pdf()))
        new.select(pages)
        new.save(path, garbage=3, deflate=True)
        new.close()

    # ---------- search ----------
    def hits(self, p):
        """Where the search text is on page p. PDFs store "é" as one character or as "e" plus an
        accent, so both spellings are looked for; an accent found as a piece of its own joins its word."""
        if p not in self.fhits:
            page, out = self.doc[p], []
            for q in dict.fromkeys(unicodedata.normalize(form, self.fq) for form in ('NFC', 'NFD')):
                for r in page.search_for(q):
                    if out and abs(r.x0 - out[-1].x1) < 1 and abs(r.y0 - out[-1].y0) < r.height:
                        out[-1] |= r
                    else:
                        out.append(r)
            self.fhits[p] = [r * page.rotation_matrix for r in out]  # search rectangles are unrotated, like words
        return self.fhits[p]

    def find(self, d=1):
        """Enter / Shift+Enter in the search box, F3 / Shift+F3: go to the next / previous match."""
        q = self.fvar.get().strip()
        if not self.doc or not q:
            return 'break'
        if q != self.fq:  # a new search starts on the page in view
            self.fq, self.fhits, self.fcur = q, {}, (self.view()[0], None)
        (p, i), n = self.fcur, self.doc.page_count
        for _ in range(n + 1):
            hits = self.hits(p)
            i = (0 if d > 0 else len(hits) - 1) if i is None else i + d
            if 0 <= i < len(hits):
                break
            p, i = (p + d) % n, None
        else:
            self.fstat.config(text='无结果')
            return 'break'
        self.fcur = p, i
        self.fstat.config(text=f'第 {p + 1} 页')
        self.moveto(self.tops[p] + hits[i].y0 * self.zoom - self.canvas.winfo_height() / 3)
        self.draw_sel()
        return 'break'

    def end_find(self, e=None):
        self.fq = None
        self.fvar.set('')
        self.fstat.config(text='')
        self.draw_sel()
        self.canvas.focus_set()
        return 'break'

    # ---------- translation ----------
    def toggle_panel(self):
        """Open or close the translation panel, then fit the pages to the width that is left, so
        that the whole width of the page stays in view."""
        if self.panel.get():
            self.pw.add(self.right, weight=2)
            self.root.update_idletasks()
            if self.sash:  # as wide as it was when it was closed
                self.pw.sashpos(0, self.sash)
        else:
            self.sash = self.pw.sashpos(0)
            self.pw.forget(self.right)
        self.root.update_idletasks()
        for tab in self.tabs:
            tab['fit'] = True
        self.fit = True
        self.layout()

    def translate(self, text):
        if not self.cfg['api_key'] and not re.search(r'//(localhost|127\.0\.0\.1)', self.cfg['base_url']):
            self.settings()
            if not self.cfg['api_key']:
                return
        self.jobs += 1
        k = self.jobs
        self.entries[k] = [text.split('\n'), '', False, False, (self.path, self.sel, self.cls)]
        if len(self.entries) == 1:
            self.render_panel()  # replaces the usage hint
        else:
            self.out.configure(state='normal')
            self.add_entry(k)
            self.out.configure(state='disabled')
        self.out.see('end')
        key = '%08x ' % zlib.crc32(f"{self.cfg['model']}\n{self.cfg['prompt']}\n{self.cfg['glossary']}".encode()) + text
        if key in self.cache:  # translated before with the same settings: no API call
            self.q.put((k, self.cache[key], 'text'))
            self.q.put((k, None, 'done'))
        else:
            cfg = dict(self.cfg, title=self.paper_title())
            threading.Thread(target=self.worker, args=(cfg, text, k, key), daemon=True).start()

    def worker(self, cfg, text, k, key):
        parts = []
        try:
            for piece in llm_stream(cfg, text.replace('\n', '\n\n')):  # blank line between paragraphs
                parts.append(piece)
                self.q.put((k, piece, 'text'))
            self.cache[key] = ''.join(parts)
        except Exception as e:
            self.q.put((k, f'[翻译失败] {e}', 'error'))
        self.q.put((k, None, 'done'))

    def poll(self):
        """Move streamed text from worker threads into the panel (Tk is single-threaded), and open
        files that a second launch of the app handed over."""
        dirty, done = set(), False
        try:
            while True:
                k, s, kind = self.q.get_nowait()
                e = self.entries.get(k)
                done |= kind == 'done'
                if e:  # gone if the panel was cleared meanwhile
                    if kind == 'done':
                        e[3] = True
                    else:
                        e[1] += ('\n' if kind == 'error' and e[1] else '') + s
                        e[2] |= kind == 'error'
                    dirty.add(k)
        except queue.Empty:
            pass
        if dirty:
            self.out.configure(state='normal')
            for k in dirty:
                self.put_entry(k)
            self.out.configure(state='disabled')
            self.out.see(f'e{max(dirty)}')
        if done:  # keep the newest 3000 translations on disk
            while len(self.cache) > 3000:
                del self.cache[next(iter(self.cache))]
            save_json(CACHE_FILE, dict(self.cache))
        while not INBOX.empty():
            for path in INBOX.get():
                self.open(path)
            self.root.deiconify()
            self.root.attributes('-topmost', True)  # come to the front
            self.root.attributes('-topmost', False)
            self.root.focus_force()
        self.root.after(30, self.poll)

    def on_entry_link(self, e):
        """Click on an entry's "file · page" line: show that text in its PDF again."""
        k = next((int(t[1:]) for t in self.out.tag_names(f'@{e.x},{e.y}') if re.fullmatch(r'j\d+', t)), None)
        if k in self.entries:
            path, sel, cls = self.entries[k][4]
            self.open(path)
            if os.path.normcase(self.path) == os.path.normcase(path):
                self.sel, self.anchor, self.cls = sel, sel[0], cls
                self.reveal(sel[0])
                self.draw_sel()
        return 'break'

    def clear(self):
        self.entries.clear()
        self.render_panel()

    # ---------- panel layout (justified) ----------
    def render_panel(self):
        """Lay out the whole panel again (after a resize or clear)."""
        t = self.out
        top = t.yview()[0]
        t.configure(state='normal')
        t.delete('1.0', 'end')
        for line in [] if self.entries else HINT:
            self.para('end', line, 'src')
            t.insert('end', '\n')
        for k in self.entries:
            self.add_entry(k)
        t.configure(state='disabled')
        t.yview_moveto(top)

    def add_entry(self, k):
        """Append entry k at the end of the panel, followed by a blank line. It is drawn between the
        marks s{k} and e{k}."""
        t = self.out
        t.insert('end', '\n')
        t.mark_set(f's{k}', 'end-2c')
        t.mark_gravity(f's{k}', 'left')
        t.mark_set(f'e{k}', 'end-2c')
        self.put_entry(k)

    def put_entry(self, k):
        """(Re)draw entry k: each source paragraph followed by its translation, as long as the reply's
        paragraphs line up with the source; otherwise all source paragraphs, then the whole reply."""
        src, dst, failed, done, (path, sel, _) = self.entries[k]
        out = [s for s in re.split(r'\s*\n\s*', dst.strip()) if s]
        seq = [(s, 'src') for s in src]
        if not failed and len(out) <= len(src) and not (done and len(out) < len(src)):
            for i, d in enumerate(out):
                seq.insert(2 * i + 1, (d, 'dst'))
        else:
            seq += [(d, 'err' if failed else 'dst') for d in out]
        t, m = self.out, f'e{k}'
        t.delete(f's{k}', m)
        t.insert(m, f'{os.path.basename(path)} · 第 {sel[0][0] + 1} 页\n', ('link', f'j{k}'))  # click: back to it
        for s, tag in seq:
            self.para(m, s, tag)
            t.insert(m, '\n', tag)

    def on_panel_resize(self, e):
        if e.width != self._pw:
            self._pw = e.width
            if self._relayout:
                self.root.after_cancel(self._relayout)
            self._relayout = self.root.after(100, self.render_panel)

    def para(self, where, text, tag):
        """Insert a justified paragraph at `where`. Tk can't justify, so we break lines ourselves,
        fill each line's spare width with transparent spacer images, and tag our line breaks
        'soft'/'softsp' so copying turns them back into nothing / a space."""
        t, fk = self.out, 'src' if tag == 'src' else 'dst'
        width = max(t.winfo_width() - 2 * sum(int(t.cget(o)) for o in ('padx', 'bd', 'highlightthickness')) - 4, 60)
        space = self.measure(fk, ' ')
        lines, cur, w = [], [], 0
        for unit in self.units(text, fk, width):
            add = unit[2] + (space if unit[0] and cur else 0)
            if cur and w + add > width:
                lines.append((cur, w))
                cur, w, add = [], 0, unit[2]
            cur.append(unit)
            w += add
        lines.append((cur, w))
        for n, (line, w) in enumerate(lines):
            last, gaps, run = n == len(lines) - 1, len(line) - 1, ''
            extra = 0 if last or gaps < 1 else width - w
            tags = (tag, 'end_' + tag) if last else tag
            for j, (sp, u, _) in enumerate(line):
                g = extra * j // gaps - extra * (j - 1) // gaps if j else 0  # this gap's share
                run += ' ' if j and sp else ''
                if g:
                    t.insert(where, run, tags)
                    t.image_create(where, image=self.spacer(g))
                    run = ''
                run += u
            t.insert(where, run, tags)
            if not last:
                t.insert(where, '\n', (tag, 'softsp' if lines[n + 1][0][0][0] else 'soft'))

    def units(self, text, fk, width):
        """Split text into unbreakable pieces (space_before, piece, px). Lines may break at spaces
        and between CJK characters, but not before closing or after opening punctuation."""
        out, sp = [], False
        for tok in re.findall(r'\s+|[⺀-￿]|[^\s⺀-￿]+', text):
            if tok.isspace():
                sp = True
                continue
            if out and not sp and (tok[0] in NO_START or out[-1][1][-1] in NO_END):
                out[-1][1] += tok
            else:  # a space between two CJK characters is dropped
                out.append([sp and not (out and out[-1][1][-1] >= '⺀' and tok[0] >= '⺀'), tok])
            sp = False
        res = []
        for sp, u in out:
            uw = self.measure(fk, u)
            if uw > width:  # wider than a whole line: allow breaks inside it
                res += [(sp and i == 0, ch, self.measure(fk, ch)) for i, ch in enumerate(u)]
            else:
                res.append((sp, u, uw))
        return res

    def measure(self, fk, s):
        w = self.widths.get((fk, s))
        if w is None:
            w = self.widths[fk, s] = self.fonts[fk].measure(s)
        return w

    def spacer(self, n):
        if n not in self.spacers:
            self.spacers[n] = tk.PhotoImage(width=n, height=1)  # a blank photo image is transparent
        return self.spacers[n]

    def copy_panel(self, e=None):
        """Copy the panel's selection without the line breaks added for layout."""
        t, out, on = self.out, [], set()
        if t.tag_ranges('sel'):
            for kind, val, _ in t.dump('sel.first', 'sel.last', text=True, tag=True):
                if kind == 'tagon':
                    on.add(val)
                elif kind == 'tagoff':
                    on.discard(val)
                elif kind == 'text':
                    out.append('' if 'soft' in on else ' ' if 'softsp' in on else val)
            self.root.clipboard_clear()
            self.root.clipboard_append(''.join(out))
        return 'break'


def listen():
    """Take this settings folder's port. None if it is taken: a copy of the app is running."""
    try:
        return socket.create_server(('127.0.0.1', PORT))
    except OSError:
        return None


def handoff(paths):
    """Pass the files to the copy of the app that is running; True if it took them (to open as tabs)."""
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=3) as s:
            s.sendall(json.dumps([os.path.abspath(p) for p in paths]).encode() + b'\n')
            return s.recv(2) == b'ok'
    except OSError:
        return False


def serve(srv):
    """Take files from later launches (e.g. a PDF double-clicked in the file manager)."""
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                conn.settimeout(2)
                INBOX.put(json.loads(conn.makefile(encoding='utf-8').readline()))
                conn.sendall(b'ok')
            except (OSError, ValueError):
                pass


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith('-psn_')]  # older macOS adds a process id
    srv = listen()  # not handoff first: with no copy running, Windows takes a second to say so
    if srv:
        threading.Thread(target=serve, args=(srv,), daemon=True).start()
    elif handoff(paths):  # already running: that window opens the files
        return
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, 'lexitap')  # the installer sees that the app is running
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    try:
        root.state('zoomed')
    except tk.TclError:
        try:
            root.attributes('-zoomed', True)  # Linux
        except tk.TclError:
            root.geometry('1280x800')
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        root.iconbitmap(default=sys.executable)  # the icon is part of the .exe: no separate file
    for icon in (os.path.join(RES, 'icon.png'), os.path.join(HERE, 'packaging', 'icon.png')):  # elsewhere, a picture
        if os.path.exists(icon):
            root.iconphoto(True, tk.PhotoImage(file=icon))
            break
    if sys.platform == 'darwin':  # PDFs opened from Finder arrive as Apple events, not arguments
        root.createcommand('::tk::mac::OpenDocument', lambda *files: INBOX.put(files))
    app = Reader(root, paths, restore=True)
    root.protocol('WM_DELETE_WINDOW', app.close)
    root.mainloop()


if __name__ == '__main__':
    main()
