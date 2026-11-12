"""Test PDFs, generated with PyMuPDF so the tests need no stored files.

two_column(path): a plain two-column paper with a running header, page numbers, headings, a word
    hyphenated at a line end, and sentences that cross a column and a page break.
journal(path): stored in the order journal PDFs use: a sideways watermark first, then two body
    columns (a heading, indented paragraphs, citations glued to full stops, a citation linked to
    the reference list, a display formula), then a figure with small labels and a caption that sits
    at the top of page 1, then the footer. Page 2 continues the text and holds the references.
low_paragraph(path): a column that ends with a paragraph in the bottom margin, where footers usually are.
"""
import re
import pymupdf

FS, LH = 10, 12.6
COLS = [(54, 300), (312, 558)]


def _width(tok, font='tiro', fs=FS):
    return sum(pymupdf.get_text_length(p[2:-1], 'tiro', 6.5) if p.startswith('^{') else
               pymupdf.get_text_length(p, font, fs) for p in re.split(r'(\^\{[^}]*\})', tok) if p)


def _draw(page, x, y, toks, font, fs, links):
    """Draw words; '^{..}' is a superscript glued to the word; a token named in links gets the
    rectangle where it is first drawn."""
    for k, tok in enumerate(toks):
        x += pymupdf.get_text_length(' ', font, fs) if k else 0
        if links.get(tok.rstrip('.,'), 0) is None:
            links[tok.rstrip('.,')] = page.number, pymupdf.Rect(x, y - fs, x + _width(tok, font, fs), y + 2)
        for p in re.split(r'(\^\{[^}]*\})', tok):
            if p.startswith('^{'):
                page.insert_text((x, y - 3.5), p[2:-1], fontsize=6.5, fontname='tiro')
                x += pymupdf.get_text_length(p[2:-1], 'tiro', 6.5)
            elif p:
                page.insert_text((x, y), p, fontsize=fs, fontname=font)
                x += pymupdf.get_text_length(p, font, fs)


def _flow(doc, items, tops, bottom, links=None):
    """Lay out items column after column, page after page. Kinds: 'h' heading, 'p' indented
    paragraph, 'c' unindented text, 'eq' a centred formula, 'r' a reference entry (8.5 pt)."""
    slots, links = [(p, c) for p in range(doc.page_count) for c in range(2)], links if links is not None else {}
    state = {'si': 0, 'y': tops[0]}

    def line(toks, x_off=0, font='tiro', fs=FS, gap=0):
        state['y'] += gap
        if state['y'] > bottom:
            state['si'] += 1
            state['y'] = tops[slots[state['si']][0]]
        p, c = slots[state['si']]
        _draw(doc[p], COLS[c][0] + x_off, state['y'], toks, font, fs, links)
        state['y'] += LH
    for kind, text in items:
        if kind == 'h':
            line(text.split(), font='hebo', fs=10.5, gap=LH)
        elif kind == 'eq':
            line(text.split(), x_off=70, gap=5)
            state['y'] += 5
        else:
            fs, indent, cur = 8.5 if kind == 'r' else FS, 10 if kind == 'p' else 0, []
            for tok in text.split() + [None]:
                if tok is not None and tok.endswith('~'):  # force a hyphenated line break here
                    line(cur + [tok[:-1] + '-'], indent, fs=fs)
                    cur, indent = [], 0
                elif tok is not None and (not cur or _width(' '.join(cur + [tok]), fs=fs) <= 246 - indent):
                    cur.append(tok)
                else:
                    line(cur, indent, fs=fs)
                    cur, indent = [tok], 0


def two_column(path):
    paras = [('h', '1 Introduction'),
             ('p', 'Large language models have changed how people read technical documents. However, reading a '
                   'dense paper in a foreign language is still slow, e.g. when a sentence spans several lines. Prior '
                   'work by Smith et al. (2021) studied this problem in depth. We build a lightweight reader that '
                   'translates whole sentences on demand.'),
             ('h', '2 Method'),
             ('p', 'Our method first extracts every word together with its bounding box. It then groups the selected '
                   'words into sentences using punctuation rules, as shown in Fig. 3 of the appendix. The trans~ '
                   'lation is produced by a large language model that receives the full sentence rather than an '
                   'arbitrary fragment, which greatly improves the quality of the output.')]
    filler = ('Readers often stop in the middle of a long sentence because one clause is unclear. A good tool should '
              'let them recover the full sentence with a single gesture and see the meaning right away. ')
    for k in range(9):
        paras += [('h', f'{k + 3} Experiment {k + 1}'), ('p', filler * 3 + f'This paragraph number {k + 1} ends here.')]
    doc = pymupdf.open()
    for n in range(2):
        pg = doc.new_page(width=612, height=792)
        pg.insert_text((180, 42), 'Published as a conference paper at ICLR 2026', fontsize=8, fontname='tiro')
        pg.insert_text((302, 765), str(n + 1), fontsize=9, fontname='tiro')
    _flow(doc, paras, [80, 80], 720)
    doc.save(path)


