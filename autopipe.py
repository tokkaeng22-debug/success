"""구글 드라이브 폴더 자동 편집: 성경 말씀 구간 자막 + 무음 구간 컷 -> MP4.

사용법
  python autopipe.py --watch "G:\\내 드라이브\\설교"   # 폴더 감시 (Google Drive 데스크톱 동기화 폴더)
  python autopipe.py --file 영상.mp4 --out 결과폴더      # 파일 1개 처리

감시 폴더에 새 영상이 올라오면 `<감시폴더>/output/` 에 아래 파일을 만든다.
  <이름>_편집.mp4        무음 컷 + 말씀 구간 자막이 입혀진 영상
  <이름>_말씀자막.srt    말씀 구간 자막 (컷 이후 타임라인 기준)
  <이름>_리포트.txt      찾은 말씀·컷 통계 (검수용)
"""
import sys, os, re, json, time, argparse, shutil, subprocess, threading, uuid

import numpy as np

import server
from server import FFMPEG, NOWIN, MEDIA, DATA, EXPORTS, make_srt, subtitles_filter, register_media, \
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


# ---------------------------------------------------------------- 무음 컷
def speech_ranges(path, dur, segs, min_silence=0.8, pad=0.25):
    """에너지 기반 무음 탐지. 위스퍼가 말한 구간은 항상 살린다. 반환: [(시작, 끝)] 초."""
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
    ranges += [[s['s'], s['e']] for s in segs]
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
    off = 0.0
    for a, b in ranges:
        if a - 1e-3 <= t <= b + 1e-3:
            return off + (t - a)
        off += b - a
    return None


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
        with open(os.path.join(tmp, 'sub.srt'), 'w', encoding='utf-8') as f:
            f.write(make_srt(subs))
        S = dict(subFont='Pretendard', subSize=3.5, subBold=True)
        S.update(sub_settings or {})
        fc.append(subtitles_filter('vc', 'vout', W, H, S))
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
def process_file(path, outdir, model='small', lang='ko', min_silence=0.8, pad=0.25, segs=None, log=print):
    """영상 1개 처리. segs를 주면 위스퍼를 건너뛴다(테스트용). 반환: 결과 파일 경로 dict."""
    os.makedirs(outdir, exist_ok=True)
    name = os.path.basename(path)
    base = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(name)[0])
    mid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(name)[1].lower()
    dst = os.path.join(MEDIA, mid + ext)
    log(f'[{name}] 복사 중…')
    shutil.copy2(path, dst)
    m = register_media(dst, mid, ext, name)
    if not m['hasVideo'] or not m['hasAudio']:
        raise RuntimeError('영상과 오디오가 모두 있는 파일이어야 합니다')
    if segs is None:
        log(f'[{name}] 음성 분석(자막) 중… 영상 길이에 따라 오래 걸릴 수 있습니다')
        job = dict(msg='', progress=0.0, result=None)
        run_transcribe(job, mid, lang, model)
        segs = job['result']['subs']
    log(f'[{name}] 말씀 구간 찾는 중… (문장 {len(segs)}개)')
    regions = find_verse_regions(segs)
    ranges = speech_ranges(media_file(m), m['duration'], segs, min_silence, pad)
    subs = []
    for r in regions:
        for sg in segs[r['first']:r['last'] + 1]:
            for c in chunk_sub(sg):
                a, b = remap(c['s'], ranges), remap(c['e'], ranges)
                if a is not None and b is not None and b > a:
                    subs.append(dict(s=round(a, 2), e=round(b, 2), t=c['t']))
    W, H = m['w'] // 2 * 2, m['h'] // 2 * 2
    kept = sum(b - a for a, b in ranges)
    outp = os.path.join(outdir, f'{base}_편집.mp4')
    log(f'[{name}] 인코딩 중… ({m["duration"]:.0f}초 → {kept:.0f}초)')
    job = dict(progress=0.0)
    encode(media_file(m), ranges, subs, W, H, outp, job)
    srt_p = os.path.join(outdir, f'{base}_말씀자막.srt')
    with open(srt_p, 'w', encoding='utf-8') as f:
        f.write(make_srt(subs))
    rep_p = os.path.join(outdir, f'{base}_리포트.txt')
    with open(rep_p, 'w', encoding='utf-8') as f:
        f.write(f'원본 {m["duration"]:.1f}초 → 편집 {kept:.1f}초 (무음 {m["duration"] - kept:.1f}초 제거, 컷 {len(ranges)}개)\n')
        f.write(f'찾은 말씀 {len(regions)}곳 — 자막 내용은 AI 인식이라 성경 원문과 다를 수 있으니 반드시 검수하세요.\n\n')
        for r in regions:
            f.write(f'- {int(r["s"] // 60):02d}:{r["s"] % 60:04.1f}~{int(r["e"] // 60):02d}:{r["e"] % 60:04.1f}  {r["ref"]}'
                    f'{"" if r["hasVerse"] else "  (절 번호 없음: 해설일 수 있음)"}\n')
            for sg in segs[r['first']:r['last'] + 1]:
                f.write(f'    {sg["t"]}\n')
    # 원본/사본 정리: 처리가 끝난 임시 복사본은 지운다
    for p_ in (dst, server.meta_path(mid)):
        try:
            os.remove(p_)
        except OSError:
            pass
    log(f'[{name}] 완료 → {outp}')
    return dict(video=outp, srt=srt_p, report=rep_p, regions=regions, cuts=len(ranges), kept=kept)


# ---------------------------------------------------------------- 폴더 감시
def load_ledger():
    try:
        with open(LEDGER, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def watch(folder, outdir, interval=15, **kw):
    done = load_ledger()
    sizes = {}
    print(f'감시 시작: {folder}\n결과 폴더: {outdir}\n(종료: Ctrl+C)')
    while True:
        for fn in sorted(os.listdir(folder)):
            p = os.path.join(folder, fn)
            if not os.path.isfile(p) or os.path.splitext(fn)[1].lower() not in VIDEO_EXT:
                continue
            key = f'{fn}|{os.path.getsize(p)}'
            if key in done:
                continue
            if sizes.get(p) != os.path.getsize(p):  # 업로드/동기화 중: 크기가 안정될 때까지 대기
                sizes[p] = os.path.getsize(p)
                continue
            try:
                process_file(p, outdir, **kw)
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
    ap = argparse.ArgumentParser(description='성경 말씀 자막 + 무음 컷 자동 편집')
    ap.add_argument('--watch', help='감시할 폴더 (Google Drive 데스크톱 동기화 폴더)')
    ap.add_argument('--file', help='영상 파일 1개만 처리')
    ap.add_argument('--out', help='결과 폴더 (기본: 감시폴더/output)')
    ap.add_argument('--model', default='small', help='whisper 모델: small(빠름) / medium(정확, 느림)')
    ap.add_argument('--silence', type=float, default=0.8, help='이 시간(초) 이상 무음이면 컷')
    ap.add_argument('--pad', type=float, default=0.25, help='말 앞뒤로 남기는 여유(초)')
    a = ap.parse_args()
    kw = dict(model=a.model, min_silence=a.silence, pad=a.pad)
    if a.file:
        process_file(a.file, a.out or os.path.join(os.path.dirname(os.path.abspath(a.file)), 'output'), **kw)
    elif a.watch:
        watch(a.watch, a.out or os.path.join(a.watch, 'output'), **kw)
    else:
        ap.print_help()
