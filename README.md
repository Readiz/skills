# Readiz Skills

Readiz 프로젝트에서 사용하는 에이전트 스킬을 공개하고 관리합니다. 실제 작업에서 확인한 개선점을 작은 변경으로 반영합니다.

| 스킬 | 용도 |
| --- | --- |
| [readiz-web-ui](skills/readiz-web-ui/SKILL.md) | 작은 버튼·아이콘·표와 접힌 설명으로 콘텐츠 중심의 웹 UI를 설계하고 검증합니다. |
| [readiz-writing](skills/readiz-writing/SKILL.md) | 한국어 글의 문체·설명의 깊이·논증·퇴고를 관리합니다. 사이트 저장·게시와 독립적으로 사용합니다. |

`readiz-web-ui`는 Readiz가 선호하는 디자인 기준입니다. 버튼 크기와 색상 등은 기본값이며, 사용자가 지정한 브랜드·플랫폼·접근성 요구를 먼저 따릅니다.

`readiz-writing`은 Readiz의 원작성 글에서 이어 온 글쓰기 기준입니다. 일반 문체와 설명 원칙은 본문에, 해설·분석의 근거 검증과 투자 글의 판단 기준은 조건부 참고 문서에 둡니다. 사이트 운영은 기존 `readiz-blog`가 담당하며, 글쓰기 스킬을 사용할 때 블로그 저장이나 위키 게시를 자동으로 시작하지 않습니다.

## 설치와 사용

Codex에서 다음과 같이 요청할 수 있습니다.

```text
$skill-installer로 https://github.com/Readiz/skills/tree/main/skills/readiz-web-ui 를 설치해줘.
$skill-installer로 https://github.com/Readiz/skills/tree/main/skills/readiz-writing 를 설치해줘.
```

필요한 스킬을 선택해 설치합니다. 설치 후 다음 요청부터 스킬 이름으로 명시적으로 사용할 수 있으며, 각 스킬의 설명에 맞는 작업에도 자동으로 선택될 수 있습니다.

```text
$readiz-web-ui를 사용해 이 대시보드의 버튼과 필터를 컴팩트하게 정리해줘.
$readiz-writing을 사용해 이 글의 문체와 논리 흐름을 다듬어줘.
```

설치 도구는 같은 이름의 기존 폴더를 덮어쓰지 않습니다. 이미 설치했다면 변경 내용을 비교하고 보존한 뒤 갱신합니다.

## 원본과 로컬 설치 관리

`skills/<스킬 이름>/`이 관리 원본입니다. 지속적으로 수정하는 환경에서는 로컬 checkout을 스킬 검색 경로에 심볼릭 링크로 연결하면 별도 복사본을 동기화할 필요가 없습니다.

새로 연결하는 macOS/Linux 환경의 예시입니다.

```sh
git clone https://github.com/Readiz/skills.git readiz-skills
cd readiz-skills
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd)/skills/readiz-web-ui" "$HOME/.agents/skills/readiz-web-ui"
ln -s "$(pwd)/skills/readiz-writing" "$HOME/.agents/skills/readiz-writing"
```

같은 이름의 폴더가 이미 있으면 먼저 원본과 비교하고, 기존 폴더를 스킬 검색 경로 밖에 보관한 뒤 연결합니다. 기존 환경이 `~/.codex/skills` 또는 별도로 설정한 경로에서 스킬을 읽고 있다면 그 경로를 유지합니다. 여러 검색 경로에 같은 스킬을 중복 설치하지 않습니다.

Codex의 스킬 검색 경로와 심볼릭 링크 지원은 [공식 문서](https://learn.chatgpt.com/docs/build-skills)를 참고하세요.

수정은 checkout에서 수행하고 차이를 검토한 뒤 커밋·push합니다. 다른 환경에서 갱신할 때는 로컬 변경을 먼저 확인하고 `git pull --ff-only`로 가져옵니다. 링크된 설치는 같은 파일을 읽으므로 변경이 함께 반영됩니다. GitHub에는 push한 변경만 공개됩니다.

## 유지보수

- 각 스킬의 사용자 의도와 적용 범위를 보존합니다. UI는 사용자 지정·접근성 요구를, 글쓰기는 독자·분량·어조·장르를 우선합니다.
- 새 스킬은 공개할 내용을 개별 검토한 뒤 추가합니다. 인증정보, 개인 데이터, 내부 서비스 주소, 기기별 절대 경로는 포함하지 않습니다.
- 스킬 수정 시 YAML frontmatter와 `agents/openai.yaml`을 확인합니다. Codex의 `skill-creator`가 설치되어 있다면 해당 스킬의 `scripts/quick_validate.py`에 스킬 폴더 경로를 넘겨 검사합니다.
- UI 판단 기준의 변경은 관련된 실제 화면에서 검증합니다. 글쓰기 기준의 변경은 해당 장르의 글과 요청 범위로 확인합니다. 문서 구조 검사와 실제 결과물의 품질 검증은 구분합니다.

## 라이선스

[MIT](LICENSE) — 재사용·수정·배포 시 라이선스의 저작권 및 허가 고지를 유지합니다.
