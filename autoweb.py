"""설교 자동 편집 웹 화면(/auto)용 API. server.py의 Flask 앱에 register(app)로 붙인다.

- 작업 대기열(한 번에 하나씩 처리: whisper가 무거움) + 폴더 감시 스레드 + 설정 저장(data/auto_settings.json)
- 실제 편집은 autopipe.process_file 이 한다. 여기서는 버튼으로 쓰기 위한 껍데기만 담당.
"""
import os, re, json, time, string, threading, uuid, subprocess, traceback

from flask import request, jsonify, send_file, send_from_directory, abort

import autopipe as ap
from server import DATA

SETTINGS_PATH = os.path.join(DATA, 'auto_settings.json')
UPLOAD_DIR = os.path.join(DATA, 'auto_in')
DEFAULT_OUT = os.path.join(DATA, 'auto_out')
os.makedirs(UPLOAD_DIR, exist_ok=True)

DEFAULTS = dict(folder='', out='', watch=False, text_mode='mixed', subs_mode='auto', sub_lines=1, sub_width=58.0,
                sub_size=4.0, sub_bottom=4.0, sub_style='box', model='small', silence=0.8, pad=0.25,
                wait_script=10, mix_thr=0.5, lang='ko')
ENUMS = dict(text_mode={'mixed', 'script', 'heard'}, subs_mode={'auto', 'all', 'verse'}, sub_style={'box', 'outline'},
             model={'small', 'medium'}, sub_lines={1, 2})
RANGES = dict(sub_width=(25, 95), sub_size=(2.0, 8.0), sub_bottom=(0.0, 20.0), silence=(0.3, 3.0), pad=(0.0, 1.0),
              wait_script=(0, 120), mix_thr=(0.1, 0.95))

LOCK = threading.RLock()
WAKE = threading.Condition(LOCK)
TASKS = []  # 최신이 뒤. dict: id,name,path,script,outdir,key,state,progress,msg,log,result,error,created,started,finished
SETTINGS = dict(DEFAULTS)
_watch = dict(sizes={}, first_seen={}, last=0.0)


# ---------------------------------------------------------------- 설정
def load_settings():
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            SETTINGS.update(clean(json.load(f)))
    except Exception:
        pass


def clean(d):
    out = {}
    for k, v in (d or {}).items():
        if k not in DEFAULTS:
            continue
        dv = DEFAULTS[k]
        try:
            if isinstance(dv, bool):
                v = bool(v)
            elif isinstance(dv, int):
                v = int(v)
            elif isinstance(dv, float):
                v = float(v)
            else:
                v = str(v).strip()
        except (TypeError, ValueError):
            continue
        if k in ENUMS and v not in ENUMS[k]:
            continue
        if k in RANGES:
            v = max(RANGES[k][0], min(RANGES[k][1], v))
        out[k] = v
    return out


def save_settings():
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=1)


def out_dir(for_upload=False):
    if SETTINGS['out']:
        return SETTINGS['out']
    if for_upload or not SETTINGS['folder']:
        return DEFAULT_OUT
    return os.path.join(SETTINGS['folder'], 'output')


def base_of(fn):
    return re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(fn)[0])


def folder_ok():
    return bool(SETTINGS['folder']) and os.path.isdir(SETTINGS['folder'])


# ---------------------------------------------------------------- 작업 대기열
def pub(t):
    d = {k: t[k] for k in ('id', 'name', 'path', 'script', 'state', 'progress', 'msg', 'result', 'error', 'created',
                           'started', 'finished', 'base')}
    d['log'] = t['log'][-40:]
    return d


def enqueue(path, script, key, outdir):
    with LOCK:
        for t in TASKS:
            if t['path'] == path and t['state'] in ('queued', 'running'):
                return t
        t = dict(id=uuid.uuid4().hex[:8], name=os.path.basename(path), base=base_of(os.path.basename(path)),
                 path=path, script=script, outdir=outdir, key=key, state='queued', progress=0.0, msg='대기 중',
                 log=[], result=None, error='', created=time.time(), started=0, finished=0, opts=dict(SETTINGS))
        TASKS.append(t)
        WAKE.notify_all()
        return t


def process_opts(o):
    return dict(model=o['model'], lang=o['lang'], min_silence=o['silence'], pad=o['pad'], subs_mode=o['subs_mode'],
                text_mode=o['text_mode'], mix_thr=o['mix_thr'], sub_lines=o['sub_lines'], sub_width=o['sub_width'],
                sub_settings=dict(subSize=o['sub_size'], subBottom=o['sub_bottom'], subStyle=o['sub_style']))


