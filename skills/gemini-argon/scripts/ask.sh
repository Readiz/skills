#!/usr/bin/env bash
# Antigravity CLI(agy)로 Gemini 4 Argon에 한 번 묻고 응답 본문만 출력한다.
# 입력 파일은 권한 700 임시 작업 폴더에 복사해 그 안에서만 읽게 한다.
# 계정이 여러 개면 남은 사용량이 있는 계정을 순서대로 쓰고, 소진되면 다음 계정으로 같은 요청을 재시도한다.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib-accounts.sh
. "$here/lib-accounts.sh"

usage() {
  cat >&2 <<'EOF'
사용법: ask.sh [-e low|medium|high] [-t 초] [-a 계정] [-f 파일]... [-k] (-p "프롬프트" | -P 프롬프트파일)
  -e  Argon 추론 단계 (기본 medium)
  -t  응답 제한 시간(초, 기본 600)
  -a  이 계정만 사용 (main 또는 accounts.sh로 만든 프로필). 생략하면 자동 선택:
      계정 순서대로 Argon 남은 사용량이 있는 계정을 쓰고, 소진되면 다음 계정으로 넘어간다.
  -f  모델이 읽을 파일. 여러 번 지정 가능. 임시 작업 폴더에 같은 이름으로 복사된다.
  -p  프롬프트 문자열
  -P  프롬프트 파일. 긴 지시는 이쪽을 쓴다.
  -k  끝난 뒤 임시 작업 폴더를 지우지 않고 경로를 stderr에 남긴다.
종료 코드: 0 성공, 1 일반 실패, 2 사용법/입력 오류, 4 모든 계정 소진, 5 로그인된 계정 없음
EOF
  exit 2
}

effort=medium timeout=600 prompt="" prompt_file="" keep=0 account=""
files=()
while getopts "e:t:a:f:p:P:kh" opt; do
  case "$opt" in
    e) effort=$OPTARG ;;
    t) timeout=$OPTARG ;;
    a) account=$OPTARG ;;
    f) files+=("$OPTARG") ;;
    p) prompt=$OPTARG ;;
    P) prompt_file=$OPTARG ;;
    k) keep=1 ;;
    *) usage ;;
  esac
done
case "$effort" in low|medium|high) ;; *) usage ;; esac
[[ -n "$prompt" || -n "$prompt_file" ]] || usage
[[ -z "$prompt" || -z "$prompt_file" ]] || usage
command -v agy >/dev/null || { echo "agy(Antigravity CLI)를 찾을 수 없습니다." >&2; exit 127; }

# 계정 검증은 작업 폴더를 만들기 전에 끝낸다.
if [[ -n "$account" ]]; then
  if [[ "$account" != "main" ]] && ! ga_valid_name "$account"; then
    echo "계정 이름은 [A-Za-z0-9_-]+ 만 허용합니다: $account" >&2; exit 2
  fi
  ga_exists "$account" || { echo "계정 '$account' 없음. 'accounts.sh login $account' 으로 먼저 만드세요." >&2; exit 2; }
  ga_has_login "$account" || { echo "계정 '$account'는 로그인되지 않았습니다. 'accounts.sh list'로 로그인 명령을 확인하세요." >&2; exit 2; }
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/gemini-argon.XXXXXX")
chmod 700 "$work"
if [[ $keep -eq 1 ]]; then
  echo "작업 폴더: $work" >&2
else
  trap 'rm -rf "$work"' EXIT
fi

for f in ${files[@]+"${files[@]}"}; do
  [[ -f "$f" ]] || { echo "파일 없음: $f" >&2; exit 2; }
  dest="$work/$(basename "$f")"
  [[ ! -e "$dest" ]] || { echo "같은 이름의 입력 파일이 둘 이상입니다: $(basename "$f")" >&2; exit 2; }
  cp "$f" "$dest"
done

if [[ -n "$prompt_file" ]]; then
  [[ -f "$prompt_file" ]] || { echo "프롬프트 파일 없음: $prompt_file" >&2; exit 2; }
  cp "$prompt_file" "$work/.prompt.md"
  prompt="작업 폴더의 .prompt.md 지시를 따르라. 파일을 만들거나 수정하지 말고 답만 출력하라."
fi

out="$work/.response.json"
model="gemini-4-argon-$effort"   # 다른 모델로 대체하지 않는다.
rc=0

# 한 계정으로 호출한다. 결과는 $out, $work/.stderr, 전역 rc에 남는다.
run_agy() {
  set +e
  (cd "$work" && HOME="$1" agy -p "$prompt" \
    --model "$model" \
    --mode plan --sandbox \
    --output-format json \
    --print-timeout "${timeout}s") >"$out" 2>"$work/.stderr"
  rc=$?
  set -e
}

