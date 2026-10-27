# Codex 모델과 에이전트 운영

설계와 구현, 조사를 작업 성격에 맞게 나누되 사용자가 선택한 메인 모델과 추론 강도(Effort)를 유지합니다. Sol이 메인일 때는 아래 역할 분담을 권장하고, Astra가 메인일 때는 직접 처리와 위임, 병렬화와 검증 범위를 Astra가 판단하도록 둡니다.

정리 기준: 2026-10-27. 모델명은 이 운영안의 선택 예시입니다. 적용할 때 계정과 클라이언트에서 각 모델과 Effort를 사용할 수 있는지 확인합니다.

## 역할 분담

| 역할 | 모델 예시 | Effort | 맡길 일 |
| --- | --- | --- | --- |
| 메인 Sol | `gpt-6.1-sol` | 현재 채팅 선택 | 요구사항, 설계, 작업 분해, 통합과 최종 검증 |
| 메인 Astra | `gpt-6-astra` | 현재 채팅 선택 | 작업에 맞춰 직접 수행과 에이전트 활용을 자율 결정 |
| `browser_researcher` | `gpt-6.1-terra` | `medium` | 실제 브라우저 조사, UI 재현, 복잡한 외부 자료 확인과 근거 수집 |
| `implementer` | `gpt-6-luna` | `medium` | 범위가 명확한 구현, 반복 수정과 테스트 |
| `reviewer` | `gpt-6.1-sol` | `xhigh` | 위험도가 높은 변경의 독립 검토 |

Sol은 브라우저 동작이 중요한 작업에서 Terra의 조사를 먼저 받고, 그 결과로 구현 범위를 정해 Luna에 맡기는 흐름을 우선 고려합니다. 결과는 메인 Sol이 검토합니다. 보안이나 동시성, 여러 구성요소에 걸친 변경처럼 별도 검토가 필요한 경우에만 reviewer를 추가합니다. 작은 수정은 직접 처리해도 됩니다.

Astra에는 같은 역할을 선택지로 제공합니다. 특정 하위 모델을 강제하거나 에이전트 사용을 금지하지 않고 작업에 가장 적합한 방식을 고르게 합니다. 다른 메인 모델을 선택한 경우에도 그 선택을 유지합니다.

## 설정 위치와 메인 모델 선택

기본 Codex 홈을 사용하는 환경의 예시입니다. `CODEX_HOME`을 별도로 지정한 환경에서는 실제 위치를 확인합니다.

```text
~/.codex/
├── config.toml
├── AGENTS.md
└── agents/
    ├── browser_researcher.toml
    ├── implementer.toml
    └── reviewer.toml
```

`config.toml`에는 새 대화에 사용할 기본값을 둡니다. 다음은 Sol을 기본 모델로 선택한 경우의 병합 예시입니다.

```toml
model = "gpt-6.1-sol"

[agents]
enabled = true
max_concurrent_threads_per_session = 3
```

현재 채팅에서 사용자가 선택한 모델과 Effort를 존중합니다. 새 대화의 기본 Effort도 지정하고 싶으면 `model_reasoning_effort = "xhigh"`를 최상위에 추가할 수 있습니다. 위 예시처럼 생략하면 해당 환경의 기본값을 사용합니다. 실제 적용값은 채팅의 모델 선택 UI나 CLI `/model`, `/status`에서 확인합니다.

