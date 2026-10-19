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
- 구글 드라이브 데스크톱 동기화 폴더를 감시(`run_auto.bat`, `--watch`) → 새 영상마다 whisper 자막 → 성경 장절 언급(`REF_RE`) + 이어지는 낭독 구간만 자막 → 무음(에너지 기반, whisper 구간은 항상 보존) 컷 → `<폴더>/output/`에 `_편집.mp4`, `_말씀자막.srt`, `_리포트.txt`.
- 인코딩은 `select/aselect` 단일 입력 패스(컷이 많아도 느려지지 않음). 편집기 `run_export`와는 별개 경로이고 자막 스타일만 `subtitles_filter()`를 공유.
- 말씀 구간은 휴리스틱(장절 언급 후 절 수×15초 예산, 1.8초 이상 휴지/종결어미에서 종료)이라 리포트로 검수 필요. 성경 원문 대조는 하지 않음.
- 처리 이력은 `data/auto_done.json`(파일명|크기). 구글 드라이브 API/커넥터는 사용하지 않음.
