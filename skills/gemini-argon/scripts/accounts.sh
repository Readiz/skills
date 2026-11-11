#!/usr/bin/env bash
# gemini-argon 계정 관리: 목록(남은 사용량)과 로그인 안내.
# 로그인 정보(파일·키체인)는 읽거나 출력하지 않는다. 이메일도 표시하지 않는다.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib-accounts.sh
. "$here/lib-accounts.sh"

usage() {
  cat >&2 <<'EOF2'
사용법:
  accounts.sh list            계정 순서대로 로그인 여부, Argon 주간 남은 %와 리셋 시각, Gemini 5시간 남은 %
  accounts.sh login <이름>    프로필 폴더를 만들고 직접 실행할 로그인 명령을 안내 (이름: [A-Za-z0-9_-]+, main 예약)
계정 순서: GEMINI_ARGON_ACCOUNTS(쉼표 구분) 또는 main + profiles 정렬순.
EOF2
  exit 2
}

pct() { awk -v f="$1" 'BEGIN{printf "%.0f%%", f*100}'; }

login_hint() {
  if [[ "$1" == "main" ]]; then
    echo "  로그인 필요: 자기 터미널에서 agy 를 실행해 로그인하세요."
  else
    echo "  로그인 필요: 자기 터미널에서 실행하세요 -> HOME=$(ga_home_for "$1") agy"
  fi
}

cmd_list() {
  local name state af ar gf gr
  while IFS= read -r name; do
    if [[ "$name" != "main" ]] && ! ga_exists "$name"; then
      echo "[$name] 프로필 폴더 없음. 'accounts.sh login $name' 으로 만드세요."
      continue
    fi
    IFS=$'\t' read -r state af ar gf gr <<<"$(ga_usage "$name")"
    case "$state" in
      ok)
        echo "[$name] 로그인됨  Argon 주간 남은 $(pct "$af") (리셋 $ar)  Gemini 5시간 남은 $( [[ -n "$gf" ]] && pct "$gf" || echo '?' )${gr:+ (리셋 $gr)}" ;;
      nologin)
        echo "[$name] 로그인 필요"
        login_hint "$name" ;;
      *)
        echo "[$name] 사용량 조회 실패(네트워크 등). 잠시 뒤 다시 시도하세요." ;;
    esac
  done < <(ga_account_order)
}

cmd_login() {
  local name=${1:-}
  [[ -n "$name" ]] || usage
  if [[ "$name" == "main" ]]; then
    echo "main은 실제 HOME 계정입니다. 자기 터미널에서 agy 를 실행해 로그인하세요." >&2
    exit 2
  fi
  ga_valid_name "$name" || { echo "계정 이름은 [A-Za-z0-9_-]+ 만 허용합니다: $name" >&2; exit 2; }
  local dir; dir=$(ga_home_for "$name")
  if [[ -d "$dir" ]]; then
    echo "이미 있는 프로필입니다: $dir"
  else
    mkdir -p "$GA_ROOT" "$GA_PROFILES"
    chmod 700 "$GA_ROOT" "$GA_PROFILES"
    mkdir "$dir"
    chmod 700 "$dir"
    echo "프로필 폴더를 만들었습니다: $dir"
  fi
  cat <<EOF2
대화형 로그인은 TTY와 브라우저가 필요해 스크립트가 직접 하지 않습니다.
자기 터미널에서 아래 명령을 실행해 로그인한 뒤 종료하세요.

  HOME=$dir agy

확인: accounts.sh list
EOF2
}

case "${1:-}" in
  list) cmd_list ;;
  login) shift; cmd_login "${1:-}" ;;
  *) usage ;;
esac
