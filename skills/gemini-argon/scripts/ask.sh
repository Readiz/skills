#!/usr/bin/env bash
# Antigravity CLI(agy)로 Gemini 4 Argon에 한 번 묻고 응답 본문만 출력한다.
# 입력 파일은 권한 700 임시 작업 폴더에 복사해 그 안에서만 읽게 한다.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
사용법: ask.sh [-e low|medium|high] [-t 초] [-f 파일]... [-k] (-p "프롬프트" | -P 프롬프트파일)
  -e  Argon 추론 단계 (기본 medium)
  -t  응답 제한 시간(초, 기본 600)
  -f  모델이 읽을 파일. 여러 번 지정 가능. 임시 작업 폴더에 같은 이름으로 복사된다.
  -p  프롬프트 문자열
  -P  프롬프트 파일. 긴 지시는 이쪽을 쓴다.
  -k  끝난 뒤 임시 작업 폴더를 지우지 않고 경로를 stderr에 남긴다.
EOF
  exit 2
}

effort=medium timeout=600 prompt="" prompt_file="" keep=0
files=()
while getopts "e:t:f:p:P:kh" opt; do
  case "$opt" in
    e) effort=$OPTARG ;;
    t) timeout=$OPTARG ;;
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
set +e
(cd "$work" && agy -p "$prompt" \
  --model "gemini-4-argon-$effort" \
  --mode plan --sandbox \
  --output-format json \
  --print-timeout "${timeout}s") >"$out" 2>"$work/.stderr"
rc=$?
set -e

python3 - "$out" "$work/.stderr" "$rc" <<'PY'
import json, sys
out, err, rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
raw = open(out, encoding="utf-8", errors="replace").read().strip()
try:
    d = json.loads(raw.splitlines()[-1]) if raw else None
except (json.JSONDecodeError, IndexError):
    d = None
if not isinstance(d, dict):
    sys.stderr.write(f"agy 실패(rc={rc}):\n{raw[-2000:]}\n{open(err, errors='replace').read()[-2000:]}\n")
    sys.exit(1)
u = d.get("usage") or {}
sys.stderr.write(
    f"[gemini-argon] status={d.get('status')} conversation={d.get('conversation_id')} "
    f"seconds={d.get('duration_seconds')} tokens={u.get('total_tokens')}\n"
)
print(d.get("response", ""))
sys.exit(0 if d.get("status") == "SUCCESS" else 1)
PY
