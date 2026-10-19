"""구글 드라이브 폴더 자동 편집: 설교 원고(docx) 맞춤 자막 + 무음 구간 컷 -> MP4.

사용법
  python autopipe.py --watch "G:\\내 드라이브\\설교영상"   # 폴더 감시 (Google Drive 데스크톱 동기화 폴더)
  python autopipe.py --file 영상.mp4 [--script 원고.docx]  # 파일 1개 처리

영상과 같은 이름의 docx/txt(예: 0119설교.mp4 + 0119설교.docx)가 있으면 원고 글자로 자막을 만든다.
감시 폴더에 새 영상이 올라오면 `<감시폴더>/output/` 에 아래 파일을 만든다.
  <이름>_편집.mp4     무음 컷 + 자막이 입혀진 영상
  <이름>_자막.srt     자막 (컷 이후 타임라인 기준)
  <이름>_리포트.txt   일치율·자막에서 빠진 원고 구간 (검수용)
"""
import sys, os, re, json, time, argparse, shutil, subprocess, threading, uuid, zipfile, html, difflib

import numpy as np

import server
from server import FFMPEG, NOWIN, MEDIA, DATA, CACHE, EXPORTS, make_srt, subtitles_filter, register_media, \
    decode_pcm, media_file, run_transcribe

VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.m4v', '.avi', '.webm', '.mts', '.m2ts'}
LEDGER = os.path.join(DATA, 'auto_done.json')

# ---------------------------------------------------------------- 성경 말씀 감지
BOOKS = ['창세기', '출애굽기', '레위기', '민수기', '신명기', '여호수아', '사사기', '룻기', '사무엘상', '사무엘하',
         '열왕기상', '열왕기하', '역대상', '역대하', '에스라', '느헤미야', '에스더', '욥기', '시편', '잠언', '전도서',
         '아가', '이사야', '예레미야애가', '예레미야', '에스겔', '다니엘', '호세아', '요엘', '아모스', '오바댜',
         '요나', '미가', '나훔', '하박국', '스바냐', '학개', '스가랴', '말라기', '마태복음', '마가복음', '누가복음',
         '요한복음', '사도행전', '로마서', '고린도전서', '고린도후서', '갈라디아서', '에베소서', '빌립보서',
         '골로새서', '데살로니가전서', '데살로니가후서', '디모데전서', '디모데후서', '디도서', '빌레몬서',
         '히브리서', '야고보서', '베드로전서', '베드로후서', '요한일서', '요한이서', '요한삼서', '유다서', '요한계시록']
_NUM = r'(?:\d+|[일이삼사오육칠팔구십백]+)'
REF_RE = re.compile(
    r'(' + '|'.join(sorted(BOOKS, key=len, reverse=True)) + r')\s*(' + _NUM + r')\s*(?:장|편)'
    r'(?:\s*(' + _NUM + r')\s*절)?(?:\s*(?:부터|에서|~|-|–)\s*(' + _NUM + r')\s*절?)?')
CLOSE_RE = re.compile(r'(말씀입니다|말씀이에요|말씀하셨습니다|하셨습니다|하였습니다|기록되어 있습니다|아멘)\s*[.!?]*\s*$')
_KD = dict(zip('일이삼사오육칠팔구', range(1, 10)))


def kor_num(s):
    if s.isdigit():
        return int(s)
    total = cur = 0
    for ch in s:
        if ch in _KD:
            cur = _KD[ch]
        elif ch in '십백':
            total += (cur or 1) * (10 if ch == '십' else 100)
            cur = 0
    return total + cur


