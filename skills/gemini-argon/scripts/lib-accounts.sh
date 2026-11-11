#!/usr/bin/env bash
# gemini-argon 계정 공통 로직 (source 전용). ask.sh, accounts.sh가 함께 쓴다.
# 계정 모델: main = 실제 $HOME 그대로, 그 외 = $GEMINI_ARGON_HOME/profiles/<이름>/ 을 HOME으로 쓴다.
# 로그인 정보(파일·키체인)는 읽지 않는다. 로그인 여부는 agy models 응답으로만 판단한다.

GA_REAL_HOME=${GA_REAL_HOME:-$HOME}
GA_ROOT=${GEMINI_ARGON_HOME:-$GA_REAL_HOME/.local/share/gemini-argon}
GA_PROFILES="$GA_ROOT/profiles"

# 이름 검증: [A-Za-z0-9_-]+ 만 허용
ga_valid_name() {
  case "$1" in
    ""|*[!A-Za-z0-9_-]*) return 1 ;;
  esac
  return 0
}

# 계정 이름 → agy가 쓸 HOME 경로
ga_home_for() {
  if [[ "$1" == "main" ]]; then
    printf '%s\n' "$GA_REAL_HOME"
  else
    printf '%s\n' "$GA_PROFILES/$1"
  fi
}

# 계정 존재 여부 (main은 항상 존재, 그 외는 프로필 폴더가 있어야 함)
ga_exists() {
  [[ "$1" == "main" ]] && return 0
  [[ -d "$GA_PROFILES/$1" ]]
}

# 계정 순서 출력(한 줄에 하나). GEMINI_ARGON_ACCOUNTS가 있으면 그것, 없으면 main + profiles 정렬순.
ga_account_order() {
  local n IFS_SAVE=$IFS
  if [[ -n "${GEMINI_ARGON_ACCOUNTS:-}" ]]; then
    IFS=,
    for n in $GEMINI_ARGON_ACCOUNTS; do
      IFS=$IFS_SAVE
      n=$(printf '%s' "$n" | tr -d '[:space:]')
      [[ -n "$n" ]] || continue
      if [[ "$n" != "main" ]] && ! ga_valid_name "$n"; then
        echo "GEMINI_ARGON_ACCOUNTS의 계정 이름이 올바르지 않습니다: $n" >&2
        continue
      fi
      printf '%s\n' "$n"
    done
    IFS=$IFS_SAVE
  else
    echo main
    if [[ -d "$GA_PROFILES" ]]; then
      for n in $(LC_ALL=C ls -1 "$GA_PROFILES" 2>/dev/null | LC_ALL=C sort); do
        [[ -d "$GA_PROFILES/$n" ]] && ga_valid_name "$n" && [[ "$n" != "main" ]] && printf '%s\n' "$n"
      done
    fi
  fi
}

# 로그인 여부: 그 HOME으로 `agy models`를 불러 Argon 모델이 보이면 로그인된 것으로 본다.
# 토큰은 파일에서 키체인으로 옮겨질 수 있어 파일 존재로 판단하지 않는다. 사용량은 쓰지 않는다.
ga_has_login() {
  local home
  home=$(ga_home_for "$1")
  [[ -d "$home" ]] || return 1
  GA_LOGIN_HOME=$home python3 -I - <<'PY2'
import os, subprocess, sys
env = dict(os.environ, HOME=os.environ["GA_LOGIN_HOME"])
try:
    p = subprocess.run(["agy", "models"], env=env, capture_output=True, text=True,
                       timeout=45, stdin=subprocess.DEVNULL, cwd="/")
except Exception:
    sys.exit(1)
sys.exit(0 if "gemini-4-argon" in (p.stdout or "") else 1)
PY2
}

# 계정별 /usage 조회. 한 줄 TSV 출력:
#   state \t argon_fraction \t argon_reset \t g5h_fraction \t g5h_reset
# state: ok | nologin | error  (nologin은 토큰 없음/인증 오류, error는 네트워크 등 그 외 실패)
# 로그인 안 된 HOME에서 /usage가 인증 대기로 멈추지 않도록 먼저 agy models로 로그인을 확인한다.
ga_usage() {
  local name=$1 home work
  if ! ga_has_login "$name"; then
    printf 'nologin\t\t\t\t\n'
    return 0
  fi
  home=$(ga_home_for "$name")
  work=$(mktemp -d "${TMPDIR:-/tmp}/gemini-argon-usage.XXXXXX")
  chmod 700 "$work"
  GA_USAGE_HOME=$home GA_USAGE_WORK=$work python3 -I - <<'PY'
import json, os, subprocess, sys, datetime

def loc(t):
    if not t:
        return ""
    try:
        d = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return d.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return t

def emit(state, a=("", ""), g=("", "")):
    print("\t".join([state, str(a[0]), a[1], str(g[0]), g[1]]))

env = dict(os.environ, HOME=os.environ["GA_USAGE_HOME"])
try:
    p = subprocess.run(["agy", "-p", "/usage", "--output-format", "json", "--print-timeout", "30s"],
                       cwd=os.environ["GA_USAGE_WORK"], env=env, capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
except Exception:
    emit("error"); sys.exit(0)
lines = (p.stdout or "").strip().splitlines()
try:
    d = json.loads(lines[-1])
except Exception:
    emit("error"); sys.exit(0)
if d.get("status") != "SUCCESS":
    msg = (d.get("error") or "").lower()
    emit("nologin" if ("auth" in msg or "sign in" in msg or "log in" in msg) else "error")
    sys.exit(0)
a = g = None
try:
    for grp in d["command"]["data"]["groups"]:
        for b in grp.get("buckets", []):
            if b.get("id") == "gemini-argon-wtu-weekly" or (grp.get("name") == "Gemini 4 Argon" and a is None):
                a = (round(b["remaining_fraction"], 4), loc(b.get("reset_time")))
            elif b.get("id") == "gemini-5h":
                g = (round(b["remaining_fraction"], 4), loc(b.get("reset_time")))
except Exception:
    emit("error"); sys.exit(0)
if a is None:
    emit("error"); sys.exit(0)
emit("ok", a, g or ("", ""))
PY
  rm -rf "$work"
}
