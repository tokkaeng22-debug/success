# CLAUDE.md

로컬 영상 편집기 (CapCut 벤치마킹). Flask 백엔드 + 단일 HTML 프런트엔드. 모든 AI(자막/음성분리)는 로컬 실행.

## 작업 규칙 (사용자 요청 시)
- 사용자가 **"success 저장소에 올려줘"**(비슷한 표현 포함)라고 하면: 변경된 파일을 커밋하고 **현재 작업 브랜치에 푸시**한다. 별도 확인을 되묻지 않는다.
  - 커밋 전 `git status`로 확인: `data/`, `venv/`, `.env`, 테스트 산출물(영상·캐시)은 올리지 않는다.
  - 작업 브랜치는 세션 지침이 정한 브랜치를 쓴다(지금까지는 `main-5cl93u`). main에 직접 푸시하지 않는다.
  - 푸시 후 그 브랜치의 열린 PR이 없으면 draft PR을 만들고, 있으면 그대로 두되 내용이 달라졌으면 PR 본문만 갱신한다.
  - 커밋 메시지 끝에는 세션 지침의 Co-Authored-By/Claude-Session 줄을 붙인다. 푸시 결과(커밋 해시)를 사용자에게 알려준다.

## 실행
- `run.bat` 또는 `venv\Scripts\python.exe server.py` → http://127.0.0.1:5173
- 첫 import가 느림(~30초). 서버 기동 후 접속 가능.
- 의존성: flask, imageio-ffmpeg, numpy, faster-whisper, torch(cpu), demucs(`--no-deps`) 외 julius einops pyyaml lameenc dora-search openunmix soundfile tqdm

## 구조
- `server.py`: 업로드/프록시/파형(`/api/peaks`), 자막(faster-whisper, CPU int8), 분리(demucs htdemucs, 해시 기반 캐시 `data/cache/sep/<sha1>`), 내보내기(ffmpeg filter_complex 생성 `run_export`), 프로젝트 저장 `/api/project`
- `static/index.html`: 전체 UI·편집 로직(상태 `P`, 타임라인 레이아웃 `LY`, undo 스냅샷 `snap/edit/changed`)
- `data/`: 업로드 사본·캐시·내보내기·project.json (git 제외)

## 주의사항
- 이 환경은 앱 제어 정책으로 PyAV DLL이 차단됨 → `server.py` 상단에서 `av`를 스텁 처리. 제거 금지.
- ffmpeg는 `imageio_ffmpeg` 번들 바이너리 사용(시스템 ffmpeg 없음, ffprobe 없음 → `ffmpeg -i` 출력 파싱).
- 미리보기 geometry(`geom()`)와 서버 내보내기 계산(`run_export`)은 동일해야 함. 한쪽을 바꾸면 다른 쪽도 수정.
- 분리 대상은 원본 30분 이하.
- `.env`와 `data/`, `venv/`는 커밋하지 않는다.

## 스티커 (P.stickers)
- `stickers.py`(Pillow, Pretendard Bold)가 이름표/말씀 카드 PNG를 렌더링. 편집기 미리보기(`/api/sticker.png`)와 내보내기(ffmpeg overlay)가 같은 PNG를 써서 결과가 일치함.
- 내보내기: `-loop 1` 이미지 입력 + `fade alpha` + overlay `enable=between(t,s,e)`, 슬라이드는 x 수식. 위치 계산(`run_export`)과 `updStickers()`(index.html)는 동일해야 함.
- 기본 세트: 이름표(왼쪽 아래)+말씀 제목(오른쪽 위), 14~25초.
- 의존성 추가: pillow