def find_verse_regions(segs, per_verse=15.0):
    """'요한복음 3장 16절' 같은 장절 언급을 찾고, 이어지는 낭독 구간까지를 말씀 구간으로 본다."""
    regions = []
    i = 0
    while i < len(segs):
        m = REF_RE.search(segs[i]['t'])
        if not m:
            i += 1
            continue
        v1 = kor_num(m.group(3)) if m.group(3) else 0
        v2 = kor_num(m.group(4)) if m.group(4) else 0
        n = min(12, v2 - v1 + 1) if v1 and v2 >= v1 else 1
        label = f"{m.group(1)} {kor_num(m.group(2))}{'편' if m.group(1) == '시편' else '장'}"
        if v1:
            label += f" {v1}절" + (f"~{v2}절" if v2 > v1 else '')
        start = segs[i]['s']
        budget = start + per_verse * n + 6
        last = i
        k = i + 1
        while k < len(segs) and segs[k]['e'] <= budget:
            gap = segs[k]['s'] - segs[k - 1]['e']
            if gap >= (4.0 if k == i + 1 else 1.8) or REF_RE.search(segs[k]['t']):
                break
            last = k
            if CLOSE_RE.search(segs[k]['t']):
                break
            k += 1
        regions.append(dict(ref=label, s=start, e=segs[last]['e'], first=i, last=last, hasVerse=bool(v1)))
        i = last + 1
    return regions


def split_text(t, maxc=22):
    lines, cur = [], ''
    for w in t.split():
        if cur and len(cur) + 1 + len(w) > maxc:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)
    return lines


def chunk_sub(seg, maxc=22):
    """긴 문장을 글자 수 비례로 시간 분할해 읽기 좋은 자막 조각으로 만든다."""
    parts = split_text(seg['t'], maxc)
    total = sum(len(p) for p in parts) or 1
    t, out = seg['s'], []
    for p in parts:
        d = (seg['e'] - seg['s']) * len(p) / total
        out.append(dict(s=t, e=t + d, t=p))
        t += d
    return out


# ---------------------------------------------------------------- 설교 원고(docx/txt) 맞춤 자막
CPS = 0.18  # 한글 한 글자당 말하는 시간(초) 추정값. 맞춘 글자가 없는 부분의 시간 보정에만 쓴다.
QUOTE_RE = re.compile(r'[“"]([^“”"]{6,}?)[”"]')
SENT_END_RE = re.compile(r'[.?!。…][”"’\')\]]*$')


