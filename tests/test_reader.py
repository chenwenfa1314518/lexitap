"""Tests for lexitap.py. Run: python tests/test_reader.py   (exit code 1 if anything fails)

Builds its own test PDFs, talks to a local fake API, keeps settings in a temporary folder (your
real settings and translation cache are never touched) and opens a real window. On a Linux server
without a screen, run it with xvfb-run."""
import json, os, shutil, sys, tempfile, threading, time, unicodedata

TESTS = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix='lexitap-test-')
os.environ['LEXITAP_HOME'] = TMP
sys.path[:0] = [os.path.dirname(TESTS), TESTS]
sys.stdout.reconfigure(errors='backslashreplace')  # a console that can't show Chinese must not stop the tests
import tkinter as tk
from types import SimpleNamespace as Ev
import pymupdf
import lexitap as pr
import make_pdfs
import mock_llm

PAPER, JOURNAL = os.path.join(TMP, 'two_column.pdf'), os.path.join(TMP, 'journal.pdf')
make_pdfs.two_column(PAPER)
make_pdfs.journal(JOURNAL)
API = mock_llm.start()
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'\n   got : {got!r}\n   want: {want!r}'))


def wait(ms, until=lambda: False):
    """Let the window run for ms milliseconds, or until until() is true."""
    end = time.time() + ms / 1000
    while time.time() < end and not until():
        root.update()
        time.sleep(0.01)


def translated():
    wait(5000, lambda: r.entries[r.jobs][3])  # the latest translation is done


def find(word, page=None, nth=0):
    for p in range(r.doc.page_count) if page is None else [page]:
        for i, w in enumerate(r.words(p)):
            if w[4] == word:
                if nth == 0:
                    return p, i
                nth -= 1
    raise KeyError(word)


def xy(pos):
    w = r.word(pos)
    return Ev(x=r.lefts[pos[0]] + (w[0] + w[2]) / 2 * r.zoom - c.canvasx(0),
              y=r.tops[pos[0]] + (w[1] + w[3]) / 2 * r.zoom - c.canvasy(0), state=0)


def sentence(word, page=None, formulas=False):
    pos = find(word, page)
    r.cls = r.word(pos)[8]
    return r.text(*r.expand(pos, pos), formulas=formulas)


def order(k):
    """Panel entry k's paragraphs in order: S = source, T = translation."""
    return ''.join('S' if v == 'end_src' else 'T' for kind, v, _ in t.dump(f's{k}', f'e{k}', tag=True)
                   if kind == 'tagon' and v in ('end_src', 'end_dst'))


def drag(a, b):
    r.on_press(xy(a))
    for f in (0.3, 0.6, 1.0):
        e0, e1 = xy(a), xy(b)
        r.on_motion(Ev(x=e0.x + (e1.x - e0.x) * f, y=e0.y + (e1.y - e0.y) * f, state=0))
    r.on_release(xy(b))
    return r.text(*r.sel)


root = tk.Tk()
root.geometry('1300x860+20+20')
print('Python', sys.version.split()[0], '| Tk', root.tk.call('info', 'patchlevel'), '|', sys.platform)
errors = []
root.report_callback_exception = lambda *exc: errors.append(exc[1])
r = pr.Reader(root)
r.cfg.update(base_url=API + '/v1', api_key='sk-test', glossary='twisted angle = 扭转角\nexciton = 激子')
c, t = r.canvas, r.out
wait(400)

# ---------- sentences ----------
r.open(PAPER)
wait(300)
check('e.g. + lowercase word', sentence('spans'),
      'However, reading a dense paper in a foreign language is still slow, e.g. when a sentence spans several lines.')
check('et al.', sentence('Smith'), 'Prior work by Smith et al. (2021) studied this problem in depth.')
check('Fig. 3', sentence('appendix.'),
      'It then groups the selected words into sentences using punctuation rules, as shown in Fig. 3 of the appendix.')
check('heading not glued to the sentence', sentence('Large'),
      'Large language models have changed how people read technical documents.')
check('word hyphenated at a line end', sentence('lation'), 'The translation is produced by a large language model that '
      'receives the full sentence rather than an arbitrary fragment, which greatly improves the quality of the output.')
last = (0, max(i for i, w in enumerate(r.words(0)) if w[8][2] == 'body'))
r.cls = r.word(last)[8]
a, b = r.expand(last, last)
check('sentence across the page break', (a[0], b[0], r.text(a, b)), (0, 1, 'A good tool should let them recover the '
      'full sentence with a single gesture and see the meaning right away.'))

# ---------- drawing ----------
(tw, th), z = pr.TILE, r.zoom
full = r.doc[0].get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)


