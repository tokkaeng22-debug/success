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

## 이미지 (P.stickers, kind:'image')
- Canva PNG/JPG 등 정지 이미지. `/api/image`가 PNG로 정규화해 `data/media/<id>.png`+메타(kind:'image') 저장, `/image/<id>`로 제공. P.media에는 넣지 않음.
- 스티커와 같은 트랙·드래그·복제·실행취소를 공유. 필드: mid, name, iw, ih, start, end, pos(cc/tl/tc/tr/cl/cr/bl/bc/br), scale(0.1~1, 1=화면 안에 꽉 맞춤), anim, ad.
- 위치 계산 `image_box()`(server.py) = `imgBox()`(index.html). 한쪽을 바꾸면 다른 쪽도 수정. 이미지는 항상 텍스트 스티커·자막 아래(미리보기 z-index, 내보내기 overlay 순서).

## SRT 불러오기 (자막 탭)
- 편집본 기준: 타임라인 시간 → 각 컷의 원본 시간으로 나눠 `P.subs[mid]`에 저장(겹치는 기존 자막은 교체). 원본 기준: 선택 파일의 `P.subs[mid]`를 교체.
- 여러 줄 자막은 한 줄로 합쳐짐(목록 입력칸이 한 줄이라서).

## ffmpeg 버전
- ffmpeg 8부터 `-filter_complex_script`가 없어짐 → `filter_script_args()`가 지원 여부를 확인해 `-/filter_complex`로 대체.