def read_script(path):
    """docx(또는 txt)에서 문단 목록을 읽는다. 표 안의 글도 포함."""
    if path.lower().endswith('.docx'):
        with zipfile.ZipFile(path) as z:
            xml = z.read('word/document.xml').decode('utf-8')
        paras = []
        for p in xml.split('</w:p>'):
            p = re.sub(r'<w:(?:tab|br)\b[^>]*/>', '<w:t> </w:t>', p)
            t = ''.join(html.unescape(x) for x in re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', p))
            if t.strip():
                paras.append(t.strip())
        return paras
    with open(path, encoding='utf-8-sig') as f:
        return [ln.strip() for ln in f if ln.strip()]


def script_words(paras):
    """문단 -> [(단어, 문장끝 여부, 성경 인용 여부)]. 장절 언급 바로 뒤의 따옴표 인용을 성경 본문으로 본다."""
    words = []
    for para in paras:
        spans = []
        for m in QUOTE_RE.finditer(para):
            pre = para[max(0, m.start() - 60):m.start()]
            if REF_RE.search(pre) or re.search(r'\d+\s*절', pre):
                spans.append((m.start(), m.end()))
        start = len(words)
        for m in re.finditer(r'\S+', para):
            w = m.group()
            words.append([w, bool(SENT_END_RE.search(w)), any(a <= m.start() < b for a, b in spans)])
        if len(words) > start:
            words[-1][1] = True
    return words


def norm(s):
    return ''.join(ch for ch in s.lower() if ch.isalnum())


def make_cues(words, maxc=18, minflush=14):
    """단어를 한 줄 maxc자, 최대 2줄짜리 자막 조각으로 묶는다. 문장이 끝나면 되도록 거기서 끊는다."""
    cues, lines, cur = [], [], ''
    nv = nw = 0

    def emit(ls):
        nonlocal nv, nw
        t = '\n'.join(ls)
        cues.append(dict(t=t, n=norm(t), verse=nv * 2 >= nw))
        nv = nw = 0

    for w, end, inv in words:
        if cur and len(cur) + 1 + len(w) > maxc:
            lines.append(cur)
            cur = ''
            if len(lines) == 2:
                emit(lines)
                lines = []
        cur = (cur + ' ' + w).strip()
        nw += 1
        nv += bool(inv)
        if end and len(lines) * maxc + len(cur) >= minflush:
            lines.append(cur)
            cur = ''
            emit(lines)
            lines = []
    if cur:
        lines.append(cur)
    if lines:
        emit(lines)
    return [c for c in cues if c['n']]


def transcript_stream(segs):
    """위스퍼 결과를 글자 단위(정규화) + 글자별 시작/끝 시간으로 펼친다. 단어 시간이 있으면 그것을 쓴다."""
    ch, ts, te = [], [], []
    for sg in segs:
        items = [(w[0], w[1], w[2]) for w in sg['w']] if sg.get('w') else [(sg['s'], sg['e'], sg['t'])]
        for a, b, txt in items:
            n = norm(txt)
            if not n:
                continue
            d = (b - a) / len(n)
            for k, c in enumerate(n):
                ch.append(c)
                ts.append(a + k * d)
                te.append(a + (k + 1) * d)
    return ''.join(ch), np.array(ts), np.array(te)


def robust_blocks(raw, long_n=6, tol=300, near=12):
    """difflib 일치 블록에서 우연히 맞은 짧은 블록(반복 문구 등)을 걸린다.
    긴 블록들의 대표 오프셋(j-i)에서 크게 벗어난 블록은 버리고, 짧은 블록은 이웃 긴 블록과 오프셋이 거의 같을 때만 남긴다."""
    big = [b for b in raw if b[2] >= long_n]
    if not big:
        return []
    ds = sorted(((b[1] - b[0]), b[2]) for b in big)
    half, acc, dmed = sum(n for _, n in ds) / 2, 0, ds[0][0]
    for d, n in ds:
        acc += n
        if acc >= half:
            dmed = d
            break
    kept = [b for b in big if abs((b[1] - b[0]) - dmed) <= tol]
    if not kept:
        return []
    for b in raw:
        if b[2] < long_n:
            nb = min(kept, key=lambda k: abs(k[0] - b[0]))
            if abs((b[1] - b[0]) - (nb[1] - nb[0])) <= near and b[2] >= 2:
                kept.append(b)
    kept.sort()
    return kept


def align_cues(cues, segs, dur):
    """원고 자막 조각 <-> 음성 인식 글자를 맞춰 각 조각의 시간을 정한다.
    글자(3글자 묶음) 일치 구간을 기준점으로 삼고, 일치가 적은 조각은 앞뒤 기준점 사이 시간으로 추정한다.
    반환: (자막 목록[s,e,t,verse], 통계)"""
    T, ts, te = transcript_stream(segs)
    offs, S = [], ''
    for c in cues:
        offs.append(len(S))
        S += c['n']
    match = np.full(len(S), -1, dtype=np.int64)
    # 원고를 앞에서부터 ~400자씩 끊어, 음성 글자열의 "지금 위치 근처"에서만 찾는다(반복 문구에 휘둘리지 않게).
    # 못 찾는 구간이 이어지면 탐색 범위를 넓힌다(설교자가 크게 건너뛴 경우 대비).
    pos, fails, i0 = 0, 0, 0
    while i0 < len(S) and len(T) >= 3:
        i1 = min(len(S), i0 + 400)
        sb = S[i0:i1]
        reach = int(len(sb) * 2 + 800) * (2 ** fails)
        j0, j1 = max(0, pos - 100), min(len(T), pos + reach)
        got = 0
        if len(sb) >= 3 and j1 - j0 >= 3:
            ta = [sb[i:i + 3] for i in range(len(sb) - 2)]
            tb = [T[j:j + 3] for j in range(j0, j1 - 2)]
            raw = [b for b in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_matching_blocks() if b[2]]
            blocks = robust_blocks(raw)
            got = sum(b[2] + 2 for b in blocks)
            if got >= 0.25 * len(sb):
                for i, j, n in blocks:
                    match[i0 + i:i0 + i + n + 2] = np.arange(j0 + j, j0 + j + n + 2)
                pos = j0 + blocks[-1][1] + blocks[-1][2] + 2
                fails = 0
            else:
                got = 0
        if not got:
            fails = min(fails + 1, 4)
        i0 = i1
    anch = {}  # 조각 번호 -> (시작, 끝, 일치율)
    for k, c in enumerate(cues):
        L = len(c['n'])
        m = match[offs[k]:offs[k] + L]
        hit = np.nonzero(m >= 0)[0]
        frac = len(hit) / max(L, 1)
        if frac >= 0.3:
            f, l = int(hit[0]), int(hit[-1])
            anch[k] = (float(ts[m[f]]) - f * CPS, float(te[m[l]]) + (L - 1 - l) * CPS, frac)
    res = [None] * len(cues)
    for k, (a, b, fr) in anch.items():
        res[k] = [a, b, 'm']
    interp = dropped = 0
    k = 0
    while k < len(cues):  # 일치가 적은 연속 구간: 앞뒤 기준점 사이에 글자 수 비례로 배치(시간 여유가 맞을 때만)
        if k in anch:
            k += 1
            continue
        j = k
        while j < len(cues) and j not in anch:
            j += 1
        lo = res[k - 1][1] if k > 0 and res[k - 1] else 0.0
        hi = res[j][0] if j < len(cues) and res[j] else dur
        tot = sum(len(cues[x]['n']) for x in range(k, j))
        exp = tot * CPS
        win = hi - lo
        if tot and 0.6 * exp <= win <= 2.0 * exp + 2:
            t = lo
            for x in range(k, j):
                d = win * len(cues[x]['n']) / tot
                res[x] = [t, t + d, 'i']
                t += d
                interp += 1
        else:
            dropped += j - k
        k = j
    out = []
    prev_end = 0.0
    live = [x for x in range(len(cues)) if res[x]]
    for idx, x in enumerate(live):
        a, b, kind = res[x]
        nxt = res[live[idx + 1]][0] if idx + 1 < len(live) else dur
        a = max(a, prev_end, 0.0)
        b = min(max(b, a + 0.8), nxt if nxt > a + 0.3 else b, dur)
        if b - a < 0.3:
            dropped += 1
            continue
        out.append(dict(s=round(a, 2), e=round(b, 2), t=cues[x]['t'], verse=cues[x]['verse'], kind=kind, k=x))
        prev_end = b
    stats = dict(cues=len(cues), matched=len(anch), interp=interp, dropped=dropped,
                 script_cov=float((match >= 0).mean()) if len(S) else 0.0,
                 speech_cov=float(len(set(match[match >= 0].tolist())) / max(len(T), 1)))
    return out, stats


def find_script(video_path):
    stem = os.path.splitext(video_path)[0]
    for ext in ('.docx', '.DOCX', '.txt', '.TXT'):
        if os.path.isfile(stem + ext):
            return stem + ext
    return None


def get_segments(mid, m, model, lang, words, log):
    """위스퍼 분석 결과를 영상 해시+모델별로 저장해 두고 재사용한다(재실행 시 오래 걸리지 않게)."""
    cp = os.path.join(CACHE, f"tr_{m['hash']}_{model}_{'w' if words else 's'}.json")
    if os.path.exists(cp):
        log('  저장된 음성 분석 결과를 재사용합니다')
        with open(cp, encoding='utf-8') as f:
            return json.load(f)
    job = dict(msg='', progress=0.0, result=None)
    run_transcribe(job, mid, lang, model, words)
    segs = job['result']['subs']
    with open(cp, 'w', encoding='utf-8') as f:
        json.dump(segs, f, ensure_ascii=False)
    return segs


# ---------------------------------------------------------------- 무음 컷
def speech_ranges(path, dur, segs, min_silence=0.8, pad=0.25):
    """에너지 기반 무음 탐지. 위스퍼가 말한 구간은 항상 살린다(단어 시간이 있으면 단어 단위, 없으면 문장 단위).
    반환: [(시작, 끝)] 초."""
    pcm = decode_pcm(path, 16000, 1)
    fr = 320  # 20ms
    n = len(pcm) // fr
    if n == 0:
        return [(0.0, dur)]
    rms = np.sqrt((pcm[:n * fr].reshape(n, fr) ** 2).mean(axis=1))
    db = 20 * np.log10(rms + 1e-7)
    lo, hi = np.percentile(db, 10), np.percentile(db, 90)
    thr = float(np.clip(lo + 0.25 * (hi - lo), -55, -30))
    voiced = db > thr
    ranges = []
    i = 0
    while i < n:
        if voiced[i]:
            j = i
            while j < n and voiced[j]:
                j += 1
            ranges.append([i * 0.02, j * 0.02])
            i = j
        else:
            i += 1
    for sg in segs:
        ranges += [[w[0], w[1]] for w in sg['w']] if sg.get('w') else [[sg['s'], sg['e']]]
    ranges.sort()
    merged = []
    for a, b in ranges:  # 무음이 min_silence 미만이면 이어 붙임
        if merged and a - merged[-1][1] < min_silence:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        a, b = max(0.0, a - pad), min(dur, b + pad)
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        elif b - a >= 0.3:
            out.append([a, b])
    return [(a, b) for a, b in out] or [(0.0, dur)]


def remap(t, ranges):
    """원본 시간 -> 컷 편집 후 시간. 잘려 나간 구간 안의 시간은 그 지점(다음 구간 시작)으로 모은다."""
    off = 0.0
    for a, b in ranges:
        if t < a:
            return off
        if t <= b:
            return off + (t - a)
        off += b - a
    return off


# ---------------------------------------------------------------- 인코딩 (select/aselect 단일 패스)
def encode(src, ranges, subs, W, H, outp, job=None, sub_settings=None):
    tmp = os.path.join(EXPORTS, 'tmp_' + uuid.uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)
    expr = '+'.join(f'between(t,{a:.3f},{b:.3f})' for a, b in ranges)
    total = sum(b - a for a, b in ranges)
    fc = [f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB,scale={W}:{H},setsar=1,format=yuv420p[vc]",
          f"[0:a]aselect='{expr}',asetpts=N/SR/TB,aresample=44100[aout]"]
    lab = 'vc'
    if subs:
        S = dict(subFont='Pretendard', subSize=4.0, subBold=True, subStyle='box', subBottom=4.0)
        S.update(sub_settings or {})
        fc.append(subtitles_filter('vc', 'vout', W, H, S, subs, tmp))
        lab = 'vout'
    with open(os.path.join(tmp, 'graph.txt'), 'w', encoding='utf-8') as f:
        f.write(';\n'.join(fc))
    cmd = [FFMPEG, '-y', '-v', 'error', '-nostats', '-progress', 'pipe:1', '-i', src,
           '-filter_complex_script', 'graph.txt', '-map', f'[{lab}]', '-map', '[aout]',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', outp]
    p = subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='replace', creationflags=NOWIN)
    err = []
    threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True).start()
    for line in p.stdout:
        if job is not None and line.startswith(('out_time_us=', 'out_time_ms=')):
            try:
                job['progress'] = min(0.99, int(line.split('=')[1]) / 1e6 / max(total, 0.1))
            except ValueError:
                pass
    p.wait()
    shutil.rmtree(tmp, ignore_errors=True)
    if p.returncode != 0:
        raise RuntimeError('ffmpeg 오류: ' + (err[0] if err else '')[-600:])