사용자 설정 위에는 프로젝트 설정, 실행 옵션과 관리 정책 등이 적용될 수 있습니다. 위 조각으로 파일 전체를 교체하지 말고 기존 표와 키를 확인해 병합합니다. 구성 계층은 [공식 Config basics](https://learn.chatgpt.com/docs/config-file/config-basic)를 참고합니다.

하위 역할의 모델은 각각의 파일에 지정합니다. 이 운영안에서는 모든 하위 에이전트를 하나의 모델로 묶는 `default_subagent_model`을 새로 설정하지 않습니다. 기존 기본값이 있으면 의도를 먼저 확인합니다.

## 역할별 에이전트 예시

현재 공식 형식은 사용자 범위의 `~/.codex/agents/` 또는 프로젝트 범위의 `.codex/agents/`에 역할별 TOML을 두는 방식입니다. `name`, `description`, `developer_instructions`가 필요합니다. 파일에 `model`과 `model_reasoning_effort`를 함께 명시하면 역할별 선택을 분명히 할 수 있습니다. [공식 Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

`~/.codex/agents/browser_researcher.toml`

```toml
name = "browser_researcher"
description = "실제 브라우저 조사, UI 재현, 복잡한 외부 자료 조사와 근거 수집에 사용합니다. 브라우저 동작이 중요하면 구현 전에 우선 고려합니다."
model = "gpt-6.1-terra"
model_reasoning_effort = "medium"
developer_instructions = """
부모가 지정한 질문을 연결된 검색 및 브라우저 도구로 조사합니다.
실제 관찰과 추론을 구분하고 출처 URL, 재현 절차와 확인한 근거를 보고합니다.
상호작용 도구가 없으면 가능한 조사 범위와 필요한 도구를 알립니다.
저장소 파일을 수정하지 않고 관찰 결과와 다음 조치를 부모에게 전달합니다.
"""
```

`~/.codex/agents/implementer.toml`

```toml
name = "implementer"
description = "부모가 범위를 정한 일반 기능 구현, 작은 수정, 리팩터링과 테스트에 사용합니다."
model = "gpt-6-luna"
model_reasoning_effort = "medium"
developer_instructions = """
부모가 전달한 범위와 수용 조건에 맞춰 구현합니다.
저장소 규칙을 따르고 무관한 리팩터링과 다른 에이전트의 파일 수정을 피합니다.
변경에 맞는 검사를 수행하고 수정 파일, 결과와 남은 문제를 보고합니다.
설계가 모호하거나 실패가 반복되면 근거와 함께 부모에게 돌려보냅니다.
"""
```

`~/.codex/agents/reviewer.toml`

```toml
name = "reviewer"
description = "보안, 설계 또는 회귀 위험이 큰 변경을 독립적으로 검토할 때 사용합니다."
model = "gpt-6.1-sol"
model_reasoning_effort = "xhigh"
developer_instructions = """
파일을 수정하지 않고 변경과 관련 실행 경로를 검토합니다.
정확성, 회귀, 보안, 동시성과 필요한 검증의 누락을 확인합니다.
실제 코드와 검사 근거를 대조하고 구체적인 파일 위치와 심각도를 보고합니다.
발견 사항과 확인하지 못한 범위를 구분해 부모에게 전달합니다.
"""
```

## 공통 AGENTS.md 문안

다음 문안은 사용자 공통 `AGENTS.md`에 병합할 수 있는 예시입니다. 역할 파일은 에이전트를 정의하고, 공통 지침은 어떤 상황에 위임할지 설명합니다. 공식 문서는 직접 위임 요청이나 적용되는 `AGENTS.md` 또는 스킬의 지시에 따라 Codex가 하위 에이전트를 실행한다고 설명합니다. [공식 Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

```markdown
## 에이전트 활용 지침

사용자가 Codex UI에서 선택한 메인 모델과 추론 강도를 존중한다.
아래 역할 분담은 작업을 돕는 권장안이다.

사용 가능한 전문 역할:

- browser_researcher: Terra medium. 브라우저와 UI 조사, 고급 외부 조사와 근거 수집.
- implementer: Luna medium. 범위가 명확한 일반 구현, 수정과 테스트.
- reviewer: Sol xhigh. 위험도가 높은 변경의 독립 검토.

메인이 Sol일 때:

- Sol이 요구사항, 설계, 작업 조정과 최종 검증을 담당한다.
- 복잡한 브라우저 조사에는 Terra, 명확한 구현에는 Luna를 우선 고려한다.
- 브라우저 조사와 구현이 연결되면 조사 결과로 수정 범위를 정한 뒤 구현을 위임한다.
- 별도 Sol reviewer는 변경 위험이 정당화할 때 사용한다.
- 작은 작업이나 위임 비용이 더 큰 작업은 직접 처리한다.

메인이 Astra일 때:

- 위 역할은 선택지로 참고한다.
- 직접 처리, 위임, 병렬 작업과 검증 범위는 Astra가 작업에 맞게 판단한다.
- 선택한 Astra 모델과 Effort를 유지한다.

다른 모델이 메인일 때:

- 현재 선택 모델을 유지하고 작업에 맞게 위임 여부를 판단한다.

공통:

- 독립된 작업만 병렬화하고 같은 파일의 동시 수정을 조정한다.
- 실제 사용 가능한 모델과 도구를 확인하고 사용할 수 없는 역할은 알린다.
- 실제 호출한 에이전트와 모델, 수행한 검사만 보고한다.
```

`AGENTS.override.md`가 있으면 같은 범위의 `AGENTS.md` 대신 사용될 수 있습니다. 실제 읽히는 파일을 확인하고 프로젝트의 지침을 보존합니다. [공식 AGENTS.md 안내](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

모델 감지 Hook, `ROUTING_MODE`, `spawn_agent` 차단과 Astra 전용 강제 프로필은 이 운영안에서 사용하지 않습니다. 메인 모델을 확인할 수 없는 환경에서는 추측으로 분기하지 않고 작업에 맞는 권장 지침으로 활용합니다.

## 도구와 권한

검색 결과 확인과 웹사이트의 클릭, 스크린샷 및 동작 재현을 구분합니다. 역할에 브라우저 조사를 적었어도 실제로 부모 환경에 연결된 도구가 있어야 수행할 수 있습니다. 문서 검색만 했다면 그 범위로 보고합니다.

Codex 하위 에이전트는 부모의 권한과 MCP 연결을 따릅니다. 역할 지침에 파일을 수정하지 말라고 적는 것은 행동 지침입니다. 실제 샌드박스와 승인 정책은 부모 세션에서 설정합니다. [공식 Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

## 적용과 검증

1. 설치된 Codex 버전, 실제 설정 계층, 모델 목록과 연결 도구를 확인합니다. 기존 설정과 대화 상태를 보존합니다.
2. 필요한 역할 파일과 공통 지침만 병합하고 TOML 문법과 중복 키를 검사합니다.
3. 새 시험 세션에서 현재 모델과 Effort, 인식된 역할을 확인합니다. 실행 중인 작업은 강제 종료하지 않습니다.
4. 명확한 조사, 구현 또는 검토 작업을 하나씩 맡겨 실제 하위 실행의 모델과 결과를 확인합니다. CLI에서는 `/subagents`로 활동을 확인할 수 있습니다. [공식 Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
5. 메인 UI의 Effort와 역할 파일에 지정한 Effort를 따로 확인하고, Astra를 선택했을 때 직접 처리와 위임 선택권이 유지되는지 확인합니다.
6. 파일 저장, 역할 인식, 실제 위임, 검사 결과를 나누어 보고합니다. Claude Code의 설정은 별도로 관리합니다.

예를 들어 로그인 화면 오류를 조사한다면, 연결된 브라우저로 실제 증상을 수집하고 그 결과에 따라 수정 범위를 정합니다. 구현과 검증 결과를 대조한 뒤 어떤 역할을 실제 사용했는지 보고합니다. 병렬화와 역할 분리는 추가 모델 요청을 수반하므로 작업 품질과 사용량을 함께 살펴봅니다.
