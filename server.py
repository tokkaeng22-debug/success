import sys, types, os, re, json, uuid, hashlib, subprocess, threading, time, shutil, wave
try:
    import av  # noqa  (PyAV can be blocked by Windows app-control policy; not needed)
except Exception:
    sys.modules['av'] = types.ModuleType('av')
import numpy as np
import imageio_ffmpeg
import stickers
from flask import Flask, request, jsonify, send_file, send_from_directory, abort

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
MEDIA = os.path.join(DATA, 'media')
CACHE = os.path.join(DATA, 'cache')
EXPORTS = os.path.join(DATA, 'exports')
SUBS = os.path.join(DATA, 'subs')
FONTS = os.path.join(BASE, 'fonts')
FONT_FAMILIES = {'Pretendard', 'NanumGothic', 'NanumMyeongjo', 'Gowun Batang'}
for d in (MEDIA, CACHE, EXPORTS, SUBS, os.path.join(CACHE, 'sep')):
    os.makedirs(d, exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

app = Flask(__name__, static_folder=os.path.join(BASE, 'static'), static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = None

JOBS = {}
WHISPER_LOCK = threading.Lock()
SEP_LOCK = threading.Lock()
EXPORT_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, creationflags=NOWIN, **kw)


def meta_path(mid):
    return os.path.join(MEDIA, mid + '.json')


def load_meta(mid):
    if not re.fullmatch(r'[0-9a-f]{12}', mid or ''):
        abort(404)
    p = meta_path(mid)
    if not os.path.exists(p):
        abort(404)
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def save_meta(m):
    with open(meta_path(m['id']), 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False)


def probe(path):
    r = run([FFMPEG, '-hide_banner', '-i', path], text=True, encoding='utf-8', errors='replace')
    t = r.stderr
    dur = 0.0
    m = re.search(r'Duration: (\d+):(\d+):([\d.]+)', t)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    info = dict(duration=dur, hasVideo=False, hasAudio=False, w=0, h=0, fps=30.0, vcodec='', acodec='')
    v = re.search(r'Stream #\S+.*?Video: (\w+).*?, (\d{2,5})x(\d{2,5})', t)
    if v and v.group(1) not in ('mjpeg', 'png', 'bmp'):
        info.update(hasVideo=True, vcodec=v.group(1), w=int(v.group(2)), h=int(v.group(3)))
        f = re.search(r'([\d.]+) fps', t)
        if f:
            info['fps'] = min(120.0, max(1.0, float(f.group(1))))
        rot = re.search(r'rotation of (-?[\d.]+) degrees', t)
        if rot and abs(round(float(rot.group(1)))) % 180 == 90:
            info['w'], info['h'] = info['h'], info['w']
    a = re.search(r'Stream #\S+.*?Audio: (\w+)', t)
    if a:
        info.update(hasAudio=True, acodec=a.group(1))
    return info


def media_file(m):
    return os.path.join(MEDIA, m['id'] + m['ext'])


def new_job(kind):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = dict(id=jid, kind=kind, state='running', progress=0.0, msg='', result=None)
    return JOBS[jid]


def spawn(job, fn, *a):
    def w():
        try:
            fn(job, *a)
            if job['state'] == 'running':
                job['state'] = 'done'
                job['progress'] = 1.0
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            job['state'] = 'error'
            job['msg'] = f'{type(e).__name__}: {e}'
    threading.Thread(target=w, daemon=True).start()


def decode_pcm(path, rate, ch):
    r = run([FFMPEG, '-v', 'error', '-i', path, '-vn', '-ac', str(ch), '-ar', str(rate), '-f', 'f32le', '-'])
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError('오디오 디코딩 실패: ' + r.stderr.decode('utf-8', 'replace')[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32)


# ---------------------------------------------------------------- pages / project
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/project', methods=['GET', 'POST'])
def project():
    p = os.path.join(DATA, 'project.json')
    if request.method == 'POST':
        with open(p, 'w', encoding='utf-8') as f:
            f.write(request.get_data(as_text=True))
        return jsonify(ok=True)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        d['media'] = [m for m in d.get('media', []) if os.path.exists(meta_path(m['id']))]
        return jsonify(d)
    return jsonify(None)


# ---------------------------------------------------------------- media
COMPAT_V = {'h264', 'vp8', 'vp9', 'av1'}
COMPAT_A = {'aac', 'mp3', 'opus', 'vorbis', 'flac', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'pcm_u8'}


def register_media(path, mid, ext, filename):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    info = probe(path)
    if not info['hasVideo'] and not info['hasAudio']:
        os.remove(path)
        raise ValueError('지원하지 않는 파일입니다')
    m = dict(id=mid, name=filename, ext=ext, hash=h.hexdigest(), kind='video' if info['hasVideo'] else 'audio',
             proxy=False, **info)
    # browsers cannot play everything -> make a light preview copy
    need = False
    if m['hasVideo'] and (m['vcodec'] not in COMPAT_V or ext not in ('.mp4', '.m4v', '.mov', '.webm')):
        need = True
    if m['hasAudio'] and m['acodec'] not in COMPAT_A:
        need = True
    if need:
        out = os.path.join(CACHE, mid + '_preview.mp4' if m['hasVideo'] else mid + '_preview.m4a')
        if m['hasVideo']:
            cmd = [FFMPEG, '-y', '-v', 'error', '-i', path, '-vf', 'scale=-2:min(720\\,ih)', '-c:v', 'libx264',
                   '-preset', 'ultrafast', '-crf', '26', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', out]
        else:
            cmd = [FFMPEG, '-y', '-v', 'error', '-i', path, '-vn', '-c:a', 'aac', '-b:a', '160k', out]
        if run(cmd).returncode == 0 and os.path.exists(out):
            m['proxy'] = True
    if m['hasVideo']:
        run([FFMPEG, '-y', '-v', 'error', '-ss', str(min(1.0, m['duration'] / 2)), '-i', path, '-frames:v', '1',
             '-vf', 'scale=-2:96', os.path.join(CACHE, mid + '.jpg')])
    save_meta(m)
    return m


@app.route('/api/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f:
        return jsonify(error='파일 없음'), 400
    mid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(f.filename or '')[1].lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,6}', ext):
        ext = '.bin'
    path = os.path.join(MEDIA, mid + ext)
    f.save(path)
    try:
        return jsonify(register_media(path, mid, ext, f.filename))
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.route('/media/<mid>')
def media_orig(mid):
    m = load_meta(mid)
    return send_file(media_file(m), conditional=True)


@app.route('/preview/<mid>')
def media_preview(mid):
    m = load_meta(mid)
    if m.get('proxy'):
        p = os.path.join(CACHE, mid + ('_preview.mp4' if m['hasVideo'] else '_preview.m4a'))
        if os.path.exists(p):
            return send_file(p, conditional=True)
    return send_file(media_file(m), conditional=True)


@app.route('/thumb/<mid>')
def thumb(mid):
    load_meta(mid)
    p = os.path.join(CACHE, mid + '.jpg')
    if not os.path.exists(p):
        abort(404)
    return send_file(p)


@app.route('/api/peaks/<mid>')
def peaks(mid):
    m = load_meta(mid)
    cp = os.path.join(CACHE, mid + '.pk')
    if not os.path.exists(cp):
        if not m['hasAudio']:
            data = b''
        else:
            pcm = decode_pcm(media_file(m), 8000, 1)
            n = len(pcm) // 160
            if n:
                a = np.abs(pcm[:n * 160]).reshape(n, 160).max(axis=1)
                data = np.minimum(255, (a ** 0.6) * 255).astype(np.uint8).tobytes()
            else:
                data = b''
        with open(cp, 'wb') as f:
            f.write(data)
    return send_file(cp, mimetype='application/octet-stream')  # 50 peaks / second


@app.route('/api/jobs/<jid>')
def job(jid):
    j = JOBS.get(jid)
    if not j:
        abort(404)
    return jsonify(j)


# ---------------------------------------------------------------- subtitles (local whisper)
_models = {}


def srt_time(t):
    t = max(0, t)
    ms = int(round(t * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def make_srt(subs):
    out = []
    for i, s in enumerate(subs, 1):
        out.append(f"{i}\n{srt_time(s['s'])} --> {srt_time(s['e'])}\n{s['t']}\n")
    return '\n'.join(out)


def run_transcribe(job, mid, lang, model_name, words=False):
    m = load_meta(mid)
    if not m['hasAudio']:
        raise RuntimeError('오디오가 없는 파일입니다')
    job['msg'] = '대기 중…'
    with WHISPER_LOCK:
        job['msg'] = '오디오 준비 중…'
        audio = decode_pcm(media_file(m), 16000, 1)
        dur = len(audio) / 16000
        job['msg'] = f'AI 모델 로딩 중 ({model_name}, 처음에는 다운로드가 필요합니다)…'
        from faster_whisper import WhisperModel
        if model_name not in _models:
            _models[model_name] = WhisperModel(model_name, device='cpu', compute_type='int8',
                                               cpu_threads=max(2, (os.cpu_count() or 4) - 1))
        model = _models[model_name]
        job['msg'] = '음성 분석 중…'
        kw = dict(language=lang or None, beam_size=3, vad_filter=True, word_timestamps=words)
        try:
            segs, info = model.transcribe(audio, **kw)
            it = iter(segs)
        except Exception:
            kw['vad_filter'] = False
            segs, info = model.transcribe(audio, **kw)
            it = iter(segs)
        out = []
        for sg in it:
            t = sg.text.strip()
            if t:
                o = dict(s=round(sg.start, 2), e=round(sg.end, 2), t=t)
                if words:
                    o['w'] = [[round(w.start, 2), round(w.end, 2), w.word] for w in (sg.words or [])]
                out.append(o)
            job['progress'] = min(0.99, sg.end / max(dur, 0.1))
        base = os.path.splitext(m['name'] or mid)[0]
        with open(os.path.join(SUBS, f'{mid}_{re.sub(r"[^0-9A-Za-z가-힣._-]", "_", base)}.srt'), 'w', encoding='utf-8') as f:
            f.write(make_srt(out))
        job['result'] = dict(subs=out, lang=info.language)


@app.route('/api/transcribe', methods=['POST'])
def transcribe():
    d = request.get_json()
    load_meta(d['mediaId'])
    j = new_job('transcribe')
    spawn(j, run_transcribe, d['mediaId'], d.get('lang') or None, d.get('model') or 'small')
    return jsonify(j)


# ---------------------------------------------------------------- vocal / music separation (local demucs)
def sep_dir(m):
    return os.path.join(CACHE, 'sep', m['hash'])


@app.route('/api/sep/<mid>')
def sep_status(mid):
    m = load_meta(mid)
    d = sep_dir(m)
    return jsonify(done=os.path.exists(os.path.join(d, 'vocals.wav')) and os.path.exists(os.path.join(d, 'music.wav')),
                   hash=m['hash'])


@app.route('/sep/<h>/<name>')
def sep_file(h, name):
    if not re.fullmatch(r'[0-9a-f]{40}', h) or name not in ('vocals.wav', 'music.wav'):
        abort(404)
    p = os.path.join(CACHE, 'sep', h, name)
    if not os.path.exists(p):
        abort(404)
    dl = request.args.get('dl')
    return send_file(p, conditional=True, as_attachment=bool(dl),
                     download_name=request.args.get('name') or name)


def write_wav(path, arr):  # arr: (n, 2) float32
    pcm = (np.clip(arr, -1, 1) * 32767).astype('<i2')
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(pcm.tobytes())


def run_sep(job, mid):
    m = load_meta(mid)
    if not m['hasAudio']:
        raise RuntimeError('오디오가 없는 파일입니다')
    d = sep_dir(m)
    if os.path.exists(os.path.join(d, 'vocals.wav')) and os.path.exists(os.path.join(d, 'music.wav')):
        job['msg'] = '이전 분리 결과를 재사용합니다'
        return
    job['msg'] = '대기 중…'
    with SEP_LOCK:
        job['msg'] = '오디오 준비 중…'
        pcm = decode_pcm(media_file(m), 44100, 2)
        import torch
        import demucs.apply as dapply
        from demucs.pretrained import get_model
        torch.set_num_threads(max(1, os.cpu_count() or 4))
        job['msg'] = 'AI 모델 로딩 중 (처음에는 다운로드가 필요합니다)…'
        model = get_model('htdemucs')
        model.eval()
        wav = torch.from_numpy(pcm.reshape(-1, 2).T.copy())
        ref = wav.mean(0)
        wav = (wav - ref.mean()) / (ref.std() + 1e-8)

        class Shim:  # progress hook for demucs
            @staticmethod
            def tqdm(it, **kw):
                n = len(it)
                for i, x in enumerate(it):
                    yield x
                    job['progress'] = min(0.97, (i + 1) / n)
        dapply.tqdm = Shim
        job['msg'] = '목소리/배경음 분리 중 (CPU)…'
        with torch.no_grad():
            src = dapply.apply_model(model, wav[None], device='cpu', split=True, overlap=0.25, progress=True)[0]
        src = src * (ref.std() + 1e-8) + ref.mean()
        names = list(model.sources)
        vocals = src[names.index('vocals')]
        music = sum(src[i] for i, n in enumerate(names) if n != 'vocals')
        os.makedirs(d, exist_ok=True)
        job['msg'] = 'WAV 저장 중…'
        write_wav(os.path.join(d, 'vocals.wav'), vocals.numpy().T)
        write_wav(os.path.join(d, 'music.wav'), music.numpy().T)


@app.route('/api/separate', methods=['POST'])
def separate():
    d = request.get_json()
    m = load_meta(d['mediaId'])
    if m['duration'] > 1800 + 5:
        return jsonify(error='원본 길이가 30분을 넘어 분리할 수 없습니다'), 400
    j = new_job('separate')
    spawn(j, run_sep, d['mediaId'])
    return jsonify(j)


# ---------------------------------------------------------------- skin beauty (ffmpeg filters, local)
def _f(b, k, d=0.0):
    try:
        return max(0.0, min(100.0, float(b.get(k, d)))) / 100.0
    except (TypeError, ValueError):
        return d


def beauty_active(b):
    return bool(b) and b.get('on') and any(_f(b, k) > 0 for k in ('white', 'smooth', 'wrinkle'))


def beauty_filters(tag, src, dst, b, sw, sh, dur):
    """Skin-only retouch: skin mask (YCbCr range * face ellipse, feathered) + maskedmerge of
    smoothing / wrinkle blur / brightening. Returns filter chains turning [src] into [dst]."""
    sw, sh = int(sw) // 2 * 2, int(sh) // 2 * 2
    white, smooth, wr, ct = _f(b, 'white'), _f(b, 'smooth'), _f(b, 'wrinkle'), _f(b, 'contour', 0.3)
    roi = b.get('roi') or {}
    cx, cy = float(roi.get('cx', 0.5)), float(roi.get('cy', 0.42))
    rx, ry = max(0.03, float(roi.get('rx', 0.45))), max(0.03, float(roi.get('ry', 0.6)))
    R = []
    R.append(f"[{src}]format=yuv444p,split=2[{tag}0][{tag}s]")
    R.append(f"[{tag}s]extractplanes=y+u+v[{tag}y][{tag}u][{tag}v]")
    R.append(f"[{tag}y]lut=y='gte(val,40)*lte(val,250)*255'[{tag}ym]")
    R.append(f"[{tag}u]lut=y='gte(val,77)*lte(val,127)*255'[{tag}um]")
    R.append(f"[{tag}v]lut=y='gte(val,133)*lte(val,173)*255'[{tag}vm]")
    R.append(f"[{tag}um][{tag}vm]blend=all_mode=darken[{tag}uv]")
    R.append(f"[{tag}uv][{tag}ym]blend=all_mode=darken[{tag}sk]")
    R.append(f"color=c=black:s=160x90:r=30:d={max(dur, 0.1):.3f},format=gray,"
             f"geq=lum='255*lte(pow((X/W-{cx:.4f})/{rx:.4f},2)+pow((Y/H-{cy:.4f})/{ry:.4f},2),1)',"
             f"scale={sw}:{sh}:flags=bicubic,gblur=sigma={max(2.0, sh * 0.02):.1f},format=gray[{tag}roi]")
    feather = max(1.5, (3 + ct * 18) * sh / 1080)
    R.append(f"[{tag}sk][{tag}roi]blend=all_mode=darken,gblur=sigma={feather:.2f},format=gray[{tag}mk]")
    stages = []
    if smooth > 0:
        stages.append(('sm', f"yaepblur=radius={2 + round(smooth * 3)}:sigma={int(20 + smooth * 180)}", 0.35 + 0.65 * smooth))
    if wr > 0:
        stages.append(('wr', f"yaepblur=radius={4 + round(wr * 6)}:sigma={int(60 + wr * 400)}", 0.25 + 0.7 * wr))
    if white > 0:
        g = 1 - 0.38 * white
        sat = 1 - 0.12 * white
        stages.append(('wh', f"lutyuv=y='clip(pow(val/255,{g:.3f})*255+{12 * white:.1f},16,235)':"
                             f"u='128+(val-128)*{sat:.3f}':v='128+(val-128)*{sat:.3f}'", 1.0))
    n = len(stages)
    if n > 1:
        R.append(f"[{tag}mk]split={n}" + ''.join(f"[{tag}mk{j}]" for j in range(n)))
    cur = f"{tag}0"
    for j, (nm, eff, amt) in enumerate(stages):
        mk = f"{tag}mk{j}" if n > 1 else f"{tag}mk"
        R.append(f"[{mk}]lut=y=val*{amt:.3f},split=3[{tag}q{j}a][{tag}q{j}b][{tag}q{j}c]")
        R.append(f"[{tag}q{j}a][{tag}q{j}b][{tag}q{j}c]mergeplanes=0x001020:yuv444p[{tag}m{j}]")
        R.append(f"[{cur}]split=2[{tag}a{j}][{tag}b{j}]")
        R.append(f"[{tag}b{j}]{eff}[{tag}e{j}]")
        nxt = f"{tag}n{j}"
        R.append(f"[{tag}a{j}][{tag}e{j}][{tag}m{j}]maskedmerge[{nxt}]")
        cur = nxt
    R.append(f"[{cur}]null[{dst}]")
    return R


def run_ff(cmd):
    return run(cmd, text=True, encoding='utf-8', errors='replace')


@app.route('/api/face', methods=['POST'])
def face_detect():
    d = request.get_json()
    m = load_meta(d['mediaId'])
    if not m['hasVideo']:
        return jsonify(error='영상이 아닙니다'), 400
    import cv2
    t0, t1 = float(d.get('t0', 0)), float(d.get('t1', m['duration']))
    n = 8
    boxes = []
    det = None
    for i in range(n):
        t = t0 + (t1 - t0) * (i + 0.5) / n
        r = run([FFMPEG, '-v', 'error', '-ss', f'{t:.3f}', '-i', media_file(m), '-frames:v', '1', '-vf',
                 'scale=640:-2', '-f', 'image2pipe', '-c:v', 'mjpeg', '-'])
        if not r.stdout:
            continue
        img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        h, w = img.shape[:2]
        if det is None:
            det = cv2.FaceDetectorYN.create(os.path.join(BASE, 'models', 'face_detection_yunet_2023mar.onnx'), '', (w, h), 0.6)
        det.setInputSize((w, h))
        res = det.detect(img)[1]
        if res is None:
            continue
        f = max(res, key=lambda r_: r_[2] * r_[3])
        boxes.append((f[0] / w, f[1] / h, f[2] / w, f[3] / h))
    if not boxes:
        return jsonify(found=False)
    a = np.median(np.array(boxes), axis=0)
    x, y, bw, bh = [float(v) for v in a]
    return jsonify(found=True, count=len(boxes), roi=dict(
        cx=round(x + bw / 2, 4), cy=round(y + bh / 2 + bh * 0.08, 4),
        rx=round(min(0.5, bw * 0.85), 4), ry=round(min(0.5, bh * 0.95), 4)))


@app.route('/api/beauty_preview', methods=['POST'])
def beauty_preview():
    d = request.get_json()
    m = load_meta(d['mediaId'])
    b = d['beauty']
    t = max(0.0, min(float(d.get('t', 0)), max(0.0, m['duration'] - 0.1)))
    out = os.path.join(CACHE, 'bp_' + uuid.uuid4().hex[:8] + '.jpg')
    chains = ["[0:v]scale=640:-2,split=2[pa][pb]"]
    sw = 640
    sh = int(round(m['h'] * 640 / max(1, m['w']) / 2) * 2)
    chains += beauty_filters('p', 'pb', 'pz', b if b.get('on') else {**b, 'on': True}, sw, sh, 1)
    chains.append("[pa]format=yuv420p[pa2];[pz]format=yuv420p[pz2];[pa2][pz2]hstack[o]")
    r = run_ff([FFMPEG, '-y', '-v', 'error', '-ss', f'{t:.3f}', '-i', media_file(m), '-filter_complex', ';'.join(chains),
                '-map', '[o]', '-frames:v', '1', '-q:v', '3', out])
    if r.returncode != 0 or not os.path.exists(out):
        return jsonify(error=r.stderr[-400:]), 500
    resp = send_file(out, mimetype='image/jpeg')
    resp.call_on_close(lambda: os.path.exists(out) and os.remove(out))
    return resp


# ---------------------------------------------------------------- export
def atempo_chain(s):
    f = []
    while s < 0.5:
        f.append(0.5)
        s /= 0.5
    while s > 2:
        f.append(2.0)
        s /= 2
    f.append(s)
    return ','.join(f'atempo={x:.5f}' for x in f)


AFMT = 'aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo'


def fades(vol, fi, fo, L):
    f = ''
    if fi > 0.01:
        f += f',afade=t=in:st=0:d={min(fi, L):.3f}'
    if fo > 0.01:
        f += f',afade=t=out:st={max(0, L - fo):.3f}:d={min(fo, L):.3f}'
    return f + f',volume={vol:.4f}'


def ass_time(t):
    cs = int(round(max(0, t) * 100))
    return f'{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}'


def subtitles_filter(src, dst, W, H, S, subs, tmp):
    """자막을 sub.ass(tmp)로 쓰고 이를 입히는 ffmpeg 필터 문자열을 돌려준다.
    PlayRes를 영상 해상도(W x H)로 고정해 글자 크기·여백이 실제 픽셀 기준(화면 높이 대비 %)이 되게 한다.
    S: subFont, subSize(높이 %), subBold, subStyle('outline' 외곽선 | 'box' 흰 바탕+검은 글씨), subBottom(아래 여백 높이 %)."""
    fam = S.get('subFont') or 'Pretendard'
    if fam not in FONT_FAMILIES:
        fam = 'Pretendard'
    fs = max(8, round(H * max(1.5, min(12.0, float(S.get('subSize', 3.5)))) / 100))
    bold = -1 if S.get('subBold', True) else 0
    mv = round(H * max(0.0, min(40.0, float(S.get('subBottom', 6.0)))) / 100)
    if S.get('subStyle') == 'box':  # BorderStyle 3: 외곽선 색이 글자 뒤 상자 색이 된다
        prim, outl, bs, ol = '&H00000000', '&H00FFFFFF', 3, max(2, round(fs * 0.22))
    else:
        prim, outl, bs, ol = '&H00FFFFFF', '&H00000000', 1, max(1, round(fs / 18))
    lines = ['[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {W}', f'PlayResY: {H}', 'WrapStyle: 2',
             'ScaledBorderAndShadow: yes', '', '[V4+ Styles]',
             'Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,'
             'Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,'
             'MarginV,Encoding',
             f'Style: Default,{fam},{fs},{prim},&H000000FF,{outl},&H00000000,{bold},0,0,0,100,100,0,0,{bs},{ol},0,2,'
             f'{round(W * 0.05)},{round(W * 0.05)},{mv},1', '', '[Events]',
             'Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text']
    for c in subs:
        txt = str(c['t']).replace('\\', '/').replace('{', '(').replace('}', ')').replace('\r', '')
        txt = '\\N'.join(x.strip() for x in txt.split('\n') if x.strip())
        lines.append(f"Dialogue: 0,{ass_time(c['s'])},{ass_time(max(c['e'], c['s'] + 0.05))},Default,,0,0,0,,{txt}")
    with open(os.path.join(tmp, 'sub.ass'), 'w', encoding='utf-8-sig') as f:
        f.write('\n'.join(lines) + '\n')
    bs_ = chr(92)
    fdir = FONTS.replace(bs_, '/').replace(':', bs_ + ':')
    return f"[{src}]ass=sub.ass:fontsdir='{fdir}'[{dst}]"


def run_export(job, P, burn):
    S = P['settings']
    W, H = int(S['W']) // 2 * 2, int(S['H']) // 2 * 2
    clips = P['clips']
    if not clips:
        raise RuntimeError('타임라인에 컷이 없습니다')
    inputs = []
    fc = []
    tmp = os.path.join(EXPORTS, 'tmp_' + job['id'])
    os.makedirs(tmp, exist_ok=True)

    def add(path, pre=()):
        inputs.extend(list(pre) + ['-i', path])
        return len(inputs and [x for x in inputs if x == '-i']) - 1

    total = 0.0
    segs = []
    for i, c in enumerate(clips):
        m = load_meta(c['mid'])
        sp = max(0.25, min(4.0, float(c['speed'])))
        a_in, a_out = float(c['in']), float(c['out'])
        L = (a_out - a_in) / sp
        total += L
        # ---- video
        k = add(media_file(m))
        rot = int(c.get('rot', 0)) % 360
        ch = f"[{k}:v]trim=start={a_in:.4f}:end={a_out:.4f},setpts=(PTS-STARTPTS)/{sp:.5f},fps=30"
        bt = c.get('beauty')
        if beauty_active(bt):
            fc.append(ch + f"[bx{i}]")
            fc.extend(beauty_filters(f"b{i}", f"bx{i}", f"bz{i}", bt, m['w'], m['h'], L))
            ch = f"[bz{i}]null"
        if c.get('flip'):
            ch += ',hflip'
        ch += {0: '', 90: ',transpose=1', 180: ',transpose=1,transpose=1', 270: ',transpose=2'}[rot]
        rw, rh = (m['h'], m['w']) if rot in (90, 270) else (m['w'], m['h'])
        # m['w'],m['h'] already account for container rotation metadata (ffmpeg autorotates)
        kk = max(W / rw, H / rh) if S.get('fit') == 'cover' else min(W / rw, H / rh)
        sc = float(c.get('scale', 1))
        bw, bh = max(2, round(rw * kk * sc / 2) * 2), max(2, round(rh * kk * sc / 2) * 2)
        ox = (W - bw) / 2 + float(c.get('x', 0)) * W
        oy = (H - bh) / 2 + float(c.get('y', 0)) * H
        ch += f",scale={bw}:{bh},setsar=1[vz{i}]"
        fc.append(ch)
        fc.append(f"color=c=black:s={W}x{H}:r=30:d={L:.4f}[bg{i}]")
        fc.append(f"[bg{i}][vz{i}]overlay=x={ox:.1f}:y={oy:.1f}:shortest=1,format=yuv420p[v{i}]")
        # ---- audio
        sep = c.get('sep') or {'mode': 'orig'}
        mode = sep.get('mode', 'orig')
        cv = 0.0 if c.get('mute') else float(c.get('vol', 1))
        fi, fo = float(c.get('fi', 0)), float(c.get('fo', 0))
        tail = f",{atempo_chain(sp)}" if abs(sp - 1) > 1e-3 else ''
        if not m['hasAudio']:
            fc.append(f"anullsrc=r=44100:cl=stereo,atrim=duration={L:.4f},asetpts=PTS-STARTPTS[a{i}]")
        elif mode == 'orig':
            ka = add(media_file(m))
            fc.append(f"[{ka}:a]atrim=start={a_in:.4f}:end={a_out:.4f},asetpts=PTS-STARTPTS,{AFMT}{tail}"
                      f"{fades(cv, fi, fo, L)}[a{i}]")
        else:
            d = sep_dir(m)
            parts = []
            for stem, key, on in (('vocals', 'v', mode in ('both', 'vocals')), ('music', 'm', mode in ('both', 'music'))):
                if not on:
                    continue
                p = os.path.join(d, stem + '.wav')
                if not os.path.exists(p):
                    raise RuntimeError(f'분리 결과가 없습니다: {m["name"]}')
                ka = add(p)
                fc.append(f"[{ka}:a]atrim=start={a_in:.4f}:end={a_out:.4f},asetpts=PTS-STARTPTS,{AFMT}{tail},"
                          f"volume={float(sep.get(key, 1)):.4f}[s{i}{stem}]")
                parts.append(f"[s{i}{stem}]")
            if len(parts) == 2:
                fc.append(f"{parts[0]}{parts[1]}amix=inputs=2:normalize=0{fades(cv, fi, fo, L)}[a{i}]")
            else:
                fc.append(f"{parts[0]}anull{fades(cv, fi, fo, L)}[a{i}]")
        segs.append(f"[v{i}][a{i}]")
    fc.append(''.join(segs) + f"concat=n={len(clips)}:v=1:a=1[vc][ac0]")
    # original-audio master volume + sync
    ov = float(S.get('origVol', 1))
    sync = float(S.get('syncMs', 0))
    chain = f"[ac0]volume={ov:.4f}"
    if sync > 0:
        chain += f",adelay={int(sync)}|{int(sync)}"
    elif sync < 0:
        chain += f",atrim=start={-sync / 1000:.3f},asetpts=PTS-STARTPTS"
    fc.append(chain + "[ac1]")
    mix = ['[ac1]']
    for j, t in enumerate(P.get('tracks', [])):
        tm = load_meta(t['mid'])
        if not tm['hasAudio']:
            continue
        kt = add(media_file(tm))
        t_in, t_out = float(t['in']), float(t['out'])
        dur = max(0.05, float(t['dur']))
        seglen = max(0.05, t_out - t_in)
        s = f"[{kt}:a]atrim=start={t_in:.4f}:end={t_out:.4f},asetpts=PTS-STARTPTS,{AFMT}"
        if t.get('loop') and dur > seglen:
            s += f",aloop=loop=-1:size={int(seglen * 44100)}:start=0"
        s += f",atrim=end={dur:.4f},asetpts=PTS-STARTPTS"
        tv = 0.0 if t.get('mute') else float(t.get('vol', 1))
        s += fades(tv, float(t.get('fi', 0)), float(t.get('fo', 0)), dur)
        st = max(0, int(float(t['start']) * 1000))
        s += f",adelay={st}|{st}[t{j}]"
        fc.append(s)
        mix.append(f"[t{j}]")
    if len(mix) > 1:
        fc.append(''.join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=longest,alimiter=limit=0.95[aout]")
    else:
        fc.append("[ac1]alimiter=limit=0.95[aout]")
    vlabel = 'vc'
    for j, st in enumerate(P.get('stickers') or []):
        s0 = max(0.0, float(st['start']))
        e0 = min(total, float(st['end']))
        if e0 - s0 < 0.2:
            continue
        lines = str(st.get('text', '')).split(chr(10))
        png, iw, ih = stickers.render(lines, st.get('kind', 'name'), st.get('color', '#f59e0b'), H, float(st.get('scale', 1)))
        pth = os.path.join(tmp, f'stk{j}.png')
        with open(pth, 'wb') as f:
            f.write(png)
        k = add(pth, ['-loop', '1', '-framerate', '30', '-t', f'{total:.3f}'])
        anim = st.get('anim', 'slide')
        d = max(0.1, min(2.0, float(st.get('ad', 0.5)), (e0 - s0) / 2))
        mx, my = round(W * 0.04), round(H * 0.05)
        pos = st.get('pos', 'bl')
        x0 = mx if pos[1] == 'l' else (W - iw - mx if pos[1] == 'r' else (W - iw) / 2)
        y0 = my if pos[0] == 't' else H - ih - my
        chain = f"[{k}:v]format=rgba"
        if anim != 'none':
            chain += f",fade=t=in:st={s0:.3f}:d={d:.3f}:alpha=1,fade=t=out:st={e0 - d:.3f}:d={d:.3f}:alpha=1"
        fc.append(chain + f"[stk{j}]")
        xe = f"{x0:.1f}"
        if anim == 'slide' and pos[1] != 'c':
            sl = round(W * 0.06) * (-1 if pos[1] == 'l' else 1)
            xe = f"{x0:.1f}+({sl})*(max(0,1-(t-{s0:.3f})/{d:.3f})+max(0,(t-{e0 - d:.3f})/{d:.3f}))"
        fc.append(f"[{vlabel}][stk{j}]overlay=x='{xe}':y={y0:.1f}:enable='between(t,{s0:.3f},{e0:.3f})'[vs{j}]")
        vlabel = f'vs{j}'
    if burn and P.get('subs'):
        fc.append(subtitles_filter(vlabel, 'vout', W, H, S, P['subs'], tmp))
        vlabel = 'vout'
    script = os.path.join(tmp, 'graph.txt')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(';\n'.join(fc))
    name = f"export_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    outp = os.path.join(EXPORTS, name)
    cmd = [FFMPEG, '-y', '-v', 'error', '-nostats', '-progress', 'pipe:1'] + inputs + [
        '-filter_complex_script', 'graph.txt', '-map', f'[{vlabel}]', '-map', '[aout]',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', '-t', f'{total:.3f}', outp]
    # inputs hold absolute paths; run from tmp dir so graph/sub paths are short & colon free
    with EXPORT_LOCK:
        job['msg'] = '영상 인코딩 중…'
        p = subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding='utf-8', errors='replace', creationflags=NOWIN)
        err = []
        threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True).start()
        for line in p.stdout:
            if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
                try:
                    job['progress'] = min(0.99, int(line.split('=')[1]) / 1e6 / max(total, 0.1))
                except ValueError:
                    pass
        p.wait()
    shutil.rmtree(tmp, ignore_errors=True)
    if p.returncode != 0:
        raise RuntimeError('ffmpeg 오류: ' + (err[0] if err else '')[-600:])
    job['result'] = dict(file=name, size=os.path.getsize(outp))


@app.route('/api/export', methods=['POST'])
def export():
    d = request.get_json()
    j = new_job('export')
    spawn(j, run_export, d['project'], bool(d.get('burn')))
    return jsonify(j)


@app.route('/exports/<name>')
def exports(name):
    if not re.fullmatch(r'export_[0-9_]+\.mp4', name):
        abort(404)
    return send_from_directory(EXPORTS, name, as_attachment=bool(request.args.get('dl')))


@app.route('/api/sticker.png')
def sticker_png():
    a = request.args
    png, w, h = stickers.render(a.get('t', '').split(chr(10)), a.get('k', 'name'), a.get('c', '#f59e0b'),
                                max(100, min(4320, int(float(a.get('H', 1080))))), float(a.get('s', 1)))
    r = app.response_class(png, mimetype='image/png')
    r.headers['Cache-Control'] = 'max-age=3600'
    return r


@app.route('/fonts/<name>')
def fonts(name):
    return send_from_directory(FONTS, name)


@app.route('/api/srt', methods=['POST'])
def srt():
    d = request.get_json()
    return app.response_class(make_srt(d['subs']), mimetype='text/plain; charset=utf-8')


if __name__ == '__main__':
    import webbrowser
    port = 5173
    threading.Timer(1.2, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    app.run(host='127.0.0.1', port=port, threaded=True)
