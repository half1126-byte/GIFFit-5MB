# GIFFit

**프레임을 줄이지 않고, 5MB 안에서 가능한 선명하게.**

GIFFit은 MP4·MOV·AVI·MKV·WebM 영상을 파일당 최대 `5,000,000 bytes`의 애니메이션 GIF로 변환하는 Windows 데스크톱 앱입니다. 영상은 외부 서버로 전송되지 않고 PC 안에서만 처리됩니다.

[Windows 최신 버전 다운로드](https://github.com/half1126-byte/GIFFit-5MB/releases/latest)

## 주요 기능

- 원본 프레임 수와 재생 순서 우선 유지
- 실제 GIF 후보를 반복 측정해 5MB 이하 해상도 자동 탐색
- 화질 우선·균형·해상도 우선 모드
- 256색 전체 영상 팔레트와 Floyd–Steinberg 디더링
- 여러 영상 순차 변환, 진행 표시, 안전 취소
- 결과 프레임 수·재생 길이·무한 반복·전체 디코딩 검증
- 원본 및 기존 결과 파일 덮어쓰기 방지

## 사용법

1. [Releases](https://github.com/half1126-byte/GIFFit-5MB/releases)에서 `GIFFit_5MB_Windows_Portable_v1.0.0.zip`을 받습니다.
2. ZIP을 완전히 압축 해제합니다.
3. `GIFFit_5MB\GIFFit_5MB.exe`를 실행합니다. `_internal` 폴더는 EXE와 함께 보관해야 합니다.
4. 영상을 추가하고 저장 위치와 품질 모드를 선택한 뒤 변환을 시작합니다.

코드 서명이 없는 첫 배포판이므로 Windows SmartScreen 안내가 표시될 수 있습니다.

## 품질에 관하여

GIF는 최대 256색이고 프레임 시간은 10ms 단위입니다. 따라서 AV1 영상처럼 완전히 같은 화질을 만들 수는 없습니다. GIFFit은 프레임을 삭제하지 않고 해상도를 조절하며, 기본 **화질 우선** 모드에서는 Gifsicle의 추가 손실 압축을 사용하지 않습니다. 모든 프레임을 유지한 최소 해상도도 상한을 넘으면 결과를 조용히 손상시키지 않고 실패로 안내합니다.

## 개발·빌드

소스 빌드 방법은 [DEVELOPMENT_KO.md](DEVELOPMENT_KO.md)를 참고하세요. 단위 테스트는 외부 미디어 도구 없이 실행할 수 있습니다.

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

## 라이선스

GIFFit 소스 코드는 [MIT License](LICENSE)로 배포됩니다. Windows 휴대용 패키지에는 별도 프로세스로 실행되는 FFmpeg 9.0.1(GPLv3)과 Gifsicle 1.95(GPLv2)가 포함됩니다. 자세한 내용은 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)와 `licenses` 폴더를 확인하세요.
