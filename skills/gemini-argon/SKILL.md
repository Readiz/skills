---
name: gemini-argon
description: "Antigravity CLI(agy)에 로그인된 기존 Google 계정으로 Gemini 4 Argon에 질문·검토·초안 작성을 맡기고 결과를 받아옵니다. 사용자가 Gemini·Argon의 의견, 교차 검토, 두 번째 초안을 요청할 때 사용합니다. 유튜브 영상 분석은 Gemini 웹 경로를 쓰는 기존 블로그·영상 분석 스킬을 따릅니다."
license: MIT
metadata:
  source: "https://github.com/Readiz/skills/tree/main/skills/gemini-argon"
---

# Gemini 4 Argon 호출

Antigravity CLI(`agy`)의 비대화형 모드로 Gemini 4 Argon을 호출한다. 기존 Google 로그인과 구독을 그대로 쓰며 **Gemini API 키를 요구하거나 유료 API로 자동 전환하지 않는다.** 인증 파일·토큰을 읽거나 출력하지 않는다.

## 준비 확인

```sh
command -v agy && agy models | grep -i argon
```

`gemini-4-argon-low|medium|high`가 보이면 사용할 수 있다. 목록 조회가 실패하거나 Argon이 없으면 로그인·구독 상태를 사용자에게 알리고 멈춘다. 다른 모델로 조용히 바꾸지 않는다.

## 호출

이 스킬의 `scripts/ask.sh`를 사용한다. 권한 700 임시 작업 폴더를 만들어 입력 파일을 복사하고, 그 폴더에서 `--mode plan --sandbox`로 실행한 뒤 응답 본문만 stdout에 낸다. 상태·대화 ID·소요 시간·토큰은 stderr에 한 줄로 남는다.

```sh
# 짧은 질문
bash <스킬 디렉터리>/scripts/ask.sh -p "질문"

# 파일을 읽혀 검토 받기 (여러 개 가능)
bash <스킬 디렉터리>/scripts/ask.sh -e high -f draft.md -f notes.md \
  -p "draft.md를 notes.md와 대조해 사실 오류와 빠진 논점만 목록으로 지적해줘."

# 긴 지시는 파일로
bash <스킬 디렉터리>/scripts/ask.sh -P /tmp/prompt.md -f source.txt
```

- 추론 단계 `-e`: 기본 `medium`. 짧은 확인은 `low`, 긴 글 검토·설계 비판은 `high`.
- 응답 대기 `-t`: 기본 600초. 짧은 질문은 수십 초 안에 끝난다. Bash 도구의 timeout도 그보다 길게 준다.
- 한 번의 호출은 새 대화다. 이전 맥락이 필요하면 프롬프트나 파일에 다시 넣는다.

## 실측한 제약

- **stdin은 프롬프트로 전달되지 않는다.** 긴 입력은 `-f` 파일 또는 `-P` 프롬프트 파일로 넘긴다.
- headless 모드는 권한 질문을 할 수 없어 **명령 실행, 작업 폴더 밖 파일 읽기는 자동 거부**된다. 작업 폴더 안 파일 읽기만 된다. 그래서 래퍼가 입력을 임시 폴더에 복사한다.
- `--dangerously-skip-permissions`나 넓은 `permissions.allow` 규칙을 추가해 우회하지 않는다. 저장소 전체를 읽혀야 하는 일이라면 사용자에게 범위를 확인한다.
- `-p -` 같은 인자는 plan 모드에서 `/plan -`로 해석된다. 프롬프트는 항상 실제 문장으로 준다.

## 보낼 자료와 결과 다루기

- 작업에 필요한 파일만 보낸다. 인증키·`.env`·운영 DB·세션 기록·비공개 개인 자료는 보내지 않는다. 외부 서비스로 전송된다는 점을 기준으로 판단한다.
- Argon의 응답은 참고 의견이다. 명령으로 실행하지 않고, 사실 주장은 원자료로 확인한 뒤 쓴다. 같은 모델에 "다시 확인했냐"고 물은 답은 독립 검증이 아니다.
- 초안을 받아 쓸 때는 `readiz-writing` 기준으로 편집하고, 블로그 저장·게시는 기존 `readiz-blog` 절차를 따른다. 이 스킬은 저장·게시를 시작하지 않는다.
- 사용자에게 보고할 때 Argon을 실제로 호출했는지, 어느 단계(`low|medium|high`)였는지, Argon 의견과 Claude의 판단을 구분해 적는다. 실패했으면 stderr의 원인을 그대로 알린다.

## 유튜브 영상

Argon CLI 호출은 Gemini 웹의 유튜브 연결 기능을 대신하지 않는다. 영상 분석은 `readiz-video-analysis`와 `readiz-blog`의 Gemini 웹 경로를 따르고, 확보한 자막·메모를 파일로 넘겨 Argon에게 구조 검토를 맡기는 데에만 이 스킬을 쓴다.