## 자동 편집 웹 화면 (/auto)
- `http://127.0.0.1:5173/auto` — 폴더 선택/감시 토글/영상 목록/편집 시작/직접 올리기/설정(미리보기)/진행률/결과 영상·리포트 보기. `run.bat`(server.py) 하나로 편집기(`/`)와 함께 뜸. 상단 메뉴로 서로 이동.
- 상단 `⏻ 프로그램 종료` 버튼 → `POST /api/shutdown`(Host/Origin이 127.0.0.1/localhost일 때만 허용). 작업 중이면 409 후 확인받아 force로 중지·정리 뒤 `os._exit`. run.bat의 Ctrl+C와 같은 효과.
- `autoweb.py`: `register(app)`로 라우트 등록. 작업 대기열(워커 1개, whisper가 무거워 순차 처리) + 감시 스레드(`ap.ready_files`) + 설정 저장(`data/auto_settings.json`) + 폴더 탐색(`/api/auto/browse`) + 결과 파일 제공(`/api/auto/out/<video|srt|report>?name=<이름>`). 편집 자체는 `autopipe.process_file`(진행률 콜백 `progress`)이 수행.
- `server.py`를 `python server.py`로 실행하면 `__main__`이라서 main 블록에서 `sys.modules.setdefault('server', ...)`를 먼저 해 둬야 autopipe/autoweb의 `import server`가 같은 모듈을 쓴다(두 번 import 금지).
- `run_auto.bat`(CLI 감시)과 웹의 "새 영상 자동 처리"는 같은 장부(`data/auto_done.json`)를 쓰지만 동시에 켜면 중복 처리될 수 있으니 하나만 사용.
- 작업 중지: 작업마다 `threading.Event`(`cancel`)를 두고 복사(`copy_file`)·음성 분석(`run_transcribe` 루프의 `job.cancel`)·인코딩(`encode`에서 ffmpeg kill)이 확인해 `server.Canceled`를 던진다. 인코딩은 `<출력>.part`에 쓰고 성공해야 `os.replace`하므로 중지/실패해도 기존 결과가 안 망가짐. 중지한 작업은 장부에 `중지됨`으로 기록해 폴더 감시가 곧바로 재시작하지 않게 함(목록의 [편집 시작]으로 재시작). 모델 다운로드/로딩 중에는 끝날 때까지 못 멈춤.
- 자동 편집은 `register_media(light=True)`로 편집기용 미리보기 변환·썸네일을 건너뜀(원본만 읽음).
- 화면 수정 시 `static/auto.html`(단일 파일, 바닐라 JS). 폴더 경로는 브라우저가 알 수 없어 서버가 직접 탐색.

