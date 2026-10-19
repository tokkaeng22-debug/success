# CLAUDE.md

로컬 영상 편집기 (CapCut 벤치마킹). Flask 백엔드 + 단일 HTML 프런트엔드. 모든 AI(자막/음성분리)는 로컬 실행.

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

## 자동 편집 (autopipe.py)
- 구글 드라이브 데스크톱 동기화 폴더를 감시(`run_auto.bat`, `--watch`) → 새 영상마다 whisper 분석 → 자막 → 무음 컷 → `<폴더>/output/`에 `_편집.mp4`, `_자막.srt`, `_리포트.txt`.
- **원고 맞춤(권장)**: 영상과 같은 이름의 `.docx`/`.txt`(예: `0119설교.mp4` + `0119설교.docx`)가 있으면 원고 글자를 자막으로 쓰고 시간만 음성에서 가져옴(`read_script` → `make_cues` → `align_cues`). 원고가 없으면 `--wait-script`(기본 600초) 동안 올라오길 기다린 뒤 원고 없이 처리. 원고 추가/변경 시 장부 키가 달라져 재처리됨.
- `align_cues`: 원고를 ~400자씩 끊어 음성 글자열의 "현재 위치 근처"에서만 3글자 묶음으로 매칭(`difflib`). 우연히 맞은 짧은 블록은 `robust_blocks`로 걸러냄. 반복 문구에 휘둘리지 않으려면 전체 한 번에 매칭하지 말 것. 일치가 적은 조각은 앞뒤 기준점 사이 시간 여유가 맞을 때만 추정 배치, 아니면 제외(리포트에 목록).
- `--text mixed|script|heard`(기본 mixed): mixed는 `mix_cues` — 원고와 일치율 `--mix-threshold`(0.5) 이상인 조각은 원고 글자(성경 인용 조각은 일치율 무관하게 원고 우선), 그 외 말소리(즉흥 멘트·말 바꾼 곳)는 whisper 단어로 `heard_cues` 생성(환청 방지: 단어 확신도 평균 0.35 미만·3단어 미만·1.2초 미만 구간 제외). script는 원고 글자만, heard는 whisper 글자만. 리포트에 "AI 들은 글자로 쓴 자막" 목록이 나오니 검수용으로 활용.
- `--subs auto|all|verse`: 원고 있으면 전체(`all`), 없으면 말씀 구간만. 원고의 "장절 언급 바로 뒤 따옴표 인용"을 성경 본문(`verse`)으로 간주.
- 원고 없는 경우: 장절 언급(`REF_RE`) + 이어지는 낭독 구간만 자막(휴리스틱, 약함).
- whisper는 원고 모드에서 `word_timestamps=True`로 실행(`run_transcribe(words=True)`). 분석 결과는 `data/cache/tr_<해시>_<모델>_<w|s>.json`에 저장·재사용.
- 무음: 에너지 기반 + whisper 단어 구간은 항상 보존(단어 시간 없으면 문장 구간). 인코딩은 `select/aselect` 단일 입력 패스(컷이 많아도 느려지지 않음). 편집기 `run_export`와는 별개 경로이고 자막 입히기만 `subtitles_filter()`를 공유.
- 자막은 SRT가 아니라 `sub.ass`로 직접 생성(`subtitles_filter`): PlayRes=영상 해상도로 고정해 글자 크기·여백이 실제 픽셀(높이 대비 %)이 됨. ffmpeg `subtitles=sub.srt`+`force_style`은 PlayResY=288 기준이라 1080p에서 글자가 ~3.7배 커지고 위치도 틀어지므로 되돌리지 말 것. 스타일: `box`(흰 바탕+검은 글씨, 자동 편집 기본 크기 4%/아래 여백 4%) / `outline`(편집기 기본 3.5%/6%). 옵션 `--sub-size --sub-bottom --sub-style`.
- 자막 줄 수/폭: 기본 한 줄(`--sub-lines 1`), 한 줄 글자 수는 영상 너비의 `--sub-width`%(기본 58, 강대상 너비)를 채우도록 `process_file`에서 계산(한글 한 글자 폭 ≈ 글자 크기×0.67). 문단 끝에서는 자막을 반드시 끊어 제목·메모 줄이 말한 문장에 붙지 않게 함(`script_words`의 문단끝 플래그).
- 처리 이력은 `data/auto_done.json`(파일명|크기|원고크기). 구글 드라이브 API/커넥터는 사용하지 않음.