def same(k):
    """Is the tile where it should be, and the same picture as that part of the whole page?"""
    pix = r.tile(*k)
    x, y, n = pix.x, pix.y, pix.width * 3
    return (x, y) == (k[2] * tw, k[1] * th) and all(
        pix.samples[row * n:(row + 1) * n] == full.samples[((y + row) * full.width + x) * 3:][:n] for row in range(pix.height))


inview = {(0, j, i) for j in range((min(c.winfo_height() - pr.GAP, int(r.rects[0].height * z)) - 1) // th + 1)
          for i in range((int(r.rects[0].width * z) - 1) // tw + 1)}
check('pages are drawn as tiles that cover the view and fit together seamlessly',
      (inview <= set(r.tiles), all(same(k) for k in r.tiles if k[0] == 0)), (True, True))
a = find('Smith')
r.anchor, r.cls, r.sel = a, r.word(a)[8], (a, find('al.'))
r.scroll_px(300)
wait(300)
v = r.view()
r.set_zoom(8)
wait(300)
stack = list(c.find_all())
check('highest zoom: same place, only the part around the view is drawn, the selection stays on top',
      (r.zoom, r.view()[0], abs(r.view()[1] - v[1]) < 1, 0 < len(r.tiles) <= (c.winfo_width() // tw + 2) * (3 * c.winfo_height() // th + 2),
       min(stack.index(i) for i in c.find_withtag('sel')) > max(stack.index(i) for _, i in r.tiles.values())),
      (8.0, 0, True, True, True))
r.sel = None
r.set_zoom(None)
wait(300)

# ---------- kinds of text in a journal page ----------
r.open(JOURNAL)
wait(300)
kinds = {w[4]: w[8] for p in range(2) for w in r.words(p)}
check('caption, footer, sideways watermark', (kinds['Figure'][2], kinds['5946'][2], kinds['Downloaded'][0]),
      ('caption', 'margin', (0.0, -1.0)))
body = [i for i, w in enumerate(r.words(0)) if w[8] == kinds['Characterization']]
r.cls = kinds['Characterization']
a, b = r.expand((0, body[-1]), (0, body[-1]))
s = r.text(a, b)
check('page-break sentence skips caption, labels, footer, watermark',
      (b[0], s.startswith('Sentence number'), [j for j in ('Signatures', 'Energy', 'Downloaded', '5946') if j in s]),
      (1, True, []))
check('citation glued to the full stop', sentence('pattern.18'),
      'The relative orientation of the two layers is then measured from the diffraction pattern.18')
check('caption sentence', sentence('Reprinted'), 'Reprinted with permission from ref 47.')
check('watermark on its own', sentence('https://example.org/sharing'),
      'See https://example.org/sharing for the ways in which this article may be shared.')
check('formula becomes [公式] for translation', sentence('energy', formulas=True),
      'The energy of each stacking is given by [公式] where m is the mass of the unit cell.')
check('title for context', r.paper_title(), 'Moiré Patterns in Stacked Layers: An Overview')

# ---------- API client ----------
cfg = dict(pr.DEFAULTS, base_url=API + '/v1', api_key='k', model='m1', title='A Paper',
           glossary='moiré superlattice = 莫尔超晶格\nexciton = 激子')
reply = ''.join(pr.llm_stream(cfg, 'The moiré superlattice is nice.\nSecond paragraph.'))
system = mock_llm.Handler.last[2]['messages'][0]['content']
check('streamed reply', reply.startswith('【译1】'), True)
check('prompt: title, used glossary terms only, keep paragraphs',
      ('A Paper' in system, '莫尔超晶格' in system, '激子' in system, '保持同样的分段' in system), (True, True, False, True))
check('one JSON answer instead of a stream', ''.join(pr.llm_stream(dict(cfg, base_url=API + '/plain'), 'Hi.')), '【译1】Hi.')
for name, url, want in [('HTTP error message', '/bad', 'HTTP 401：Authentication Fails, your api key is invalid'),
                        ('error inside the stream', '/sseerr', '{"message": "rate limit reached"}')]:
    try:
        list(pr.llm_stream(dict(cfg, base_url=API + url), 'x'))
        check(name, 'no error', want)
    except RuntimeError as e:
        check(name, str(e), want)
sent = {}
real, pr.send = pr.send, lambda req: sent.update(r=req) or 1 / 0
try:
    list(pr.llm_stream(dict(pr.DEFAULTS, api_key='k'), 'x'))
except ZeroDivisionError:
    pass
pr.send = real
check('DeepSeek: thinking switched off', (sent['r'].full_url, json.loads(sent['r'].data)['thinking']),
      ('https://api.deepseek.com/chat/completions', {'type': 'disabled'}))

saved = {k: os.environ.pop(k) for k in list(os.environ) if k.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
os.environ['http_proxy'] = API  # a system proxy: the fake API plays it too, and then sees the whole address
pr.DIRECT.clear()
list(pr.llm_stream(cfg, 'Hi.'))
check('system proxy set: the API is reached without it', mock_llm.Handler.last[0], '/v1/chat/completions')
reply = ''.join(pr.llm_stream(dict(cfg, base_url='http://127.0.0.1:1/v1'), 'Hi.'))  # nothing listens there
check('...and through the proxy if it cannot be reached directly', (reply, mock_llm.Handler.last[0]),
      ('【译1】Hi.', 'http://127.0.0.1:1/v1/chat/completions'))
reply = ''.join(pr.llm_stream(dict(cfg, base_url=API + '/geo'), 'Hi.'))
check('...or if it refuses requests from here (HTTP 403)', (reply, mock_llm.Handler.last[0]),
      ('【译1】Hi.', API + '/geo/chat/completions'))
for name, url, proxy, want in [('a wrong key is reported at once, without asking again through the proxy', '/bad', API, ('HTTP 401', 1)),
                               ('both ways fail: the answer of the server is reported', '/geo', 'http://127.0.0.1:1', ('HTTP 403', 1))]:
    os.environ['http_proxy'], n = proxy, mock_llm.Handler.count
    try:
        list(pr.llm_stream(dict(cfg, base_url=API + url), 'x'))
        check(name, 'no error', want)
    except RuntimeError as e:
        check(name, (str(e)[:8], mock_llm.Handler.count - n), want)
del os.environ['http_proxy']
os.environ.update(saved)

# ---------- translation panel ----------
r.on_double(xy(find('Kim')))
translated()
k = r.jobs
src, dst, failed, done, origin = r.entries[k]
check('double-click translates the sentence', (src, dst.startswith('【译1】'), done), (['Kim et al. found that two stacked '
      'layers with a small twisted angle rearranged their atoms (Figure 1c),32 which changed the symmetry of the '
      'lattice.'], True, True))
system = mock_llm.Handler.last[2]['messages'][0]['content']
check('request carries the paper title and the matching glossary term',
      ('Moiré Patterns in Stacked Layers' in system, '扭转角' in system, '激子' in system), (True, True, False))
width = t.winfo_width() - 2 * sum(int(t.cget(o)) for o in ('padx', 'bd', 'highlightthickness')) - 4
full, bad = set(), []
for i in range(1, int(t.index('end-1c').split('.')[0]) + 1):
    if {'soft', 'softsp'} & set(t.tag_names(f'{i}.end')):
        full.add(t.count(f'{i}.0', f'{i}.end', 'xpixels')[0])
    if t.get(f'{i}.0') in pr.NO_START:
        bad.append(i)
off = max(abs(w - width) for w in full)  # text is measured piece by piece: some systems round a line differently
check(f'panel lines justified to the full width (off by {off} px at most), none starts with closing punctuation',
      (off <= 2, bad), (True, []))
t.tag_add('sel', f's{k}', f'e{k}')
r.copy_panel()
check('copying an entry gives its text without layout line breaks', root.clipboard_get(),
      f'journal.pdf · 第 1 页\n{src[0]}\n{dst}\n')
t.tag_remove('sel', '1.0', 'end')
before = mock_llm.Handler.count
r.on_double(xy(find('Kim')))
translated()
check('a sentence translated before comes from the cache', mock_llm.Handler.count, before)
r.cache = pr.load_json(pr.CACHE_FILE, {})
r.on_double(xy(find('Kim')))
translated()
check('...also after a restart (cache file on disk)', (mock_llm.Handler.count, len(r.cache) > 0), (before, True))
paras = drag(find('2.'), find('care.11')).split('\n')
translated()
check('heading kept as its own paragraph, translated paragraph by paragraph',
      (paras[0], len(paras), order(r.jobs)), ('2. METHODS', 2, 'STST'))
mock_llm.Handler.keep = 1  # the model merges paragraphs: all source first, then the reply
r.cache.clear()
r.finish()
translated()
check('merged reply: source paragraphs first, then the reply', order(r.jobs), 'SST')
mock_llm.Handler.keep = 99

# ---------- reading: next sentence, back to the source, search ----------
r.on_double(xy(find('Kim')))
translated()
r.next_sentence(1)
translated()
check('→ selects and translates the next sentence', r.text(*r.sel), 'As in the first sample, several stacking '
      'orders also exist in the second one [2].')
r.next_sentence(-1)
check('← goes back a sentence', r.text(*r.sel)[:25], 'Kim et al. found that two')
k = r.jobs
r.open(PAPER)
t.see(f's{k}')
root.update()
x, y, _, _ = t.bbox(f's{k}')
r.on_entry_link(Ev(x=x + 3, y=y + 3))
check('clicking an entry\'s "file · page" line shows that text in its PDF again',
      (os.path.basename(r.path), r.text(*r.sel)[:25]), ('journal.pdf', 'Kim et al. found that two'))
r.fvar.set('twisted')
r.find(1)
first = r.fcur
r.find(1)
r.find(-1)
check('search: next and previous match', (r.fcur, r.fstat.cget('text').startswith('第')), (first, True))
r.fvar.set('no such words here')
r.find(1)
check('search: nothing found', r.fstat.cget('text'), '无结果')
ACCENTS = os.path.join(TMP, 'accents.pdf')  # as in many journals: "é" is stored as "e" and an accent drawn a little higher
doc = pymupdf.open()
tw, font, pos = pymupdf.TextWriter(doc.new_page().rect), pymupdf.Font('helv'), pymupdf.Point(72, 100)
for piece in unicodedata.normalize('NFD', 'The moiré pattern of a moiré superlattice, in a résumé.').replace('\u0301', '|\u0301|').split('|'):
    pos = pymupdf.Point(tw.append(pos - (0, 1.1) if piece == '\u0301' else pos, piece, font=font)[1].x, pos.y)
tw.write_text(doc[0])
doc.save(ACCENTS)
r.open(ACCENTS)
found = []
for q in ('moiré', unicodedata.normalize('NFD', 'moiré'), 'résumé', 'moire'):
    r.fvar.set(q)
    r.find(1)
    found.append(len(r.hits(0)))
check('search finds accented words however the PDF stores the accent, one match per word', found, [2, 2, 1, 2])
r.close_tab(r.cur)
r.end_find()

# ---------- links ----------
cite = find('[2].')
link = r.link_at(xy(cite).x, xy(cite).y)
check('citation link: target is its entry in the reference list', (r.target(*link)[0], r.target(*link)[1] is not None),
      (1, True))
r.preview(*link)
root.update()
check('hovering shows a preview', r.tip is not None and r.tip.winfo_children()[0].cget('image') != '', True)
r.hide_tip()
start = r.view()
r.follow(*link)
check('clicking jumps to the reference list', r.view()[0], 1)
r.go_back()
check('Alt+← comes back', r.view(), start)
url = next(l for l in r.links(0) if l['kind'] == pymupdf.LINK_URI)
r.preview(0, url)
check('a web link shows its address', r.tip.winfo_children()[0].cget('text'), 'https://doi.org/10.1234/example.2021')
r.hide_tip()

# ---------- tabs, session, second launch ----------
r.switch(0)
c.yview_moveto(0.4)
root.update()
view = r.view()
r.switch(1)
r.open(PAPER)
check('tabs: reopening an open file shows its tab, at the same place', (len(r.tabs), r.cur, r.view()), (2, 0, view))
r.save_session()
saved, real = open(pr.CFG_FILE, encoding='utf-8').read(), json.dump
json.dump = lambda *a, **k: 1 / 0  # the app dies in the middle of writing its settings
try:
    pr.save_cfg(r.cfg)
except ZeroDivisionError:
    pass
json.dump = real
check('a save that fails half-way leaves the settings file as it was', open(pr.CFG_FILE, encoding='utf-8').read(), saved)
while r.tabs:
    r.close_tab(r.cur)
check('closing all tabs shows the start screen', (r.doc, root.title()), (None, 'LexiTap'))
r.restore()
r.show_tab()
check('session: the tabs come back at the same place', ([os.path.basename(x['path']) for x in r.tabs], r.view()[0]),
      (['two_column.pdf', 'journal.pdf'], view[0]))
srv = pr.listen()  # the port follows the temporary data folder
threading.Thread(target=pr.serve, args=(srv,), daemon=True).start()
check('one window per settings folder: a second copy cannot take its port', pr.listen(), None)
r.close_tab(1)
check('a second launch hands its file to the open window', pr.handoff([JOURNAL]), True)
wait(5000, lambda: len(r.tabs) == 2)
check('...which opens it as a tab', [os.path.basename(x['path']) for x in r.tabs], ['two_column.pdf', 'journal.pdf'])

# ---------- panel closed: selecting only selects ----------
n = len(r.entries)
r.switch(0)
r.panel.set(False)
r.toggle_panel()
drag(find('Smith', 0), find('al.', 0))
check('panel closed: exact selection, no translation', (r.text(*r.sel), len(r.entries)), ('Smith et al.', n))
r.panel.set(True)
r.toggle_panel()

# ---------- closing the window ----------
r.close_tab(1)
closed = []
root.destroy = lambda real=root.destroy: (closed.append(1), real())
r.close()
check('closing the window saves the open tabs', (len(closed), len(pr.load_cfg()['session']['tabs'])), (1, 1))

print('callback errors:', len(errors), *errors)
print('FAILURES:', fails + len(errors))
for tab in r.tabs:
    tab['doc'].close()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails or errors else 0)