# ---------------------------------------------------------------- 전체 처리
def fmt_t(t):
    return f'{int(t // 60):02d}:{t % 60:04.1f}'


def process_file(path, outdir, model='small', lang='ko', min_silence=0.8, pad=0.25, segs=None, script=None,
                 subs_mode='auto', sub_settings=None, log=print):
    """영상 1개 처리. script(docx/txt)가 있으면 원고 맞춤 자막, 없으면 장절 언급 기반 말씀 자막.
    subs_mode: auto(원고 있으면 all, 없으면 verse) / all(전체) / verse(말씀 구간만).
    segs를 주면 위스퍼를 건너뛴다(테스트용). 반환: 결과 파일 경로 dict."""
    os.makedirs(outdir, exist_ok=True)
    name = os.path.basename(path)
    base = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(name)[0])
    mid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(name)[1].lower()
    dst = os.path.join(MEDIA, mid + ext)
    log(f'[{name}] 복사 중…')
    shutil.copy2(path, dst)
    try:
        m = register_media(dst, mid, ext, name)
        if not m['hasVideo'] or not m['hasAudio']:
            raise RuntimeError('영상과 오디오가 모두 있는 파일이어야 합니다')
        mode = subs_mode if subs_mode != 'auto' else ('all' if script else 'verse')
        if segs is None:
            log(f'[{name}] 음성 분석 중… 영상 길이에 따라 오래 걸릴 수 있습니다 (25분 영상은 15~30분)')
            segs = get_segments(mid, m, model, lang, bool(script), log)
        ranges = speech_ranges(media_file(m), m['duration'], segs, min_silence, pad)
        rep = []
        if script:
            log(f'[{name}] 원고({os.path.basename(script)})와 음성을 맞추는 중…')
            cues = make_cues(script_words(read_script(script)))
            aligned, st = align_cues(cues, segs, m['duration'])
            if mode == 'verse':
                aligned = [c for c in aligned if c['verse']]
            subs = []
            for c in aligned:
                a, b = remap(c['s'], ranges), remap(c['e'], ranges)
                if b - a >= 0.3:
                    subs.append(dict(s=round(a, 2), e=round(b, 2), t=c['t']))
            rep.append(f'원고 맞춤 자막({"전체" if mode == "all" else "성경 말씀 구간만"}): 원고 조각 {st["cues"]}개 중 '
                       f'음성과 일치 {st["matched"]}개, 시간 추정 {st["interp"]}개, 제외 {st["dropped"]}개')
            rep.append(f'원고 글자의 {st["script_cov"] * 100:.0f}%가 음성에서 확인됨, 음성의 {st["speech_cov"] * 100:.0f}%가 원고와 일치')
            rep.append('※ 일치율이 낮으면(원고와 다르게 설교했거나 음성 인식이 어려운 구간) 해당 구간 자막이 빠지거나 시간이 어긋날 수 있습니다.')
            miss = [c for c in cues if c['t'] not in {a['t'] for a in aligned}]
            if miss:
                rep.append(f'\n[자막에서 빠진 원고 구간 {len(miss)}곳 — 설교에서 건너뛰었는지 확인하세요]')
                rep += [f'  - {c["t"].replace(chr(10), " ")}' for c in miss[:60]]
        else:
            log(f'[{name}] 말씀 구간 찾는 중… (문장 {len(segs)}개)')
            regions = find_verse_regions(segs)
            subs = []
            for r in regions:
                for sg in segs[r['first']:r['last'] + 1]:
                    for c in chunk_sub(sg):
                        a, b = remap(c['s'], ranges), remap(c['e'], ranges)
                        if b - a >= 0.3:
                            subs.append(dict(s=round(a, 2), e=round(b, 2), t=c['t']))
            rep.append(f'원고 없이 장절 언급으로 찾은 말씀 {len(regions)}곳 (같은 이름의 docx를 영상 옆에 두면 훨씬 정확합니다)')
            for r in regions:
                rep.append(f'- {fmt_t(r["s"])}~{fmt_t(r["e"])}  {r["ref"]}'
                           f'{"" if r["hasVerse"] else "  (절 번호 없음: 해설일 수 있음)"}')
                rep += [f'    {sg["t"]}' for sg in segs[r['first']:r['last'] + 1]]
        W, H = m['w'] // 2 * 2, m['h'] // 2 * 2
        kept = sum(b - a for a, b in ranges)
        outp = os.path.join(outdir, f'{base}_편집.mp4')
        log(f'[{name}] 인코딩 중… ({m["duration"]:.0f}초 → {kept:.0f}초, 자막 {len(subs)}개)')
        encode(media_file(m), ranges, subs, W, H, outp, dict(progress=0.0), sub_settings)
        srt_p = os.path.join(outdir, f'{base}_자막.srt')
        with open(srt_p, 'w', encoding='utf-8') as f:
            f.write(make_srt(subs))
        rep_p = os.path.join(outdir, f'{base}_리포트.txt')
        with open(rep_p, 'w', encoding='utf-8') as f:
            f.write(f'원본 {m["duration"]:.1f}초 → 편집 {kept:.1f}초 (무음 {m["duration"] - kept:.1f}초 제거, 컷 {len(ranges)}개)\n')
            f.write('\n'.join(rep) + '\n')
        log(f'[{name}] 완료 → {outp}')
        return dict(video=outp, srt=srt_p, report=rep_p, cuts=len(ranges), kept=kept, subs=len(subs))
    finally:  # 처리용 임시 복사본 정리
        for p_ in (dst, server.meta_path(mid)):
            try:
                os.remove(p_)
            except OSError:
                pass


