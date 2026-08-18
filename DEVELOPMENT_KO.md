# GIFFit 개발·빌드 안내

## 구성

- `app.py`: 한국어 Tkinter GUI와 진단용 소스 CLI
- `converter.py`: FFmpeg·Gifsicle 기반 적응형 변환 엔진
- `tests/`: 탐색·메타데이터·취소·GUI 레이아웃 검사
- `build.ps1`: PyInstaller Windows 휴대용 폴더 빌드

## 빌드 환경

1. Windows 10/11 64비트와 Python 3.12를 준비합니다.
2. 프로젝트 폴더에서 다음 명령을 실행합니다.

   ```powershell
   py -3.12 -m venv .venv312
   .\.venv312\Scripts\python.exe -m pip install -r requirements-build.txt
   ```

3. `tools` 폴더에 아래 64비트 실행 파일을 둡니다.

   - FFmpeg 9.0.1 Essentials의 `ffmpeg.exe`, `ffprobe.exe`
   - Gifsicle 1.95의 `gifsicle.exe`

4. 단위 테스트 후 빌드합니다.

   ```powershell
   .\.venv312\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
   .\build.ps1
   ```

5. 결과는 `dist\GIFFit_5MB`에 생성됩니다. 배포할 때는 EXE만 떼지 말고 폴더 전체를 ZIP으로 묶습니다.

## 변환 원칙

- 5,000,000바이트 하드 상한을 게시 직전에 다시 검사합니다.
- 원본과 결과의 프레임 수를 확인할 수 없으면 실패 처리합니다.
- 실제 GIF 후보를 반복 생성해 용량을 측정하며, 프레임 수나 재생 순서를 줄이지 않습니다.
- GIF 규격상 색상은 최대 256색이고 프레임 시간은 10ms 단위입니다.

공개 또는 상업 배포 전에는 `THIRD_PARTY_NOTICES.txt`와 `licenses` 폴더의 FFmpeg·Gifsicle·Python·Tcl/Tk 조건을 검토하세요.
