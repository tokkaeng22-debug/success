"""설교 자동 편집 웹 화면(/auto)용 API. server.py의 Flask 앱에 register(app)로 붙인다.

- 작업 대기열(한 번에 하나씩 처리: whisper가 무거움) + 폴더 감시 스레드 + 설정 저장(data/auto_settings.json)
- 실제 편집은 autopipe.process_file 이 한다. 여기서는 버튼으로 쓰기 위한 껍데기만 담당.
"""
import os, re, json, time, string, threading, uuid, subprocess, traceback, hashlib, shutil
import urllib.request, urllib.error
from urllib.parse import urlparse, quote

from flask import request, jsonify, send_file, send_from_directory, abort

import autopipe as ap
from server import DATA, BASE, Canceled

SETTINGS_PATH = os.path.join(DATA, 'auto_settings.json')
UPLOAD_DIR = os.path.join(DATA, 'auto_in')
DEFAULT_OUT = os.path.join(DATA, 'auto_out')
os.makedirs(UPLOAD_DIR, exist_ok=True)

DEFAULTS = dict(folder='', out='', watch=False, text_mode='mixed', subs_mode='auto', sub_lines=1, sub_width=58.0,
                sub_size=4.0, sub_bottom=4.0, sub_style='box', model='small', silence=0.8, pad=0.25,
                wait_script=10, mix_thr=0.5, lang='ko', stumble=True, update_branch='main-5cl93u')
ENUMS = dict(text_mode={'mixed', 'script', 'heard'}, subs_mode={'auto', 'all', 'verse'}, sub_style={'box', 'outline'},
             model={'small', 'medium'}, sub_lines={1, 2})
RANGES = dict(sub_width=(25, 95), sub_size=(2.0, 8.0), sub_bottom=(0.0, 20.0), silence=(0.3, 3.0), pad=(0.0, 1.0),
              wait_script=(0, 120), mix_thr=(0.1, 0.95))

_exit = os._exit  # 테스트에서 바꿔 끼울 수 있게 별칭으로 둔다
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
        if k == 'update_branch' and not re.fullmatch(r'[A-Za-z0-9._/-]{1,80}', v):
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


# ---------------------------------------------------------------- 자동 적용: 서버 재시작 / 화면 새로고침 / 업데이트
CODE_FILES = ('server.py', 'autopipe.py', 'autoweb.py', 'stickers.py', 'static/index.html', 'static/auto.html')


def code_version():
    """코드 파일의 수정 시각 묶음. 바뀌면 열려 있는 화면이 스스로 새로고침한다."""
    h = hashlib.sha1()
    for f in CODE_FILES:
        try:
            h.update(str(os.stat(os.path.join(BASE, *f.split('/'))).st_mtime_ns).encode())
        except OSError:
            pass
    return h.hexdigest()[:10]


def busy():
    with LOCK:
        return any(t['state'] in ('queued', 'running') for t in TASKS)


def defer_reload_while_busy():
    """Werkzeug 자동 재시작이 작업 도중에 서버를 끄지 않도록, 작업이 끝날 때까지 재시작을 미룬다."""
    try:
        from werkzeug import _reloader
        orig = _reloader.ReloaderLoop.trigger_reload
        if getattr(orig, '_deferred', False):
            return

        def patched(self, filename):
            while busy():
                time.sleep(2)
            orig(self, filename)
        patched._deferred = True
        _reloader.ReloaderLoop.trigger_reload = patched
    except Exception:  # noqa  (Werkzeug 내부 구조가 다르면 기본 동작 유지)
        pass


UPDATE_REPO = 'tokkaeng22-debug/success'
UPDATE_ALLOW = re.compile(r'^(?:[^/]+\.py|CLAUDE\.md|agent\.md|static/[^/]+|fonts/[^/]+)$')  # .bat, data/, venv/, models/ 는 건드리지 않음
UPDATE_NEED = ('server.py', 'autopipe.py', 'autoweb.py', 'static/index.html', 'static/auto.html')
BACKUP_DIR = os.path.join(DATA, 'update_backup')