## 자동 편집 (autopipe.py)
- 구글 드라이브 데스크톱 동기화 폴더를 감시(`run_auto.bat`, `--watch`) → 새 영상마다 whisper 분석 → 자막 → 무음 컷 → `<폴더>/output/`에 `_편집.mp4`, `_자막.srt`, `_리포트.txt`.
- **원고 맞춤(권장)**: 영상과 같은 이름의 `.docx`/`.txt`(예: `0119설교.mp4` + `0119설교.docx`)가 있으면 원고 글자를 자막으로 쓰고 시간만 음성에서 가져옴(`read_script` → `make_cues` → `align_cues`). 원고가 없으면 `--wait-script`(기본 600초) 동안 올라오길 기다린 뒤 원고 없이 처리. 원고 추가/변경 시 장부 키가 달라져 재처리됨.
- `align_cues`: 원고를 ~400자씩 끊어 음성 글자열의 "현재 위치 근처"에서만 3글자 묶음으로 매칭(`difflib`). 우연히 맞은 짧은 블록은 `robust_blocks`로 걸러냄. 반복 문구에 휘둘리지 않으려면 전체 한 번에 매칭하지 말 것. 일치가 적은 조각은 앞뒤 기준점 사이 시간 여유가 맞을 때만 추정 배치, 아니면 제외(리포트에 목록).
- `--text mixed|script|heard`(기본 mixed): mixed는 `mix_cues` — 원고와 일치율 `--mix-threshold`(0.5) 이상인 조각은 원고 글자(성경 인용 조각은 일치율 무관하게 원고 우선), 그 외 말소리(즉흥 멘트·말 바꾼 곳)는 whisper 단어로 `heard_cues` 생성(환청 방지: 단어 확신도 평균 0.35 미만·3단어 미만·1.2초 미만 구간 제외). script는 원고 글자만, heard는 whisper 글자만. 리포트에 "AI 들은 글자로 쓴 자막" 목록이 나오니 검수용으로 활용.
- **말 더듬기/다시 말하기 삭제**(기본 켬, `fix_stumbles`/`--no-stumble`/웹 설정 `stumble`): whisper 단어 시간 필요. whisper가 더듬은 말을 "정리"해 버리지 않게 `DISFLUENT_PROMPT`를 `initial_prompt`로 준다(캐시 파일명 접미사 `d`). `find_stumbles`: 단어 i..j 뒤에 같은 말이 다시 시작되면 앞 시도 [i,j)를 지움. **보수적 규칙** — 끝맺은 말(`_COMPLETE_RE`: 다/요/죠/고/며/면…) 뒤 반복(강조·병렬 구조)은 유지, 단순 더듬기(≤2단어 완전 반복) / 3단어 이상 일치+꼬리 ≤3 / 2단어 일치+꼬리 ≤2+끊긴 마지막 단어(앞부분만 말했거나 확신도<0.5)만 삭제, 원고에 같은 구절이 연달아 있으면(`phrase+phrase in script_norm`) 의도한 반복으로 유지, 재시작까지 간격 >1.8초면 제외. 한 글자 단어는 접두 일치로 보지 않음("이 이웃" 오삭제 방지). 자르는 지점은 `_valley`(말 사이 가장 조용한 20ms 프레임). 지운 단어는 `strip_words`로 위스퍼 결과에서 빼서 원고 맞춤·자막에 안 걸리게 하고, 남길 구간에서 `subtract_ranges`로 뺀다. 리포트에 "지움 → 이어지는 말" 목록. 합성 소리로는 "자연스럽게 들리는지"를 검증 못 했으니 실제 영상으로 확인할 것.
- `--subs auto|all|verse`: 원고 있으면 전체(`all`), 없으면 말씀 구간만. 원고의 "장절 언급 바로 뒤 따옴표 인용"을 성경 본문(`verse`)으로 간주.
- 원고 없는 경우: 장절 언급(`REF_RE`) + 이어지는 낭독 구간만 자막(휴리스틱, 약함).
- whisper는 원고 모드에서 `word_timestamps=True`로 실행(`run_transcribe(words=True)`). 분석 결과는 `data/cache/tr_<해시>_<모델>_<w|s>.json`에 저장·재사용.
- 무음: 에너지 기반 + whisper 단어 구간은 항상 보존(단어 시간 없으면 문장 구간). 인코딩은 `select/aselect` 단일 입력 패스(컷이 많아도 느려지지 않음). 편집기 `run_export`와는 별개 경로이고 자막 입히기만 `subtitles_filter()`를 공유.
- 자막은 SRT가 아니라 `sub.ass`로 직접 생성(`subtitles_filter`): PlayRes=영상 해상도로 고정해 글자 크기·여백이 실제 픽셀(높이 대비 %)이 됨. ffmpeg `subtitles=sub.srt`+`force_style`은 PlayResY=288 기준이라 1080p에서 글자가 ~3.7배 커지고 위치도 틀어지므로 되돌리지 말 것. 스타일: `box`(흰 바탕+검은 글씨, 자동 편집 기본 크기 4%/아래 여백 4%) / `outline`(편집기 기본 3.5%/6%). 옵션 `--sub-size --sub-bottom --sub-style`.
- 자막 줄 수/폭: 기본 한 줄(`--sub-lines 1`), 한 줄 글자 수는 영상 너비의 `--sub-width`%(기본 58, 강대상 너비)를 채우도록 `process_file`에서 계산(한글 한 글자 폭 ≈ 글자 크기×0.67). 문단 끝에서는 자막을 반드시 끊어 제목·메모 줄이 말한 문장에 붙지 않게 함(`script_words`의 문단끝 플래그).
- 처리 이력은 `data/auto_done.json`(파일명|크기|원고크기). 구글 드라이브 API/커넥터는 사용하지 않음.
