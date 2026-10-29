# 다운로드 동작과 결과

`download(url, destination, *, report_path=None, expected_type=None, expected_sha256=None, max_bytes=104857600, min_free_bytes=1073741824, timeout=45, guard=None, retry=False) -> dict`를 사용한다. `destination`과 `report_path`는 문자열 또는 `pathlib.Path`를 받는다. `report_path`가 없으면 목적지 옆에 `<filename>.receipt.json`을 기록한다. 기존 report가 인식된 receipt이고 같은 URL fingerprint를 가리킬 때만 갱신한다. 임의 기존 파일, 다른 출처의 receipt, 목적지와 같은 경로는 보존하고 요청을 거부한다. 실패도 receipt를 반환하며 CLI는 실패 시 0이 아닌 종료 코드다. 부분 파일 재개는 지원하지 않는다. 성공한 파일의 재사용은 가능하다.

기본 URL은 HTTP(S)이며 userinfo가 없어야 한다. TLS 인증서 검증을 유지하고 HTTPS에서 HTTP로 내려가는 리다이렉트는 거부한다. 요청은 정직한 `ReadizDownload/1.0` UA와 콘텐츠 종류에 맞는 Accept를 사용한다. 인증정보가 없는 직접 요청만 지원한다. `expected_type`은 pdf, csv, docx/xlsx/pptx, zip/archive, gzip/tar, odt/ods/odp, doc/xls/ppt, txt/json/rtf/xml 중에서 선택한다. 미지원 값은 요청 전에 실패한다.

| 결과 | 의미와 처리 |
|---|---|
| `http_401`, `http_403` | 인증/접근 거부. 응답 제목과 제한된 본문을 확인한다. 403만으로 IP 차단이라 부르지 않는다. |
| `http_429` | `Retry-After`를 receipt에 기록한다. 만료 전에는 `retry=True`여도 요청하지 않는다. |
| `explicit_block`, `challenge` | 본문 명시적 차단은 `explicit_block`, Cloudflare `cf-mitigated: challenge`나 챌린지 본문은 `challenge`다. 우선 공급자 추가 요청을 멈추고 보고한다. 재개 지시나 사전 승인된 재개 조건이 있으면 `retry=True`를 사용할 수 있다. HTTP 429는 본문 문구와 무관하게 `http_429`로 분류해 Retry-After를 보존한다. |
| `http_5xx`, `http_other` | HTTP 오류. 자동 재시도하지 않는다. |
| `network_error` | HTTP 응답을 받지 못한 네트워크/TLS/시간 초과 오류다. HTTP 차단으로 해석하지 않는다. |
| `html_instead_of_file` | 성공 상태여도 HTML/챌린지 페이지가 파일인 척 반환됐다. |
| `truncated` | Content-Length와 수신 바이트가 맞지 않는다. |
| `size_limit`, `insufficient_space` | 설정 용량 한도 또는 여유 공간 조건을 충족하지 못했다. |
| `hash_mismatch`, `type_mismatch`, `destination_exists` | 검증 실패 또는 기존 파일 보호. 기존 목적지 파일은 덮어쓰지 않는다. |

오류 진단은 본문 최대 8 KiB를 읽고 제목과 마스킹된 excerpt를 최대 500자로 제한한다. 전체 본문/헤더, 원본 URL, query, fragment, 인증정보를 저장하지 않는다. 저장 헤더는 Content-Type, Content-Length, Retry-After, cf-mitigated만 허용한다. URL fingerprint는 원본 URL의 SHA-256이며 원문 복구용 정보가 아니다.

성공 receipt가 재사용되려면 동일한 URL fingerprint, 목적지 크기와 SHA-256이 일치해야 한다. 파일은 다운로드 뒤 스트리밍 재읽기로 크기와 해시를 검증한다. 출처가 바뀌거나 파일 검증이 실패하면 재사용하지 않는다. 실패 receipt는 기본 보존하며 `retry=True`에서만 재요청할 수 있다. 명시적 차단/챌린지는 사용자의 재개 지시나 사전 승인된 재개 조건 없이 `retry=True`를 설정하지 않는다. 429 대기 정보와 본문은 Retry-After 만료 전 재시도 시에도 원 receipt에 보존된다.

검증된 부분 파일은 우선 하드링크로 목적지에 확정한다. 파일시스템이 하드링크를 지원하지 않는 경우에만 배타적 생성으로 빈 목적지 예약을 만들고, 열린 파일과 목적지의 device/inode가 일치하는지 확인한 뒤 `os.replace`로 검증 파일을 원자적으로 바꾼다. 이 빈 예약 파일은 완료 자료가 아니다. 소비자는 다운로드를 쓰거나 읽을 때 `status == "succeeded"` receipt가 있는지 확인한다. 중간 실패에서는 현재 실행이 만든 동일 inode 예약만 다시 mount/guard 확인 후 정리한다. 저장 오류 receipt에는 안전한 errno, os.strerror 메시지와 실패 stage가 기록된다.

요청을 새로 시작하면 항상 같은 디렉터리의 고유 부분 파일을 사용하고 검증된 뒤에만 목적지 이름을 덮어쓰지 않는 방식으로 확정한다. 호출자가 제공한 `guard(stage, path)`는 파일과 receipt 쓰기 전, 스트리밍 중, 확정·예약·정리 전에 실행하며 예외가 나면 IO를 중단한다. Stage에는 `before_write`, `during_write`, `before_commit`, `before_hardlink`, `before_reservation`, `before_replace`, `before_reservation_cleanup`, `before_receipt`가 전달될 수 있다. API와 CLI는 `/Volumes` 경로가 실제 마운트 아래인지 각 쓰기 단계에서 확인해 누락된 마운트에 로컬 대체 파일을 만들지 않는다. API 호출자는 NAS의 공유·파일시스템 종류와 쓰기 권한·여유 공간을 별도로 확인한다.

CLI 예: `python3 scripts/download.py URL /path/to/file.pdf --expected-type pdf --report /path/to/receipt.json`. `/Volumes` 아래 목적지는 실제 마운트 지점인지 확인한다. 출력 receipt에는 원본 signed URL이 없다.
