# Claude Code Remote Control 참고

정리 기준: 2026-10-27. Remote Control은 로컬에서 실행 중인 Claude Code 세션에 휴대전화나 웹 브라우저에서 이어서 접근하는 기능입니다. 코드와 파일, MCP 도구 작업은 로컬 컴퓨터에서 실행됩니다.

## 연결 방법

이미 열려 있는 Claude Code 대화에서 `/remote-control` 또는 짧은 명령 `/rc`를 입력해 해당 세션에 원격 연결을 엽니다. 새 대화를 시작하며 바로 연결하려면 터미널에서 다음처럼 실행합니다.

```sh
claude --remote-control
```

대화마다 명령을 입력하는 대신 자동 연결을 켜려면 Claude Code의 `/config`에서 Remote Control 자동 연결을 설정합니다. 사용자 범위 설정을 직접 병합하는 예시는 다음과 같습니다.

```json
{
  "remoteControlAtStartup": true
}
```

사용자 설정 파일은 `~/.claude/settings.json`입니다. 기존 JSON을 통째로 덮어쓰지 말고 속성을 기존 객체에 병합합니다. 프로젝트 설정에서 `remoteControlAtStartup: true`를 지정해도 이 사용자 시작 동작은 켜지지 않습니다. 프로젝트 설정의 `false`는 해당 저장소에서 사용자 자동 연결을 끕니다. 사용자 설정 위치와 Remote Control 동작은 공식 [Remote Control 문서](https://code.claude.com/docs/en/remote-control)에서 확인하세요.

`claude remote-control`은 대화에 연결하는 옵션과 별개로 원격 요청을 받을 서버 프로세스를 시작합니다. 서버는 시작한 디렉터리에서 기본 세션을 열고, 이후 원격 요청으로 생성하는 세션에는 `--spawn worktree`를 지정해 작업별 worktree를 만들 수 있습니다. 이미 진행 중인 대화는 이 서버의 세션으로 옮겨지지 않습니다. 명령 옵션과 서버 동작의 최신 범위는 [Remote Control 문서](https://code.claude.com/docs/en/remote-control)를 확인합니다.

## 모바일에서 이어서 사용하기

같은 계정으로 Claude 모바일 앱에 로그인하고 **Code** 탭을 엽니다. 연결 가능한 세션을 고르거나 터미널이 보여주는 QR 코드 또는 URL로 연결합니다. 모바일 화면에서 보낸 요청도 Claude Code 프로세스가 실행되는 컴퓨터에서 처리됩니다. 따라서 컴퓨터가 켜져 있고 해당 프로세스와 네트워크 연결이 유지되어야 파일, 코드와 MCP 작업을 진행할 수 있습니다. 모바일 기능과 연결 흐름은 공식 [모바일 문서](https://code.claude.com/docs/en/mobile)를 참고하세요.

| 기능 | 주된 목적 | 실행 위치와 범위 |
| --- | --- | --- |
| Remote Control | 모바일이나 웹에서 기존 Claude Code 대화를 이어서 사용 | 세션과 도구 실행은 로컬 컴퓨터에 연결됩니다. |
| Computer use | Claude가 데스크톱 화면을 보고 마우스와 키보드로 GUI 작업 | 허용된 데스크톱 화면과 입력 권한이 필요합니다. 코드 터미널 연결과는 다른 GUI 제어 기능입니다. |
| Cowork Dispatch | 모바일에서 데스크톱 Cowork에 작업을 보내기 | Cowork 세션과 작업 실행 경로를 사용합니다. Claude Code Remote Control의 별칭은 아닙니다. |

각 기능의 지원 상태와 설정은 제품 문서에서 확인합니다. Desktop 앱의 사용 맥락은 [Claude Code Desktop 문서](https://code.claude.com/docs/en/desktop)에 설명되어 있습니다.

## 프로세스와 연결 복귀 확인

절전 복귀 후 네트워크 재연결과 재부팅 후 프로세스 자동 시작은 별개의 동작입니다. 실제로 계속 사용할 환경에서는 다음 절차로 확인합니다.

1. PC에서 Remote Control을 시작하고 표시된 세션이 모바일의 Code 탭에 나타나는지 확인합니다.
2. 휴대전화에서 짧은 입력을 보내고 PC의 같은 세션에서 결과가 생성되는지 확인합니다.
3. 결과가 휴대전화에 다시 표시되는지 확인하고, 연결을 잠시 끊었다가 동일 세션으로 복귀할 수 있는지 살펴봅니다.
4. 화면을 닫거나 절전 상태에서 복귀한 뒤 프로세스가 살아 있는지와 연결이 복구되는지를 따로 확인합니다.
5. 재부팅 후 자동 시작을 원한다면 로그인 뒤 프로세스가 실제로 시작되는지와 모바일 연결이 되는지를 별도 검증합니다. 자동 연결 설정만으로 운영체제 부팅 시 프로세스가 시작된다고 가정하지 않습니다.

검증할 때는 PC의 입력 처리와 모바일 결과 표시, 네트워크 복귀, 재부팅 뒤 프로세스 시작을 나눠 확인합니다.

기존 대화의 파일, 세션 복원, 재시작과 모델 선택을 보존하는 운영은 [Claude 세션 처리 스킬](../skills/claude-session-handling/SKILL.md)을 참고하세요.
