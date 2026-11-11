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
사용법: ask.sh [-e low|medium|high] [-t 초] [-a 계정] [-d 폴더] [-f 파일]... [-k] (-p "프롬프트" | -P 프롬프트파일)
  -e  Argon 추론 단계 (기본 medium)
  -t  응답 제한 시간(초, 기본 600)
  -a  이 계정만 사용 (main 또는 accounts.sh로 만든 프로필). 생략하면 자동 선택:
      계정 순서대로 Argon 남은 사용량이 있는 계정을 쓰고, 소진되면 다음 계정으로 넘어간다.
  -d  읽기 전용 폴더 모드. 폴더를 사본(snap/)으로 떠서 그 안을 모델이 직접 탐색하게 한다.
      git 저장소면 HEAD 기준 사본(미커밋 변경은 제외, 경고), 아니면 현재 파일 기준(.git·node_modules 제외).
      사본 상한 200MB. -f 파일은 사본 안 _argon_inputs/ 로 들어간다.
  -f  모델이 읽을 파일. 여러 번 지정 가능. 임시 작업 폴더에 같은 이름으로 복사된다.
  -p  프롬프트 문자열
  -P  프롬프트 파일. 긴 지시는 이쪽을 쓴다.
  -k  끝난 뒤 임시 작업 폴더를 지우지 않고 경로를 stderr에 남긴다.
종료 코드: 0 성공, 1 일반 실패, 2 사용법/입력 오류, 4 모든 계정 소진, 5 로그인된 계정 없음,
       6 응답이 정상 종료되지 않았으나 본문은 출력함(다른 계정으로 재시도하지 않음)
EOF
  exit 2
}

effort=medium timeout=600 prompt="" prompt_file="" keep=0 account="" dir_in=""
files=()
while getopts "e:t:a:d:f:p:P:kh" opt; do
  case "$opt" in
    e) effort=$OPTARG ;;
    t) timeout=$OPTARG ;;
    a) account=$OPTARG ;;
    d) dir_in=$OPTARG ;;
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

# -d 폴더는 작업 폴더를 만들기 전에 확인한다.
if [[ -n "$dir_in" ]]; then
  [[ -d "$dir_in" ]] || { echo "폴더 없음: $dir_in" >&2; exit 2; }
  dir_in=$(cd "$dir_in" && pwd -P)
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/gemini-argon.XXXXXX")
chmod 700 "$work"
if [[ $keep -eq 1 ]]; then
  echo "작업 폴더: $work" >&2
else
  trap 'rm -rf "$work"' EXIT
fi

# agy의 작업 폴더. 폴더 모드에서는 snap 안(저장소와 같은 상대 경로), 아니면 work.
cwd="$work"
mode_dir=0
if [[ -n "$dir_in" ]]; then
  mode_dir=1
  snap="$work/snap"
  mkdir "$snap"
  top=""
  top=$(git -C "$dir_in" rev-parse --show-toplevel 2>/dev/null) || top=""
  if [[ -n "$top" ]]; then
    prefix=$(git -C "$dir_in" rev-parse --show-prefix)
    short=$(git -C "$top" rev-parse --short HEAD 2>/dev/null) || { echo "커밋이 없는 저장소라 사본을 만들 수 없습니다: $dir_in" >&2; exit 2; }
    if [[ -n "$prefix" ]]; then
      git -C "$top" archive HEAD -- "$prefix" | tar -x -C "$snap" || { echo "git archive 실패(HEAD에 없는 경로?): $prefix" >&2; exit 2; }
    else
      git -C "$top" archive HEAD | tar -x -C "$snap" || { echo "git archive 실패" >&2; exit 2; }
    fi
    cwd="$snap/${prefix%/}"
    [[ -d "$cwd" ]] || { echo "HEAD에 없는 경로라 사본이 비어 있습니다: ${prefix:-.}" >&2; exit 2; }
    ndirty=0
    if [[ -n "$prefix" ]]; then
      ndirty=$(git -C "$top" status --porcelain -- "$prefix" | wc -l | tr -d ' ')
    else
      ndirty=$(git -C "$top" status --porcelain | wc -l | tr -d ' ')
    fi
    if [[ "$ndirty" -gt 0 ]]; then
      echo "[gemini-argon] 주의: ${ndirty}개 파일의 미커밋 변경은 사본에 포함되지 않습니다(HEAD $short 기준)." >&2
    fi
    echo "[gemini-argon] 폴더 사본: $dir_in @ $short" >&2
  else
    if command -v rsync >/dev/null; then
      rsync -a --exclude .git --exclude node_modules "$dir_in/" "$snap/"
    else
      cp -R "$dir_in/." "$snap/"
      find "$snap" \( -name .git -o -name node_modules \) -prune -exec rm -rf {} + 2>/dev/null || true
    fi
    cwd="$snap"
    echo "[gemini-argon] 폴더 사본: $dir_in (git 아님, 현재 파일 기준)" >&2
  fi
  snap_kb=$(du -sk "$snap" | awk '{print $1}')
  if [[ "$snap_kb" -gt 204800 ]]; then
    echo "폴더 사본이 상한(200MB)을 넘습니다: ${snap_kb}KB. 더 좁은 폴더를 지정하세요." >&2
    exit 2
  fi