def journal(path):
    body = [('h', '1. INTRODUCTION'),
            ('p', 'Characterization of the samples matters as much as making them. Optical methods are fast and '
                  'leave the sample intact.^{40,42-44} The relative orientation of the two layers is then measured '
                  'from the diffraction pattern.^{18} Kim et al. found that two stacked layers with a small twisted '
                  'angle rearranged their atoms (Figure 1c),^{32} which changed the symmetry of the lattice.'),
            ('p', 'As in the first sample, several stacking orders also exist in the second one [2]. The energy of '
                  'each stacking is given by'),
            ('eq', 'E = m × c2 ± 0.5 (3)'),
            ('c', 'where m is the mass of the unit cell.')]
    for n in range(1, 19):
        if n == 5:
            body.append(('h', '2. METHODS'))
        body.append(('p', ' '.join(f'Sentence number {n * 2 + j} explains how the twisted samples were prepared, '
                                   f'transferred and measured in our laboratory with great care.^{{{n * 2 + j}}}'
                                   for j in range(2))))
    body += [('h', 'REFERENCES'), ('r', '[1] A. Author, First Title, J. Test 2019, 1, 1.'),
             ('r', '[2] B. Author, Stacking in Bilayers, J. Test 2020, 2, 2.')]
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=612, height=792)
    for pg in doc:  # 1. a sideways watermark first in the content stream
        pg.insert_text((28, 700), 'Downloaded via EXAMPLE UNIV on May 1, 2024 at 10:00:00 (UTC). See https://'
                       'example.org/sharing for the ways in which this article may be shared.',
                       fontsize=6.5, fontname='helv', rotate=90)
    links = {'[2]': None}
    _flow(doc, body, [352, 80], 712, links)  # 2. the body
    pg = doc[0]  # 3. figure and caption at the top of page 1, stored after the body
    pg.draw_rect(pymupdf.Rect(150, 60, 460, 255), color=(0, 0, 0), width=0.6)
    for (x, y), s in [((156, 74), '(a)'), ((280, 250), 'Energy (eV)'), ((153, 160), 'Reflection'), ((300, 74), '1.6')]:
        pg.insert_text((x, y), s, fontsize=7, fontname='helv')
    pg.insert_text((54, 278), 'Figure 2.', fontsize=8.5, fontname='tibo')
    cap = ('Signatures of the coupled state in twisted samples. (a) Reflection spectrum of a sample with a twist '
           'angle near zero (light blue, top) and of one with a large twist angle (black, bottom). '
           'Reprinted with permission from ref 47. Copyright 2019 Example Press. (b) Sketch of the sample with '
           'three layers: two of one material on top of one of another.').split()
    y, cur, x0 = 278, [], 54 + pymupdf.get_text_length('Figure 2. ', 'tibo', 8.5)
    for tok in cap + [None]:
        if tok is not None and pymupdf.get_text_length(' '.join(cur + [tok]), 'tiro', 8.5) <= 558 - x0:
            cur.append(tok)
            continue
        pg.insert_text((x0, y), ' '.join(cur), fontsize=8.5, fontname='tiro')
        cur, y, x0 = [tok], y + 10.5, 54
    for n, pg in enumerate(doc):  # 4. the footer last
        pg.insert_text((54, 768), str(5946 + n), fontsize=8, fontname='tibo')
        pg.insert_text((230, 768), 'https://doi.org/10.1234/example.2021', fontsize=7, fontname='helv')
        pg.insert_text((470, 768), 'J. Example 2021, 15, 5944-5958', fontsize=7, fontname='tiit')
    target = doc[1].search_for('[2] B. Author')[0]  # the citation [2] (on page 1) links to its reference entry
    pg = doc[0]  # one page object for both links: a second one would write back a stale link list
    pg.insert_link({'kind': pymupdf.LINK_GOTO, 'from': links['[2]'][1], 'page': 1, 'to': target.tl})
    pg.insert_link({'kind': pymupdf.LINK_URI, 'from': pymupdf.Rect(230, 760, 380, 770),
                    'uri': 'https://doi.org/10.1234/example.2021'})
    doc.set_metadata({'title': 'Moiré Patterns in Stacked Layers: An Overview'})
    doc.save(path)


def low_paragraph(path):
    """Page 1: under a running header in the body's font, a left column whose last paragraph lies wholly
    in the bottom margin strip; its sentence goes on at the top of the right column. Page 2: the same
    header and little else."""
    doc = pymupdf.open()
    for n in range(2):
        pg = doc.new_page(width=612, height=792)
        lines = [(54, 62, f'Journal of Invented Results {n + 12} (2026)'), (302, 776, str(n + 1)),
                 (54, 86, 'The second page says little.')]
        if n == 0:
            lines[2:] = [(54, 86 + LH * i, f'Sample {i + 11} of the first group holds water far too firmly.') for i in range(50)]
            lines += [(54, 716, 'We did not study that group here.'), (64, 738, 'In the second group the hydrogen atoms'),
                      (54, 738 + LH, 'of every ring'), (312, 86, 'keep the oxygen atoms in place.'),
                      (312, 86 + LH, 'That group is studied below.')]
        for x, y, s in lines:
            pg.insert_text((x, y), s, fontsize=FS, fontname='tiro')
    doc.save(path)


if __name__ == '__main__':
    two_column('two_column.pdf')
    journal('journal.pdf')
    low_paragraph('low_paragraph.pdf')
