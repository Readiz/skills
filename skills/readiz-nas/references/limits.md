# CLI 계약과 한계

`readiz-nas`는 개인 설정 `~/.config/readiz-nas/access.json`의 mount/source/launcher/허용 로컬 루트/공간 및 복사 한도를 사용한다. 실제 서버·공유 경로와 프로젝트별 루트는 공개 저장소에 기록하지 않는다. 설정은 설치 시 사용자 환경에 별도로 만든다.

CLI는 `status`, `list PATH`, `read PATH --offset N --max-bytes N`, `hash PATH`, `probe PATH`, `copy SOURCE DEST [--resume]` 명령을 제공한다. NAS 경로는 `nas:상대경로`로 지정한다. 복사는 regular file만 지원하고 NAS→로컬은 허용 로컬 루트 안의 명시 목적지와 설정 한도를 요구한다. 기본 상한은 512 MiB, 최소 여유 공간은 2 GiB다. list는 최대 500항목, read는 최대 1 MiB이며 바이너리 자료는 base64 또는 오류로 처리한다.

복사 중 partial과 목적지가 함께 존재할 수 있어 필요한 여유 공간은 입력 파일 크기의 두 배와 최소 여유 기준을 합해 판단한다. 기본 한도로 NAS에서 로컬로 가져오는 한 건은 확정 전 최대 1 GiB를 사용할 수 있다. 작업자가 보존한 원본과 이전 작업의 산출물은 이 상한에 포함하지 않으므로 작업 전체의 용량 예산도 별도로 확인한다.

새 환경은 실제 mount/source와 실행기, 허용 로컬 루트를 담은 별도 소유자 전용 JSON 프로필을 준비한 뒤 설치한다. 설치는 프로필·wrapper·개인 state 디렉터리만 추가하며 macOS 권한을 부여하지 않는다.

```sh
python3 /PATH/TO/readiz-nas/scripts/install.py \
  --profile "$HOME/.config/readiz-nas-profile.json" \
  --skill-dir /PATH/TO/readiz-nas
```

Network Volumes TCC 사전조회는 비공개 macOS API다. API 없음·예외·미결정·알 수 없는 값은 fail-closed이며 NAS 내용에 접근하지 않는다. `prompt_requested=false`는 해당 조회 코드가 권한 요청 함수를 부르지 않았다는 뜻이지, Apple이 향후 파일 열기를 포함해 팝업이 없음을 보장한다는 뜻이 아니다. 0(허용)도 이후 파일 작업이나 샌드박스·SMB 접근을 보증하지 않는다.

`readiz-nas-python` 고정 실행기가 없으면 중단하고 다른 Python으로 대체하지 않는다. 진행 없는 단계는 기본 15초 제한과 재개 영수증을 사용한다. 실제 스트리밍에 진행이 있으면 전체 작업시간 제한으로 중단하지 않는다. 자식 프로세스를 회수할 수 없는 상태는 그대로 보고하고 추가 작업을 시작하지 않는다.

복사는 원본을 보존하고 기존 목적지를 덮어쓰지 않는다. 파일별 동일 폴더 partial, O_EXCL 예약 파일과 로컬 영수증을 사용한다. 재개 시 원본 identity·SHA-256과 partial prefix를 확인하며, 확정 뒤 목적지 전체를 다시 읽어 크기와 SHA-256을 검증한다. 실패한 복사는 완료로 표시하지 않는다. 이 도구는 임의 명령 실행, 서비스 실행, 권한 설정, 전체 Mac 복사를 제공하지 않는다.

복사 확정은 목적지 이름을 `O_EXCL`로 먼저 예약한 소유 inode에 검증된 partial을 이어 쓰고, 전체 재읽기 해시 검증 뒤 로컬 receipt를 `complete`로 기록하는 방식이다. 확정 전 목적지 경로에 partial 내용이 보일 수 있으므로 receipt의 `complete` 전에는 완성 파일로 취급하지 않는다. 기존 경로를 바꾸지 않는 대신 rename 원자성에 의존하지 않는다.

경로 검사는 기존 symlink 구성요소를 거부하고 파일 leaf는 `O_NOFOLLOW`로 연다. 이는 모든 상위 디렉터리 교체를 원자적으로 고정하는 보장은 아니다. 동시에 수정할 수 있는 비신뢰 사용자가 있는 NAS 폴더는 작업 대상으로 삼지 않는다.