def worker():
    while True:
        with LOCK:
            t = next((x for x in TASKS if x['state'] == 'queued'), None)
            while t is None:
                WAKE.wait(2.0)
                t = next((x for x in TASKS if x['state'] == 'queued'), None)
            t['state'], t['started'], t['msg'] = 'running', time.time(), '시작하는 중…'

        def log(s):
            t['log'].append(str(s))

        def prog(msg, frac):
            t['msg'], t['progress'] = msg, frac

        try:
            res = ap.process_file(t['path'], t['outdir'], script=t['script'], log=log, progress=prog,
                                  **process_opts(t['opts']))
            t['result'] = {k: res[k] for k in ('video', 'srt', 'report', 'cuts', 'kept', 'subs')}
            t['state'], t['progress'], t['msg'] = 'done', 1.0, '완료'
            if t['key']:
                done = ap.load_ledger()
                done[t['key']] = time.strftime('%Y-%m-%d %H:%M:%S')
                ap.save_ledger(done)
        except Exception as e:  # noqa
            traceback.print_exc()
            t['state'], t['error'], t['msg'] = 'error', f'{type(e).__name__}: {e}', '실패'
            if t['key']:
                done = ap.load_ledger()
                done[t['key']] = f'실패: {e}'
                ap.save_ledger(done)
        t['finished'] = time.time()


def watcher():
    """설정에서 감시가 켜져 있으면 폴더를 훑어 새 영상을 대기열에 넣는다."""
    while True:
        time.sleep(3)
        try:
            if not (SETTINGS['watch'] and folder_ok()):
                continue
            if time.time() - _watch['last'] < 10:
                continue
            _watch['last'] = time.time()
            for p, sc, key in ap.ready_files(SETTINGS['folder'], _watch['sizes'], _watch['first_seen'],
                                             ap.load_ledger(), SETTINGS['wait_script'] * 60):
                enqueue(p, sc, key, out_dir())
        except Exception:  # noqa
            traceback.print_exc()


# ---------------------------------------------------------------- 파일 목록
def list_files():
    if not folder_ok():
        return []
    done = ap.load_ledger()
    out = []
    for fn in sorted(os.listdir(SETTINGS['folder']), key=str.lower):
        p = os.path.join(SETTINGS['folder'], fn)
        if fn.startswith('~$') or not os.path.isfile(p) or os.path.splitext(fn)[1].lower() not in ap.VIDEO_EXT:
            continue
        sc = ap.find_script(p)
        base = base_of(fn)
        has_out = os.path.exists(os.path.join(out_dir(), base + '_편집.mp4'))
        t = next((x for x in reversed(TASKS) if x['path'] == p), None)
        st = 'new'
        if t and t['state'] in ('queued', 'running'):
            st = t['state']
        elif t and t['state'] == 'error':
            st = 'error'
        elif has_out:
            st = 'done'
        out.append(dict(name=fn, base=base, path=p, size=os.path.getsize(p), script=os.path.basename(sc) if sc else None,
                        state=st, has_out=has_out, task=t['id'] if t else None,
                        error=t['error'] if t and t['state'] == 'error' else ''))
    return out


# ---------------------------------------------------------------- 폴더 탐색
def drives():
    res = []
    if os.name == 'nt':
        for c in string.ascii_uppercase:
            if os.path.exists(f'{c}:\\'):
                res.append(dict(name=f'{c}:\\', path=f'{c}:\\'))
                for n in ('내 드라이브', 'My Drive'):
                    q = os.path.join(f'{c}:\\', n)
                    if os.path.isdir(q):
                        res.append(dict(name=f'☁ Google Drive — {n} ({c}:)', path=q, star=True))
    else:
        home = os.path.expanduser('~')
        res += [dict(name='홈', path=home), dict(name='/', path='/')]
    return res


def browse(path):
    if not path:
        return dict(path='', parent=None, dirs=drives(), videos=0, shortcuts=True)
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        return dict(error='폴더를 찾을 수 없습니다', path=p)
    dirs, videos = [], 0
    try:
        for e in sorted(os.scandir(p), key=lambda x: x.name.lower()):
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(('.', '$')):
                dirs.append(dict(name=e.name, path=e.path))
            elif e.is_file() and os.path.splitext(e.name)[1].lower() in ap.VIDEO_EXT:
                videos += 1
    except PermissionError:
        return dict(error='이 폴더는 열 수 없습니다', path=p)
    par = os.path.dirname(p.rstrip('\\/'))
    return dict(path=p, parent=(par if par and par != p else ''), dirs=dirs, videos=videos)