def _get(url, as_json=True, limit=60 << 20):
    req = urllib.request.Request(url, headers={'User-Agent': 'sermon-editor-updater', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RuntimeError('GitHub 요청 한도에 걸렸습니다. 잠시(약 1시간) 뒤에 다시 시도하세요')
        if e.code == 404:
            raise RuntimeError('GitHub에서 해당 브랜치/파일을 찾을 수 없습니다')
        raise RuntimeError(f'GitHub 오류 {e.code}')
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f'인터넷에 연결할 수 없습니다: {getattr(e, "reason", e)}')
    if len(data) > limit:
        raise RuntimeError('받은 파일이 너무 큽니다')
    return json.loads(data) if as_json else data


def blob_sha(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def update_check(branch):
    """GitHub 최신 커밋과 내 PC 파일을 비교한다(파일 해시 기준). 반환: dict(latest, msg, changed[], notes[])"""
    c = _get(f'https://api.github.com/repos/{UPDATE_REPO}/commits/{quote(branch, safe="")}')
    sha, msg = c['sha'], c['commit']['message'].split('\n')[0]
    tree = _get(f'https://api.github.com/repos/{UPDATE_REPO}/git/trees/{sha}?recursive=1')
    changed, files = [], {}
    for e in tree.get('tree', []):
        p = e.get('path', '')
        if e.get('type') != 'blob' or not UPDATE_ALLOW.match(p) or any(s in ('', '.', '..') or s.startswith('.') for s in p.split('/')):
            continue
        files[p] = e['sha']
        lp = os.path.join(BASE, *p.split('/'))
        try:
            with open(lp, 'rb') as f:
                same = blob_sha(f.read()) == e['sha']
        except OSError:
            same = False
        if not same:
            changed.append(p)
    try:
        recent = _get(f'https://api.github.com/repos/{UPDATE_REPO}/commits?sha={quote(branch, safe="")}&per_page=6')
        notes = [x['commit']['message'].split('\n')[0] for x in recent]
    except RuntimeError:
        notes = []
    return dict(latest=sha, short=sha[:7], msg=msg, changed=changed, notes=notes, branch=branch, files=files)


def update_apply(branch):
    """바뀐 파일만 받아서(해시 검증 + 문법 검사) 백업 후 덮어쓴다. 서버는 파일이 바뀐 것을 보고 스스로 다시 켜진다."""
    if busy():
        raise RuntimeError('작업이 진행 중입니다. 끝난 뒤에 업데이트하세요')
    info = update_check(branch)
    if not info['changed']:
        return dict(changed=[], short=info['short'], msg=info['msg'])
    sha, new = info['latest'], {}
    for p in info['changed']:
        data = _get(f'https://raw.githubusercontent.com/{UPDATE_REPO}/{sha}/{quote(p)}', as_json=False)
        if blob_sha(data) != info['files'][p]:
            raise RuntimeError(f'받은 파일이 GitHub 원본과 다릅니다({p}). 업데이트를 중단했습니다')
        if p.endswith('.py'):
            try:
                compile(data, p, 'exec')  # 실행하지 않고 문법만 검사
            except SyntaxError as e:
                raise RuntimeError(f'새 버전에 문법 오류가 있어 적용하지 않았습니다({p}: {e.msg})')
        new[p] = data
    for p in UPDATE_NEED:
        if p not in new and not os.path.exists(os.path.join(BASE, *p.split('/'))):
            raise RuntimeError(f'필수 파일({p})이 없어 적용하지 않았습니다')
    ts = time.strftime('%Y%m%d_%H%M%S')
    for p, data in new.items():
        lp = os.path.join(BASE, *p.split('/'))
        if os.path.exists(lp):
            bp = os.path.join(BACKUP_DIR, ts, *p.split('/'))
            os.makedirs(os.path.dirname(bp), exist_ok=True)
            shutil.copy2(lp, bp)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        with open(lp + '.new', 'wb') as f:
            f.write(data)
        os.replace(lp + '.new', lp)
    try:  # 백업은 최근 3번만 보관
        for d in sorted(os.listdir(BACKUP_DIR))[:-3]:
            shutil.rmtree(os.path.join(BACKUP_DIR, d), ignore_errors=True)
    except OSError:
        pass
    return dict(changed=sorted(new), short=info['short'], msg=info['msg'], backup=ts)


# ---------------------------------------------------------------- 작업 대기열
def pub(t):
    d = {k: t[k] for k in ('id', 'name', 'path', 'script', 'state', 'progress', 'msg', 'result', 'error', 'created',
                           'started', 'finished', 'base', 'cancel_req')}
    d['log'] = t['log'][-40:]
    return d


def enqueue(path, script, key, outdir):
    with LOCK:
        for t in TASKS:
            if t['path'] == path and t['state'] in ('queued', 'running'):
                return t
        t = dict(id=uuid.uuid4().hex[:8], name=os.path.basename(path), base=base_of(os.path.basename(path)),
                 path=path, script=script, outdir=outdir, key=key, state='queued', progress=0.0, msg='대기 중',
                 log=[], result=None, error='', created=time.time(), started=0, finished=0, opts=dict(SETTINGS),
                 cancel=threading.Event(), cancel_req=False)
        TASKS.append(t)
        WAKE.notify_all()
        return t


def process_opts(o):
    return dict(model=o['model'], lang=o['lang'], min_silence=o['silence'], pad=o['pad'], subs_mode=o['subs_mode'],
                text_mode=o['text_mode'], mix_thr=o['mix_thr'], sub_lines=o['sub_lines'], sub_width=o['sub_width'], fix_stumbles=o['stumble'],
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
            res = ap.process_file(t['path'], t['outdir'], script=t['script'], log=log, progress=prog, cancel=t['cancel'],
                                  **process_opts(t['opts']))
            t['result'] = {k: res[k] for k in ('video', 'srt', 'report', 'cuts', 'kept', 'subs')}
            t['state'], t['progress'], t['msg'] = 'done', 1.0, '완료'
            if t['key']:
                done = ap.load_ledger()
                done[t['key']] = time.strftime('%Y-%m-%d %H:%M:%S')
                ap.save_ledger(done)
        except Canceled:
            t['state'], t['msg'] = 'canceled', '중지됨'
            if t['key']:  # 폴더 감시가 곧바로 다시 시작하지 않도록 기록. 다시 하려면 목록의 [편집 시작]을 누른다
                done = ap.load_ledger()
                done[t['key']] = '중지됨'
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
                           files=list_files(), tasks=[pub(t) for t in TASKS][-30:], ver=code_version(),
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
                if t['id'] != tid:
                    continue
                if t['state'] == 'queued':
                    t['state'], t['msg'] = 'canceled', '취소됨'
                    return jsonify(ok=True)
                if t['state'] == 'running':  # 복사/음성 분석/인코딩 중 어디서든 곧 멈춘다
                    t['cancel'].set()
                    t['cancel_req'], t['msg'] = True, '중지하는 중…'
                    return jsonify(ok=True, stopping=True)
        return jsonify(error='이미 끝났거나 찾을 수 없는 작업입니다'), 400

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

    def local_only():
        """다른 웹사이트가 이 주소(127.0.0.1)로 몰래 요청을 보내는 것을 막는다(Host/Origin이 로컬일 때만 허용)."""
        ok = ('127.0.0.1', 'localhost')
        if request.host.split(':')[0] not in ok:
            abort(403)
        o = request.headers.get('Origin', '')
        if o and urlparse(o).hostname not in ok:
            abort(403)

    @app.route('/api/shutdown', methods=['POST'])
    def shutdown():
        """프로그램(서버) 종료. 작업이 돌고 있으면 force가 있어야 하며, 중지 신호를 보내 정리한 뒤 끝낸다."""
        local_only()
        force = bool((request.get_json(silent=True) or {}).get('force'))
        with LOCK:
            active = [t for t in TASKS if t['state'] in ('queued', 'running')]
            if active and not force:
                return jsonify(error='작업이 진행 중입니다', busy=len(active)), 409
            for t in active:
                t['cancel'].set()
                if t['state'] == 'queued':
                    t['state'], t['msg'] = 'canceled', '종료로 취소됨'

        def bye():
            t0 = time.time()
            while time.time() - t0 < 10 and any(t['state'] == 'running' for t in TASKS):
                time.sleep(0.2)  # 복사본/임시 파일을 정리할 시간을 준다
            time.sleep(0.4)
            _exit(0)
        threading.Thread(target=bye, daemon=True).start()
        return jsonify(ok=True)

    @app.route('/api/update/check', methods=['POST'])
    def update_check_route():
        local_only()
        try:
            r = update_check(SETTINGS['update_branch'])
        except RuntimeError as e:
            return jsonify(error=str(e)), 502
        r.pop('files')
        r['busy'] = busy()
        return jsonify(r)

    @app.route('/api/update/apply', methods=['POST'])
    def update_apply_route():
        local_only()
        try:
            return jsonify(ok=True, **update_apply(SETTINGS['update_branch']))
        except RuntimeError as e:
            return jsonify(error=str(e)), 409 if busy() else 502
