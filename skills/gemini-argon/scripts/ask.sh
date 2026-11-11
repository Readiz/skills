#!/usr/bin/env bash
# Antigravity CLI(agy)로 Gemini 4 Argon에 한 번 묻고 응답 본문만 출력한다.
# 입력 파일은 권한 700 임시 작업 폴더에 복사해 그 안에서만 읽게 한다.
# 계정이 여러 개면 남은 사용량이 있는 계정을 순서대로 쓰고, 소진되면 다음 계정으로 같은 요청을 재시도한다.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib-accounts.sh
. "$here/lib-accounts.sh"

usage() {
  cat >&2 <<'USAGE'
사용법: ask.sh [-e low|medium|high] [-t 초] [-a 계정] [-d 폴더]... [-f 파일]... [-o 폴더] [-k] (-p "프롬프트" | -P 프롬프트파일)
  -e  Argon 추론 단계 (기본 medium)
  -t  응답 제한 시간(초, 기본 600)
  -a  이 계정만 사용 (main 또는 accounts.sh로 만든 프로필). 생략하면 자동 선택:
      계정 순서대로 Argon 남은 사용량이 있는 계정을 쓰고, 소진되면 다음 계정으로 넘어간다.
  -d  읽기 전용 폴더 모드. 폴더를 사본(snap/)으로 떠서 그 안을 모델이 직접 탐색하게 한다. 여러 번 지정 가능.
      git 저장소면 HEAD 기준 사본(미커밋 변경은 제외, 경고). HEAD에 없는(추적 밖·무시된) 경로는 현재 파일 기준 복사.
      -d가 둘 이상이면 모두 같은 저장소 안이어야 하며 사본은 저장소 루트와 같은 구조(agy cwd = 사본 루트).
      git이 아닌 폴더는 하나만 가능(현재 파일 기준, .git·node_modules 제외). 사본 전체 상한 200MB.
      -f 파일은 사본 안 _argon_inputs/ 로 들어간다.
  -f  모델이 읽을 파일. 여러 번 지정 가능. 임시 작업 폴더에 같은 이름으로 복사된다.
  -p  프롬프트 문자열
  -P  프롬프트 파일. 긴 지시는 이쪽을 쓴다.
  -o  결과 저장 폴더. <폴더>/<날짜-시각>-<계정>/ 에 result.md, stderr.log, prompt.md, meta.json 을 남긴다.
  -k  끝난 뒤 임시 작업 폴더를 지우지 않고 경로를 stderr에 남긴다.
종료 코드: 0 성공, 1 일반 실패, 2 사용법/입력 오류, 4 모든 계정 소진, 5 로그인된 계정 없음,
       6 응답이 정상 종료되지 않았으나 본문은 출력함(다른 계정으로 재시도하지 않음)
USAGE
  exit 2
}

effort=medium timeout=600 prompt="" prompt_file="" keep=0 account="" out_root=""
dirs=()
files=()
while getopts "e:t:a:d:f:p:P:o:kh" opt; do
  case "$opt" in
    e) effort=$OPTARG ;;
    t) timeout=$OPTARG ;;
    a) account=$OPTARG ;;
    d) dirs+=("$OPTARG") ;;
    f) files+=("$OPTARG") ;;
    p) prompt=$OPTARG ;;
    P) prompt_file=$OPTARG ;;
    o) out_root=$OPTARG ;;
    k) keep=1 ;;
    *) usage ;;
  esac
done
case "$effort" in low|medium|high) ;; *) usage ;; esac
[[ -n "$prompt" || -n "$prompt_file" ]] || usage
[[ -z "$prompt" || -z "$prompt_file" ]] || usage
command -v agy >/dev/null || { echo "agy(Antigravity CLI)를 찾을 수 없습니다." >&2; exit 127; }

# ---- 결과 저장(-o)용 상태 ----
work="" record=0 teepid="" tdir="" errlog=""
last_account="" last_before="?" last_after="?" prompt_sent=""
created_at=$(date +%Y-%m-%dT%H:%M:%S%z)
t_start=$(date +%Y%m%d-%H%M%S)
model="gemini-4-argon-$effort"   # 다른 모델로 대체하지 않는다.

# -o 가 있으면 stderr 를 tee 로 파일에도 남긴다(화면 출력은 유지). fd 9 = 원래 stderr.
if [[ -n "$out_root" ]]; then
  tdir=$(mktemp -d "${TMPDIR:-/tmp}/gemini-argon-log.XXXXXX")
  errlog="$tdir/stderr.log"
  mkfifo "$tdir/fifo"
  exec 9>&2
  tee "$errlog" <"$tdir/fifo" >&9 &
  teepid=$!
  exec 2>"$tdir/fifo"
fi