# ---------------------------------------------------------------- 라우트
def inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def register(app):
    load_settings()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()

    @app.route('/auto')
    def auto_page():
        return send_from_directory(app.static_folder, 'auto.html')

    @app.route('/api/auto/state')
    def auto_state():
        with LOCK:
            return jsonify(settings=SETTINGS, defaults=DEFAULTS, folder_ok=folder_ok(), out_dir=out_dir(),
                           files=list_files(), tasks=[pub(t) for t in TASKS][-30:],
                           busy=any(t['state'] in ('queued', 'running') for t in TASKS))

    @app.route('/api/auto/settings', methods=['POST'])
    def auto_settings():
        with LOCK:
            SETTINGS.update(clean(request.get_json() or {}))
            SETTINGS['folder'] = SETTINGS['folder'].strip().strip('"')
            _watch['sizes'].clear()
            _watch['first_seen'].clear()
            _watch['last'] = 0.0
            save_settings()
        return jsonify(ok=True, settings=SETTINGS, folder_ok=folder_ok())

    @app.route('/api/auto/browse')
    def auto_browse():
        return jsonify(browse(request.args.get('path', '')))

    @app.route('/api/auto/run', methods=['POST'])
    def auto_run():
        d = request.get_json() or {}
        if not folder_ok():
            return jsonify(error='먼저 영상 폴더를 정해 주세요'), 400
        todo = []
        if d.get('all'):
            done = ap.load_ledger()
            for f in list_files():
                sc = ap.find_script(f['path'])
                key = f"{f['name']}|{f['size']}|{os.path.getsize(sc) if sc else 0}"
                if f['state'] == 'new' and key not in done:
                    todo.append((f['path'], sc, key))
        else:
            p = d.get('path', '')
            if not (os.path.isfile(p) and inside(p, SETTINGS['folder'])):
                return jsonify(error='영상 파일을 찾을 수 없습니다'), 404
            sc = ap.find_script(p)
            todo.append((p, sc, f'{os.path.basename(p)}|{os.path.getsize(p)}|{os.path.getsize(sc) if sc else 0}'))
        ids = [enqueue(p, sc, key, out_dir())['id'] for p, sc, key in todo]
        return jsonify(ok=True, queued=len(ids), ids=ids)

    @app.route('/api/auto/upload', methods=['POST'])
    def auto_upload():
        v, s = request.files.get('video'), request.files.get('script')
        if not v or not v.filename:
            return jsonify(error='영상 파일을 선택하세요'), 400
        vname = os.path.basename(v.filename.replace('\\', '/'))
        if os.path.splitext(vname)[1].lower() not in ap.VIDEO_EXT:
            return jsonify(error='지원하지 않는 영상 형식입니다'), 400
        d = os.path.join(UPLOAD_DIR, uuid.uuid4().hex[:8])
        os.makedirs(d)
        vp = os.path.join(d, vname)
        v.save(vp)
        sc = None
        if s and s.filename:
            ext = os.path.splitext(s.filename)[1].lower()
            if ext not in ('.docx', '.txt'):
                return jsonify(error='원고는 .docx 또는 .txt 파일이어야 합니다'), 400
            sc = os.path.join(d, os.path.splitext(vname)[0] + ext)
            s.save(sc)
        t = enqueue(vp, sc, None, out_dir(for_upload=True))
        return jsonify(ok=True, id=t['id'])

    @app.route('/api/auto/cancel', methods=['POST'])
    def auto_cancel():
        tid = (request.get_json() or {}).get('id')
        with LOCK:
            for t in TASKS:
                if t['id'] == tid and t['state'] == 'queued':
                    t['state'], t['msg'] = 'canceled', '취소됨'
                    return jsonify(ok=True)
        return jsonify(error='대기 중인 작업만 취소할 수 있습니다(처리 중인 작업은 끝날 때까지 기다려 주세요)'), 400

    @app.route('/api/auto/clear', methods=['POST'])
    def auto_clear():
        with LOCK:
            TASKS[:] = [t for t in TASKS if t['state'] in ('queued', 'running')]
        return jsonify(ok=True)

    def find_out(name, kind):
        suf = dict(video='_편집.mp4', srt='_자막.srt', report='_리포트.txt')[kind]
        if name != base_of(name) or name.startswith('.') or not name:
            abort(404)
        for d in (out_dir(), out_dir(for_upload=True), DEFAULT_OUT):
            p = os.path.join(d, name + suf)
            if os.path.isfile(p):
                return p
        abort(404)

    @app.route('/api/auto/out/<kind>')
    def auto_out(kind):
        if kind not in ('video', 'srt', 'report'):
            abort(404)
        p = find_out(request.args.get('name', ''), kind)
        dl = bool(request.args.get('dl'))
        if kind == 'video':
            return send_file(p, mimetype='video/mp4', conditional=True, as_attachment=dl)
        return send_file(p, mimetype='text/plain; charset=utf-8', as_attachment=dl)

    @app.route('/api/auto/openfolder', methods=['POST'])
    def auto_openfolder():
        d = out_dir()
        os.makedirs(d, exist_ok=True)
        try:
            if os.name == 'nt':
                os.startfile(d)  # noqa
            else:
                subprocess.Popen(['xdg-open', d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa
            return jsonify(error=f'폴더를 열 수 없습니다: {e}', path=d), 500
        return jsonify(ok=True, path=d)