# ---------------------------------------------------------------- 폴더 감시
def load_ledger():
    try:
        with open(LEDGER, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def watch(folder, outdir, interval=15, wait_script=600, **kw):
    """folder를 감시. 영상과 같은 이름의 docx/txt가 있으면 원고 맞춤 자막. 원고가 아직 없으면 wait_script초 기다린다."""
    done = load_ledger()
    sizes, first_seen = {}, {}
    print(f'감시 시작: {folder}\n결과 폴더: {outdir}\n(종료: Ctrl+C)')
    while True:
        for fn in sorted(os.listdir(folder)):
            p = os.path.join(folder, fn)
            if not os.path.isfile(p) or os.path.splitext(fn)[1].lower() not in VIDEO_EXT:
                continue
            sc = find_script(p)
            key = f'{fn}|{os.path.getsize(p)}|{os.path.getsize(sc) if sc else 0}'
            if key in done:
                continue
            cur = (os.path.getsize(p), os.path.getsize(sc) if sc else 0)
            if sizes.get(p) != cur:  # 업로드/동기화 중: 크기가 안정될 때까지 대기
                sizes[p] = cur
                first_seen.setdefault(p, time.time())
                continue
            if not sc and time.time() - first_seen.get(p, 0) < wait_script:
                continue  # 원고(docx)가 곧 올라올 수 있으니 잠시 기다림
            try:
                process_file(p, outdir, script=sc, **kw)
                done[key] = time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception as e:  # noqa
                import traceback
                traceback.print_exc()
                print(f'[{fn}] 실패: {e}')
                done[key] = f'실패: {e}'
            with open(LEDGER, 'w', encoding='utf-8') as f:
                json.dump(done, f, ensure_ascii=False, indent=1)
        time.sleep(interval)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='설교 영상 자동 편집: 원고 맞춤 자막 + 무음 컷')
    ap.add_argument('--watch', help='감시할 폴더 (Google Drive 데스크톱 동기화 폴더)')
    ap.add_argument('--file', help='영상 파일 1개만 처리')
    ap.add_argument('--script', help='원고 파일(docx/txt). 생략하면 영상과 같은 이름의 docx를 찾는다')
    ap.add_argument('--subs', default='auto', choices=['auto', 'all', 'verse'],
                    help='auto: 원고 있으면 전체 자막, 없으면 말씀 구간만 / all: 전체 / verse: 성경 말씀 구간만')
    ap.add_argument('--out', help='결과 폴더 (기본: 감시폴더/output)')
    ap.add_argument('--sub-size', type=float, default=4.0, help='자막 글자 크기(화면 높이 대비 %%). 작게: 3.5, 크게: 5')
    ap.add_argument('--sub-bottom', type=float, default=4.0, help='자막 아래 여백(화면 높이 대비 %%). 값이 클수록 위로 올라감')
    ap.add_argument('--sub-style', default='box', choices=['box', 'outline'], help='box: 흰 바탕+검은 글씨 / outline: 흰 글씨+검은 테두리')
    ap.add_argument('--model', default='small', help='whisper 모델: small(빠름) / medium(정확, 느림)')
    ap.add_argument('--silence', type=float, default=0.8, help='이 시간(초) 이상 무음이면 컷')
    ap.add_argument('--pad', type=float, default=0.25, help='말 앞뒤로 남기는 여유(초)')
    ap.add_argument('--wait-script', type=int, default=600, help='원고가 없을 때 올라오기를 기다리는 시간(초)')
    a = ap.parse_args()
    kw = dict(model=a.model, min_silence=a.silence, pad=a.pad, subs_mode=a.subs,
              sub_settings=dict(subSize=a.sub_size, subBottom=a.sub_bottom, subStyle=a.sub_style))
    if a.file:
        process_file(a.file, a.out or os.path.join(os.path.dirname(os.path.abspath(a.file)), 'output'),
                     script=a.script or find_script(a.file), **kw)
    elif a.watch:
        watch(a.watch, a.out or os.path.join(a.watch, 'output'), wait_script=a.wait_script, **kw)
    else:
        ap.print_help()
