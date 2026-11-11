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
종료 코드: 0 성공, 1 일반 실패, 2 사용법/입력 오류, 4 모든 계정 소진, 5 로그인된 계정 없음,
       6 응답이 정상 종료되지 않았으나 본문은 출력함(다른 계정으로 재시도하지 않음)
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

# 명령 실행(ls 등)은 headless에서 거부되므로 읽을 파일 이름을 프롬프트에 직접 알려 준다.
listing=""
for f in ${files[@]+"${files[@]}"}; do listing="$listing, $(basename "$f")"; done
guide="파일을 만들거나 수정하지 말고, 계획서·승인 요청 없이 바로 답만 출력하라. 명령 실행은 허용되지 않으니 파일은 읽기 도구로 직접 열어라."
[[ -z "$listing" ]] || guide="$guide 작업 폴더의 입력 파일: ${listing#, }."

if [[ -n "$prompt_file" ]]; then
  [[ -f "$prompt_file" ]] || { echo "프롬프트 파일 없음: $prompt_file" >&2; exit 2; }
  cp "$prompt_file" "$work/.prompt.md"
  prompt="작업 폴더의 .prompt.md 지시를 따르라. $guide"
else
  prompt="$prompt"$'\n\n'"$guide"
fi

out="$work/.response.jsonl"
model="gemini-4-argon-$effort"   # 다른 모델로 대체하지 않는다.
rc=0

# 한 계정으로 호출한다. 결과는 $out, $work/.stderr, 전역 rc에 남는다.
run_agy() {
  set +e
  (cd "$work" && HOME="$1" agy -p "$prompt" \
    --model "$model" \
    --mode plan --sandbox \
    --output-format stream-json \
    --print-timeout "${timeout}s") >"$out" 2>"$work/.stderr"
  rc=$?
  set -e
}

# 결과 판정과 출력. 종료 코드: 0 성공, 1 일반 실패, 3 사용량 소진으로 판정, 6 비정상 종료지만 본문 보존
report() {
  python3 -I - "$out" "$work/.stderr" "$rc" "$model" "$1" <<'PY'
import json, re, sys
out, err, rc, model, acct = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
raw = open(out, encoding="utf-8", errors="replace").read()
errtxt = open(err, errors="replace").read()

# stream-json: 한 줄에 JSON 하나. 결과 이벤트(d)와 init, 단계별 텍스트를 모은다.
d = None
served = None
nonjson = []
steps = {}      # step_index -> [step_type, 이어 붙인 텍스트]
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        nonjson.append(line)
        continue
    if not isinstance(ev, dict):
        nonjson.append(line)
        continue
    kind = ev.get("event")
    if kind == "init":
        served = (ev.get("init") or {}).get("model") or served
    elif kind == "step_update":
        su = ev.get("step_update") or {}
        idx = su.get("step_index")
        if idx is None:
            continue
        st = steps.setdefault(idx, ["", ""])
        if su.get("step_type"):
            st[0] = su["step_type"]
        if su.get("tool_name") and not st[0]:
            st[0] = "tool"
        if su.get("text_delta"):
            st[1] += su["text_delta"]
    elif kind == "result" and isinstance(ev.get("result"), dict):
        d = ev["result"]
    elif "status" in ev and "event" not in ev:
        d = ev   # 옛 json 형식 호환

# 마지막 tool 단계 이후의 agent_response 텍스트를 순서대로 이은 것 = full
tool_idx = [i for i, st in steps.items() if st[0] == "tool"]
last_tool = max(tool_idx) if tool_idx else -1
full = "".join(st[1] for i, st in sorted(steps.items())
               if st[0] == "agent_response" and i > last_tool)
resp = d.get("response") if isinstance(d, dict) else None
resp = resp if isinstance(resp, str) else ""

if isinstance(d, dict) and d.get("status") == "SUCCESS" and rc == 0:
    text = resp
    # 이어 쓰기가 SUCCESS로 끝난 경우: response가 뒷부분뿐이면 full을 낸다.
    if len(full.strip()) > len(resp.strip()) + 20 and resp.strip() and full.rstrip().endswith(resp.strip()):
        text = full
    u = d.get("usage") or {}
    mdl = f"model={model}(요청) served={served}" if served else f"model={model}(요청값)"
    sys.stderr.write(
        f"[gemini-argon] account={acct} {mdl} status={d.get('status')} conversation={d.get('conversation_id')} "
        f"seconds={d.get('duration_seconds')} tokens={u.get('total_tokens')}\n"
    )
    if not text.strip():
        text = full
    if not text.strip():
        sys.stderr.write(f"[gemini-argon] account={acct} 실패: 성공으로 끝났지만 답이 비어 있습니다. 대화 {d.get('conversation_id')}에서 도구 거부 등을 확인하세요.\n")
        sys.exit(1)
    print(text)
    sys.exit(0)

# 비SUCCESS: 소진 판정은 error·stderr·비JSON 원문에만 적용한다(본문 단어 오판 방지).
errfield = str(d.get("error") or "") if isinstance(d, dict) else ""
hay = "\n".join(["\n".join(nonjson)[-2000:], errtxt[-2000:], errfield])
m = re.search(r"quota|credits balance|RESOURCE_EXHAUSTED|429|rate.?limit|limit (reached|exceeded)|exhausted", hay, re.I)
if m:
    s = max(0, m.start() - 80)
    sys.stderr.write(f"[gemini-argon] account={acct} 사용량 소진으로 판정, 근거 원문: ...{hay[s:m.end() + 120]!r}\n")
    sys.exit(3)

body = full if full.strip() else resp
if body.strip():
    reason = (errfield.strip().splitlines() or [""])[0] or (f"status={d.get('status')}" if isinstance(d, dict) else f"결과 이벤트 없음, rc={rc}")
    sys.stderr.write(f"[gemini-argon] 경고: 응답이 정상 종료되지 않았습니다(사유: {reason}). 본문은 보존해 출력합니다.\n")
    print(body)
    sys.exit(6)

tail = "\n".join(nonjson)[-2000:] or raw[-2000:]
sys.stderr.write(f"agy 실패(account={acct}, rc={rc}):\n{tail}\n{errtxt[-2000:]}\n")
sys.exit(1)
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