# 종료 시 결과 저장과 정리. $? 는 스크립트 종료 코드.
finish() {
  local code=$?
  set +e
  trap - EXIT
  local saved=""
  if [[ -n "$out_root" ]]; then
    if [[ $record -eq 1 ]]; then
      saved=$(save_result "$code")
    fi
    # tee 를 닫고 flush 를 기다린다.
    exec 2>&9
    [[ -z "$teepid" ]] || wait "$teepid" 2>/dev/null
    if [[ -n "$saved" ]]; then
      cp "$errlog" "$saved/stderr.log" 2>/dev/null && chmod 600 "$saved/stderr.log"
      echo "[gemini-argon] 결과 저장: $saved" >&2
    fi
    rm -rf "$tdir"
  fi
  if [[ $keep -ne 1 && -n "$work" ]]; then rm -rf "$work"; fi
  exit "$code"
}

# <폴더>/<날짜-시각>-<계정>/ 에 result.md, prompt.md, meta.json 을 쓰고 경로를 출력한다.
save_result() {
  local code=$1 acct="${last_account:-none}" dest n=1
  mkdir -p "$out_root" || return 0
  dest="$out_root/$t_start-$acct"
  while ! mkdir -m 700 "$dest" 2>/dev/null; do
    n=$((n + 1)); dest="$out_root/$t_start-$acct-$n"
    [[ $n -lt 100 ]] || return 0
  done
  dest=$(cd "$dest" && pwd -P)
  cp "$work/.result" "$dest/result.md" 2>/dev/null || : >"$dest/result.md"
  {
    printf '%s\n' "$prompt_sent"
    if [[ -n "$prompt_file" && -f "$prompt_file" ]]; then
      printf '\n---\n[.prompt.md]\n---\n'
      cat "$prompt_file"
    fi
  } >"$dest/prompt.md"
  GA_ACCT="$acct" GA_MODEL="$model" GA_EFFORT="$effort" GA_CODE="$code" GA_WORK="$work" \
  GA_BEFORE="$last_before" GA_AFTER="$last_after" GA_CREATED="$created_at" python3 -I - "$dest/meta.json" <<'PYM'
import json, os, sys
w = os.environ["GA_WORK"]
def lines(name):
    try:
        return [l.rstrip("\n") for l in open(os.path.join(w, name), encoding="utf-8") if l.strip()]
    except OSError:
        return []
try:
    side = json.load(open(os.path.join(w, ".report.json"), encoding="utf-8"))
except Exception:
    side = {}
code = int(os.environ["GA_CODE"])
status = side.get("status") or {4: "EXHAUSTED", 5: "NO_LOGIN"}.get(code, "NONE")
folders = []
for l in lines(".folders"):
    p = l.split("\t")
    p += [""] * (3 - len(p))
    folders.append({"path": p[0], "mode": p[1], "head": p[2] or None})
meta = {
    "account": os.environ["GA_ACCT"],
    "model": os.environ["GA_MODEL"],
    "served": side.get("served"),
    "status": status,
    "exit_code": code,
    "conversation_id": side.get("conversation_id"),
    "seconds": side.get("seconds"),
    "tokens": side.get("tokens"),
    "effort": os.environ["GA_EFFORT"],
    "folders": folders,
    "inputs": lines(".inputs"),
    "argon_before": os.environ["GA_BEFORE"],
    "argon_after": os.environ["GA_AFTER"],
    "attempts": lines(".attempts"),
    "created_at": os.environ["GA_CREATED"],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2)
    f.write("\n")
PYM
  chmod 600 "$dest"/*
  printf '%s\n' "$dest"
}
trap finish EXIT

# 계정 검증은 작업 폴더를 만들기 전에 끝낸다.
if [[ -n "$account" ]]; then
  if [[ "$account" != "main" ]] && ! ga_valid_name "$account"; then
    echo "계정 이름은 [A-Za-z0-9_-]+ 만 허용합니다: $account" >&2; exit 2
  fi
  ga_exists "$account" || { echo "계정 '$account' 없음. 'accounts.sh login $account' 으로 먼저 만드세요." >&2; exit 2; }
  ga_has_login "$account" || { echo "계정 '$account'는 로그인되지 않았습니다. 'accounts.sh list'로 로그인 명령을 확인하세요." >&2; exit 2; }
fi

# -d 폴더는 작업 폴더를 만들기 전에 모두 확인한다.
rdirs=() tops=()
ngit=0 nplain=0 common_top=""
for d in ${dirs[@]+"${dirs[@]}"}; do
  [[ -d "$d" ]] || { echo "폴더 없음: $d" >&2; exit 2; }
  rd=$(cd "$d" && pwd -P)
  t=$(git -C "$rd" rev-parse --show-toplevel 2>/dev/null) || t=""
  if [[ -n "$t" ]]; then
    if [[ -n "$common_top" && "$t" != "$common_top" ]]; then
      echo "서로 다른 git 저장소의 폴더는 함께 지정할 수 없습니다: $common_top / $t" >&2; exit 2
    fi
    common_top=$t; ngit=$((ngit + 1))
  else
    nplain=$((nplain + 1))
  fi
  rdirs+=("$rd"); tops+=("$t")
done
ndirs=${#rdirs[@]}
if [[ $ndirs -gt 1 && $nplain -gt 0 ]]; then
  if [[ $ngit -gt 0 ]]; then
    echo "git 저장소 폴더와 git이 아닌 폴더는 함께 지정할 수 없습니다." >&2
  else
    echo "git이 아닌 폴더는 하나만 지정할 수 있습니다(둘 이상 불가)." >&2
  fi
  exit 2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/gemini-argon.XXXXXX")
chmod 700 "$work"
[[ $keep -ne 1 ]] || echo "작업 폴더: $work" >&2
: >"$work/.result"

# agy의 작업 폴더. 폴더 모드에서는 snap 안(저장소와 같은 상대 경로), 아니면 work.
cwd="$work"
mode_dir=0
explore_list=""
if [[ $ndirs -gt 0 ]]; then
  mode_dir=1
  snap="$work/snap"
  mkdir "$snap"
  i=0
  while [[ $i -lt $ndirs ]]; do
    dir_in=${rdirs[$i]} top=${tops[$i]}
    i=$((i + 1))
    if [[ -n "$top" ]]; then
      prefix=$(git -C "$dir_in" rev-parse --show-prefix)
      short=$(git -C "$top" rev-parse --short HEAD 2>/dev/null) || { echo "커밋이 없는 저장소라 사본을 만들 수 없습니다: $dir_in" >&2; exit 2; }
      intree=$(git -C "$top" ls-tree --name-only HEAD -- "${prefix:-.}" 2>/dev/null) || intree=""
      if [[ -n "$intree" ]]; then
        if [[ -n "$prefix" ]]; then
          git -C "$top" archive HEAD -- "$prefix" | tar -x -C "$snap" || { echo "git archive 실패(HEAD에 없는 경로?): $prefix" >&2; exit 2; }
        else
          git -C "$top" archive HEAD | tar -x -C "$snap" || { echo "git archive 실패" >&2; exit 2; }
        fi
        if [[ -n "$prefix" ]]; then
          ndirty=$(git -C "$top" status --porcelain -- "$prefix" | wc -l | tr -d ' ')
        else
          ndirty=$(git -C "$top" status --porcelain | wc -l | tr -d ' ')
        fi
        if [[ "$ndirty" -gt 0 ]]; then
          echo "[gemini-argon] 주의: ${ndirty}개 파일의 미커밋 변경은 사본에 포함되지 않습니다(HEAD $short 기준)." >&2
        fi
        echo "[gemini-argon] 폴더 사본: $dir_in @ $short" >&2
        printf '%s\tgit\t%s\n' "$dir_in" "$short" >>"$work/.folders"
      else
        # HEAD에 없는 경로(git이 무시하거나 추적하지 않음): 작업 트리의 현재 파일을 복사한다.
        mkdir -p "$snap/$prefix"
        if command -v rsync >/dev/null; then
          rsync -a --exclude .git --exclude node_modules "$dir_in/" "$snap/$prefix"
        else
          cp -R "$dir_in/." "$snap/$prefix"
          find "$snap/$prefix" \( -name .git -o -name node_modules \) -prune -exec rm -rf {} + 2>/dev/null || true
        fi
        echo "[gemini-argon] 폴더 사본: $dir_in (git 추적 밖, 현재 파일 기준)" >&2
        printf '%s\tuntracked\t\n' "$dir_in" >>"$work/.folders"
      fi
      [[ -d "$snap/${prefix%/}" ]] || { echo "사본이 비어 있습니다: ${prefix:-.}" >&2; exit 2; }
      rel=${prefix%/}
      [[ -n "$rel" ]] || rel="."
      explore_list="${explore_list:+$explore_list, }$rel"
      single_cwd="$snap/${prefix%/}"
    else
      if command -v rsync >/dev/null; then
        rsync -a --exclude .git --exclude node_modules "$dir_in/" "$snap/"
      else
        cp -R "$dir_in/." "$snap/"
        find "$snap" \( -name .git -o -name node_modules \) -prune -exec rm -rf {} + 2>/dev/null || true
      fi
      single_cwd="$snap"
      echo "[gemini-argon] 폴더 사본: $dir_in (git 아님, 현재 파일 기준)" >&2
      printf '%s\tplain\t\n' "$dir_in" >>"$work/.folders"
    fi
  done
  if [[ $ndirs -eq 1 ]]; then cwd=$single_cwd; else cwd=$snap; fi
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
  basename "$f" >>"$work/.inputs"
done

# 명령 실행(ls 등)은 headless에서 거부되므로 읽을 파일 이름을 프롬프트에 직접 알려 준다.
if [[ $mode_dir -eq 1 ]]; then
  if [[ $ndirs -gt 1 ]]; then
    where="이 작업 폴더(저장소 루트)의 ${explore_list}를 탐색하라"
    relbase="이 작업 폴더(저장소 루트)"
  else
    where="이 폴더를 탐색하라"
    relbase="이 폴더"
  fi
  guide="파일을 만들거나 수정하지 말고, 계획서·승인 요청 없이 이번 응답 안에서 답을 끝까지 출력하라. 서브에이전트·브라우저를 쓰지 말고 네가 직접 list_dir·find_by_name·grep_search·view_file 도구로 $where. 명령 실행은 허용되지 않는다. 인용에는 $relbase 기준 상대 경로와 줄 번호를 붙여라."
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
prompt_sent=$prompt

out="$work/.response.jsonl"
rc=0
record=1

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

# 사용량 TSV(state \t frac ...)에서 Argon 남은 비율(%, 소수 1자리) 또는 ?
usage_pct() {
  local st="" fr=""
  IFS=$'\t' read -r st fr _ <<<"$1" || true
  if [[ "$st" == "ok" && -n "$fr" ]]; then
    awk -v f="$fr" 'BEGIN{printf "%.1f", f * 100}'
  else
    printf '?'
  fi
}

# 결과 판정과 출력. 종료 코드: 0 성공, 1 일반 실패, 3 사용량 소진으로 판정, 6 비정상 종료지만 본문 보존
report() {
  python3 -I - "$out" "$work/.stderr" "$rc" "$model" "$1" "$cwd" "$work" "$work/.report.json" <<'PY'
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

# -o 저장용 보조 정보(상태·served·대화 ID·시간·토큰)
try:
    _d = d if isinstance(d, dict) else {}
    json.dump({"status": _d.get("status"), "served": served,
               "conversation_id": _d.get("conversation_id"),
               "seconds": _d.get("duration_seconds"),
               "tokens": (_d.get("usage") or {}).get("total_tokens")},
              open(sys.argv[8], "w", encoding="utf-8"))
except Exception:
    pass

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

# 계정 하나로 호출하고 사용량 전후를 기록한다. 결과 코드는 전역 last_r.
# $1=계정, $2=호출 전 사용량 TSV(비어 있으면 여기서 조회)
last_r=0
do_attempt() {
  local name=$1 utsv=${2-} atsv=""
  if [[ -z "$utsv" ]]; then utsv=$(ga_usage "$name") || utsv=""; fi
  last_account=$name
  last_before=$(usage_pct "$utsv")
  echo "$name" >>"$work/.attempts"
  rm -f "$work/.report.json"
  : >"$work/.result"
  run_agy "$(ga_home_for "$name")"
  last_r=0; report "$name" >"$work/.result" || last_r=$?
  atsv=$(ga_usage "$name") || atsv=""
  last_after=$(usage_pct "$atsv")
  echo "[gemini-argon] account=$name Argon 주간 남은 ${last_before}% → ${last_after}%" >&2
  cat "$work/.result"
}

# -a 지정: 그 계정만 쓰고 다른 계정으로 넘어가지 않는다.
if [[ -n "$account" ]]; then
  do_attempt "$account"
  if [[ $last_r -eq 3 ]]; then
    echo "계정 '$account'의 사용량이 소진된 것으로 보입니다. (-a 지정 시 다른 계정으로 넘어가지 않음)" >&2
    exit 4
  fi
  exit "$last_r"
fi

# 자동: 계정 순서대로 시도
exhausted=""
while IFS= read -r name; do
  if ! ga_exists "$name"; then
    echo "[gemini-argon] account=$name 건너뜀: 프로필 폴더 없음" >&2; continue
  fi
  ustate="" afrac="" areset="" utsv=""
  utsv=$(ga_usage "$name") || utsv=""
  IFS=$'\t' read -r ustate afrac areset _ _ <<<"$utsv" || true
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
      echo "[gemini-argon] account=$name 사용량 조회 실패, 일단 시도합니다." >&2
      utsv="error" ;;
  esac
  do_attempt "$name" "$utsv"
  if [[ $last_r -eq 3 ]]; then
    echo "[gemini-argon] account=$name 소진, 다음 계정으로 같은 요청을 재시도합니다." >&2
    exhausted="$exhausted$name"$'\n'
    continue
  fi
  exit "$last_r"
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