# 결과 판정과 출력. 종료 코드: 0 성공, 1 일반 실패, 3 사용량 소진으로 판정
report() {
  python3 -I - "$out" "$work/.stderr" "$rc" "$model" "$1" <<'PY'
import json, re, sys
out, err, rc, model, acct = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
raw = open(out, encoding="utf-8", errors="replace").read().strip()
errtxt = open(err, errors="replace").read()
try:
    d = json.loads(raw.splitlines()[-1]) if raw else None
except (json.JSONDecodeError, IndexError):
    d = None
if not (isinstance(d, dict) and d.get("status") == "SUCCESS" and rc == 0):
    # 성공이 아닐 때만 오류 필드·응답·stderr 원문에서 사용량 소진 문구를 찾는다.
    parts = [raw[-2000:], errtxt[-2000:]]
    if isinstance(d, dict):
        parts += [str(d.get("error", "")), str(d.get("response", ""))]
    hay = "\n".join(parts)
    m = re.search(r"quota|credits balance|RESOURCE_EXHAUSTED|429|rate.?limit|limit (reached|exceeded)|exhausted", hay, re.I)
    if m:
        s = max(0, m.start() - 80)
        sys.stderr.write(f"[gemini-argon] account={acct} 사용량 소진으로 판정, 근거 원문: ...{hay[s:m.end() + 120]!r}\n")
        sys.exit(3)
    sys.stderr.write(f"agy 실패(account={acct}, rc={rc}):\n{raw[-2000:]}\n{errtxt[-2000:]}\n")
    sys.exit(1)
u = d.get("usage") or {}
sys.stderr.write(
    f"[gemini-argon] account={acct} model={model}(요청값) status={d.get('status')} conversation={d.get('conversation_id')} "
    f"seconds={d.get('duration_seconds')} tokens={u.get('total_tokens')}\n"
)
print(d.get("response", ""))
PY
}

# -a 지정: 그 계정만 쓰고 다른 계정으로 넘어가지 않는다.
if [[ -n "$account" ]]; then
  run_agy "$(ga_home_for "$account")"
  r=0; report "$account" || r=$?
  if [[ $r -eq 3 ]]; then
    echo "계정 '$account'의 사용량이 소진된 것으로 보입니다. (-a 지정 시 다른 계정으로 넘어가지 않음)" >&2
    exit 4
  fi
  exit "$r"
fi

# 자동: 계정 순서대로 시도
exhausted=""
while IFS= read -r name; do
  if ! ga_exists "$name"; then
    echo "[gemini-argon] account=$name 건너뜀: 프로필 폴더 없음" >&2; continue
  fi
  ustate="" afrac="" areset=""
  IFS=$'\t' read -r ustate afrac areset _ _ <<<"$(ga_usage "$name")" || true
  case "$ustate" in
    nologin)
      echo "[gemini-argon] account=$name 건너뜀: 로그인 필요 (accounts.sh list 참고)" >&2
      continue ;;
    ok)
      if awk -v f="$afrac" 'BEGIN{exit !(f <= 0.01)}'; then
        echo "[gemini-argon] account=$name 건너뜀: Argon 주간 사용량 소진 (리셋 $areset)" >&2
        exhausted="$exhausted$name"$'\n'; continue
      fi ;;
    *)
      echo "[gemini-argon] account=$name 사용량 조회 실패, 일단 시도합니다." >&2 ;;
  esac
  run_agy "$(ga_home_for "$name")"
  r=0; report "$name" || r=$?
  if [[ $r -eq 3 ]]; then
    echo "[gemini-argon] account=$name 소진, 다음 계정으로 같은 요청을 재시도합니다." >&2
    exhausted="$exhausted$name"$'\n'
    continue
  fi
  exit "$r"
done < <(ga_account_order)

if [[ -z "$exhausted" ]]; then
  echo "사용 가능한 로그인된 계정이 없습니다. 'accounts.sh list'로 확인하세요." >&2
  exit 5
fi
echo "모든 사용 가능 계정의 Argon 사용량이 소진되었습니다. 리셋 시각 요약:" >&2
while IFS= read -r name; do
  [[ -n "$name" ]] || continue
  areset=""
  IFS=$'\t' read -r _ _ areset _ _ <<<"$(ga_usage "$name")" || true
  echo "  $name: Argon 주간 리셋 ${areset:-조회 실패}" >&2
done <<<"$exhausted"
exit 4
