---
name: readiz-nas
description: Mac에서 기존에 허용된 NAS 실행 경로로 파일을 조회하고 복사·검증합니다. NAS 저장과 자료 읽기, 대용량 분석에 사용하며 새 macOS 권한창을 요청하지 않습니다. 공개와 삭제는 별도 승인 범위를 따릅니다.
metadata:
  short-description: NAS 파일을 안전하게 읽고 검증합니다
---

# Readiz NAS

NAS 입출력은 설치된 `readiz-nas` CLI와 기존 승인 실행 맥락을 사용한다. `readiz-nas status`는 마운트 메타데이터와 접근 사전조회 결과를 분리해 보여준다. 정상 상태가 입출력 성공을 뜻하지 않으므로 별도 probe를 반복하지 말고, 실제 작업에서 필요한 읽기·쓰기와 결과 재읽기 검증을 수행한다.

설치되지 않았거나 프로필이 없으면 임의 경로나 Python으로 대체하지 않는다. 접근 거부·미결정·사전조회 미지원·도구 샌드박스 오류가 나면 한 번의 결과와 재개 정보를 보고 중단한다. macOS 권한 반복 요청, 설정 창, TCC 초기화, 자동 허용은 하지 않는다. 도구 샌드박스 승인은 macOS 파일 접근 권한과 별개다.

NAS 작업공간 구조와 프로젝트 경로는 비공개 프로필에서 확인한다. 큰 원본과 중간 산출물은 NAS에 보존하고 계산은 Mac에서 한다. 연결이 끊기면 로컬 `/Volumes` 대체 경로를 만들지 말고 멈춘다. NAS 접근 승인은 공개·삭제 승인이 아니며, 게시나 정리는 별도로 승인된 공개 영역·대상에만 수행한다.

일반적인 NAS 작업은 `readiz-nas list`, `readiz-nas read`, `readiz-nas hash`를 사용한다. 파일 복사는 `readiz-nas copy LOCAL_PATH nas:RELATIVE_PATH` 또는 `readiz-nas copy nas:RELATIVE_PATH LOCAL_PATH`로 진행한다. 첫 복사가 중단되면 출력된 재개 정보를 확인하고 같은 명령에 `--resume`을 붙인다. probe가 필요한 검증 작업에서는 `readiz-nas probe`가 자체 생성 파일만 검증 후 제거한다. 긴 기술 한계와 CLI 계약은 [references/limits.md](references/limits.md)를 참고한다.

다운로드 결과, 영상 원본, 글쓰기 자료, 저장 공간 정리의 NAS 저장은 해당 작업 흐름에서 이 스킬의 CLI를 재사용한다. NAS 보관을 이유로 각 작업 스킬의 승인 범위를 확대하지 않는다.