fi

# 입력 파일: 일반 모드는 작업 폴더, 폴더 모드는 cwd 아래 _argon_inputs/ 로 복사한다.
inputs_dir="$cwd"
inputs_rel=""
if [[ $mode_dir -eq 1 && ${#files[@]} -gt 0 ]]; then
  inputs_dir="$cwd/_argon_inputs"
  inputs_rel="_argon_inputs/"
  mkdir -p "$inputs_dir"
fi
listing=""
for f in ${files[@]+"${files[@]}"}; do
  [[ -f "$f" ]] || { echo "파일 없음: $f" >&2; exit 2; }
  dest="$inputs_dir/$(basename "$f")"
  [[ ! -e "$dest" ]] || { echo "같은 이름의 입력 파일이 둘 이상입니다: $(basename "$f")" >&2; exit 2; }
  cp "$f" "$dest"
  listing="$listing, $inputs_rel$(basename "$f")"
done

# 명령 실행(ls 등)은 headless에서 거부되므로 읽을 파일 이름을 프롬프트에 직접 알려 준다.
if [[ $mode_dir -eq 1 ]]; then
  guide="파일을 만들거나 수정하지 말고, 계획서·승인 요청 없이 이번 응답 안에서 답을 끝까지 출력하라. 서브에이전트·브라우저를 쓰지 말고 네가 직접 list_dir·find_by_name·grep_search·view_file 도구로 이 폴더를 탐색하라. 명령 실행은 허용되지 않는다. 인용에는 이 폴더 기준 상대 경로와 줄 번호를 붙여라."
  [[ -z "$listing" ]] || guide="$guide 추가 입력 파일: ${listing#, }."
else
  guide="파일을 만들거나 수정하지 말고, 계획서·승인 요청 없이 바로 답만 출력하라. 서브에이전트·브라우저를 쓰지 말고, 명령 실행은 허용되지 않으니 파일은 읽기 도구로 직접 열어라."
  [[ -z "$listing" ]] || guide="$guide 작업 폴더의 입력 파일: ${listing#, }."
fi

if [[ -n "$prompt_file" ]]; then
  [[ -f "$prompt_file" ]] || { echo "프롬프트 파일 없음: $prompt_file" >&2; exit 2; }
  cp "$prompt_file" "$cwd/.prompt.md"
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
  (cd "$cwd" && HOME="$1" agy -p "$prompt" \
    --model "$model" \
    --mode plan --sandbox \
    --output-format stream-json \
    --print-timeout "${timeout}s") >"$out" 2>"$work/.stderr"
  rc=$?
  set -e
}

# 결과 판정과 출력. 종료 코드: 0 성공, 1 일반 실패, 3 사용량 소진으로 판정, 6 비정상 종료지만 본문 보존
report() {
  python3 -I - "$out" "$work/.stderr" "$rc" "$model" "$1" "$cwd" "$work" <<'PY'
import json, os, re, sys
out, err, rc, model, acct = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
cwd, work = sys.argv[6], sys.argv[7]
raw = open(out, encoding="utf-8", errors="replace").read()
errtxt = open(err, errors="replace").read()

# 본문에 섞인 file://임시경로/ 접두를 지워 상대 경로만 남긴다(/var 와 /private/var 모두).
def clean(t):
    ps = set()
    for p in (cwd, work):
        for q in (p, os.path.realpath(p)):
            ps.add(q)
            if q.startswith("/private/"):
                ps.add(q[len("/private"):])
            else:
                ps.add("/private" + q)
    for q in sorted(ps, key=len, reverse=True):
        t = t.replace("file://" + q.rstrip("/") + "/", "")
    return t

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
    print(clean(text))
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
    print(clean(body))
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
