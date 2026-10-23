[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

# claude-code-statusline · DeepSeek 가격 가드 & 상태 표시줄

**Claude Code + DeepSeek** 사용자를 위한 가드 도구 모음입니다: DeepSeek 피크 가격 시간대의 하드 차단, 잔액·사용량 상태 표시줄, 그리고 Windows에서 상태 표시줄을 **빠르고 안정적으로** 만드는 엔지니어링 노트를 담았습니다.

> 비공식 플러그인이며 DeepSeek과 무관합니다. 잔액 조회에는 공개 엔드포인트 `GET https://api.deepseek.com/user/balance`만 사용합니다.

![상태 표시줄 미리보기](plugins/claude-code-statusline/demo-shot.png)

(위 이미지는 `scripts/demo-statusline.py`가 **실제 스크립트 + 가짜 stdin**으로 렌더링한 것이며, 두 가지 팔레트를 비교한 것입니다)

---

## 설치

```bash
# Claude Code 안에서
/plugin marketplace add HongtaoWang75/claude-code-statusline
/plugin install claude-code-statusline@claude-code-statusline
```

사전 조건:

- Python 3.8+ (`python`이 PATH에 있어야 합니다. `python3`만 있는 시스템은 `hooks/hooks.json`의 `python`을 `python3`으로 바꾸세요)
- 상태 표시줄은 `~/.claude/settings.json`에 설정해야 합니다(플러그인이 settings를 자동으로 바꾸지 않습니다):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"C:/Users/<당신>/.claude/plugins/marketplaces/claude-code-statusline/plugins/claude-code-statusline/scripts/deepseek-status.py\"",
    "refreshInterval": 5
  }
}
```

(`refreshInterval`의 최솟값은 1초입니다. **`0.5`를 쓰면 클램프되지 않고 상태 표시줄 갱신이 완전히 멈춥니다**.)

- 잔액 API에는 API 키가 필요합니다: 환경 변수 `DEEPSEEK_API_KEY`, 또는 `~/.claude/settings.json`의 `env.ANTHROPIC_AUTH_TOKEN`(공식 DeepSeek 엔드포인트를 쓰면 보통 이미 들어 있습니다).

---

## 기능

### 1. 피크 가격 하드 게이트 (PreToolUse 훅)

DeepSeek은 평일 **09:00–12:00 / 14:00–18:00**(베이징 시간, 중국 법정 공휴일 제외)에 가격이 2배가 됩니다. 이 시간대에 이 플러그인은 **모든 도구 호출**을 차단합니다:

- WPF 대화상자(시스템 라이트/다크 테마 추종)로 확인합니다: 메시지 아래 드롭다운에서 허용 시간을 고르고(기본 **이번 한 번만 허용**, 그 외 1 / 3 / 5 / 10 / 20 / 30 / 60분, **이번 피크 시간대 허용**), "예"를 누릅니다. "아니오" 또는 30초 무응답이면 거부. 아이콘은 동봉된 `assets/claude-light.ico`(검정, 라이트용) / `claude-dark.ico`(흰색, 다크용)입니다.
- 거부 메시지는 **모델을 위한 것**입니다: 현재 시각과 그대로 붙여넣을 수 있는 cron 표현식이 들어 있어, 모델이 `CronCreate`로 남은 작업을 다음 유휴 시간대로 예약하고 깔끔하게 마무리합니다.
- 스케줄링 도구(`CronCreate` / `CronList` / `CronDelete` / `ScheduleWakeup`)는 **항상 허용**됩니다. 그렇지 않으면 모델이 예약 자체를 할 수 없습니다.
- **예약 작업이 시작한 턴은 턴 전체가 면제**됩니다: 훅이 transcript에서 식별합니다(`turnOrigin: "scheduled"` / `scheduledTaskId`, `promptId`가 훅의 `prompt_id`와 일치). 비활성화하려면 `"exempt_scheduled": false`.
- 중국 법정 공휴일은 [timor.tech](https://timor.tech/api/holiday)에서 자동으로 가져옵니다(`scripts/fetch-holidays.py`, 주간 예약 작업에 등록). 실패하면 내장 2026년 표를 사용합니다.
- 세션 시작 시 `DSPG_BYPASS=1`을 설정하면 가드를 완전히 우회합니다.
- 진단: 모든 판정이 `~/.claude/.dspg_hook.log`에 기록됩니다. 최근 훅 원본 페이로드는 `~/.claude/.dspg_hook_payload.json`(기록은 `.dspg_hook_payloads.jsonl`, `tool_input` 포함 — 개인정보 주의).

### 2. 상태 표시줄

```
deepseek-flash[1m] · ¥22.39 · valley 2:08:03:09 · cache 99.6% · 310k toks×8 turns · 451tok/s · cost ¥0.42
```

| 세그먼트 | 의미 |
|---|---|
| `deepseek-flash[1m]` | 현재 모델명 (Opus/Sonnet 등 고가 구간은 빨간색 + `◆`) |
| `¥22.39` | 계정 잔액 (**렌더 경로는 절대 네트워크를 타지 않습니다** — 아래 참조) |
| `valley 2:08:03:09` / `peak 01:00:00` | 피크/유휴 카운트다운, **일:시:분:초** (24시간 이상일 때만 일 표시). `valley` = 유휴(초록), `peak` = 피크(주황). 중국어 UI에서는 梁文谷 / 梁文峰. **피크 가격이 있는 provider(현재 DeepSeek만)에서만 표시** |
| `cache 99.6%` | 세션 전체 캐시 적중률 |
| `310k toks×8 turns` | 현재 컨텍스트 × 이 구간의 API 호출 수 (`/compact`가 호출 수를 초기화) |
| `451tok/s` | 평균 출력 속도 (API 소요 시간 기준, 도구 실행·대기 시간 제외) |
| `cost ¥0.42` | 가격표로 추정한 세션 비용 (현재 모델 가격을 알 때만 표시) |

**색상**: 값은 경고 구간에 들어갈 때만 색이 입혀지고 평소에는 무색이라 이상이 한눈에 보입니다.

| 지표 | 무색 | 노랑 | 그라데이션 | 빨강 |
|---|---|---|---|---|
| 잔액 | > ¥1 | ¥1.00 | ¥1 → ¥0 | ¥0 |
| 캐시 적중 | ≥ 95% | < 95% | 95% → 90% | ≤ 90% |
| 출력 속도 | ≥ 150 tok/s | < 150 | 150 → 100 | ≤ 100 tok/s |
| 컨텍스트 | — | 50% | 0 초록 → 100% 빨강 | 자동 압축 임계값 |

- 컨텍스트 그라데이션의 분모는 Claude Code의 `autoCompactWindow`(`~/.claude/settings.json`)에서 읽으므로 "자동 압축까지 얼마나 남았는지"를 색으로 보여줍니다. 대체 순서: `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, 그다음 1M.
- **점멸**: 지표가 **막 경고 구간에 들어갈 때** 해당 세그먼트가 반전 표시로 3번 점멸합니다(경고색 배경 + 흰 글자). 이후에는 조용히 색만 유지됩니다. 판정은 **표시값**(잔액은 센트, 캐시는 0.1%, 속도는 정수) 기준이라, 표시가 변하지 않았는데 다시 점멸하는 일은 없습니다.
- **모델 테마 색상**: 모델명을 각 브랜드의 **주요 색상 단색**으로 표시합니다. `.dspg_config.json`의 `"model_theme": false`로 끌 수 있습니다(settings 명령에서도 묻습니다).
- 24비트 트루컬러, 미지원 터미널은 256색으로 폴백. `DSPG_TRUECOLOR=0/1`로 강제 가능.
- `/balance` 명령: 잔액·요금대·남은 시간·적중률을 모델이 보고하게 합니다.

### 3. 주요 프로바이더 지원

사용 중인 프로바이더에 맞춰 상태 표시줄·잔액·가드가 자동으로 전환됩니다:

| 프로바이더 | 식별 | 잔액 | 피크 가격 가드 | 비용 추정 |
|---|---|---|---|---|
| **DeepSeek** | `deepseek*` | `/user/balance` (CNY) | ✅ | ✅ 내장 가격표 (피크/오프피크) |
| **Moonshot / Kimi** | `kimi*`, `moonshot*` | `/v1/users/me/balance` (CNY) | — | 가격표에 추가 |
| **Anthropic Claude** | `claude*`, `opus/sonnet/haiku` | Admin key 필요 → n/a | — | 가격표에 추가 |
| **OpenAI** | `gpt*`, `o1/o3/o4` | Admin key 필요 → n/a | — | 가격표에 추가 |
| **Google Gemini** | `gemini*`, `gemma*` | n/a | — | 가격표에 추가 |
| **GLM(智谱)** | `glm*` | n/a | — | 가격표에 추가 |
| **Xiaomi MiMo** | `mimo*`, `xiaomi*` | n/a | — | 가격표에 추가 |

- 식별 순서: **BASE_URL 우선**, 매칭되지 않으면 모델명 → transcript의 마지막 assistant 모델. `~/.claude/.dspg_config.json`의 `"provider"`로 강제 지정할 수 있습니다(기본 `auto`).
- **피크 가격 차단은 DeepSeek에만 적용**됩니다. Kimi / Claude / Gemini / GLM 등에서는 차단도 카운트다운 표시도 하지 않습니다.
- 잔액은 프로바이더 통화로 표시합니다. 공개 잔액 API가 없으면 `bal N/A`로 표시되고 나머지 세그먼트는 정상 동작합니다.

**가격표**: 내장은 DeepSeek만 포함합니다(2026-10-22 공식 가격 페이지에서 수집, CNY / 100만 tokens, 피크 = 오프피크의 2배). 그 외는 `~/.claude/.dspg_prices.json`에 추가합니다:

```json
{
  "moonshot": {
    "kimi-k3": {"input": 4.0, "cache_read": 1.0, "output": 16.0, "currency": "CNY"},
    "default": "kimi-k3"
  },
  "anthropic": {
    "claude-opus-5-5": {"input": 15.0, "cache_read": 1.5, "output": 75.0, "currency": "USD"}
  }
}
```

단위는 100만 tokens, 통화는 프로바이더를 따릅니다. 선택적 `peak_multiplier`는 피크 시간대에 적용됩니다. 비용은 세션 누적 input / cache_read / output에 **현재 시간대** 단가를 곱해 추정합니다.

### 4. 백그라운드 잔액 갱신 (렌더 경로 네트워크 없음)

상태 표시줄은 매초(또는 몇 초마다) 한 프레임을 그립니다. 렌더 경로에서 네트워크 요청을 하면 통신이 흔들리는 순간 프레임이 제때 끝나지 못합니다. Claude Code는 이를 **취소하고 `taskkill`로 프로세스 트리를 정리**합니다. 몇 번 반복되면 좀비 프로세스 눈사태가 됩니다(아래 엔지니어링 노트 참조).

그래서: **렌더 경로는 로컬 캐시만 읽습니다**(`~/.claude/.balance_cache.json`, 30초 유효). 만료되면 **분리된** 백그라운드 프로세스가 갱신합니다. 따라서 렌더 비용은 일정합니다(실측 180–225 ms → shim 경유 131 ms).

### 5. 선택 사항: 상주 데몬 + 네이티브 shim (Windows에서 3배 빠름)

Claude Code는 상태 표시줄을 갱신할 때마다 `Git Bash(103 ms) → python(74 ms) → 스크립트` 체인을 통째로 띄우고, 각각 콘솔 호스트 `conhost`가 붙습니다.

이 플러그인에는 선택 사항인 **C shim + Python 상주 데몬**이 포함되어 있습니다:

```
CC → bash (CC가 강제, 생략 불가) → dspg-status.exe (~15 ms) → 127.0.0.1:18799의 데몬 (프로세스 내 렌더링)
```

| | 예전 경로 (bash+python) | shim + 데몬 |
|---|---|---|
| 프레임당 | 421 ms | **131 ms** |
| 프레임당 프로세스 수 | bash + python + 2×conhost | 네이티브 exe 1개 |

3단계 폴백:

1. 데몬이 없으면 shim이 데몬이 마지막으로 쓴 줄(`~/.claude/.dspg_line.txt`)을 출력합니다 — **빈 화면이 아닙니다**.
2. shim이 데몬을 띄웁니다(`pythonw`, 창 없음). 다음 프레임에서 복구됩니다.
3. 데몬 내장 워치독(10초마다): **멈춘 `taskkill`이 60개를 넘으면 자동 회수**(그들이 독점한 `conhost`도 함께 종료). 이는 Claude Code의 "취소 시 `taskkill /T`로 프로세스 트리 정리" 부작용을 자동 억제하는 것입니다. 회수 후에도 연속 두 번 100개를 넘을 때만 `refreshInterval`을 5초로 되돌리는 비상 브레이크가 작동합니다.

설정 방법은 [`scripts/dspg-status.c`](plugins/claude-code-statusline/scripts/dspg-status.c) 상단 주석을 참고하세요. `dspg.env`(exe와 같은 디렉터리, gitignore 처리됨)로 `PYTHONW=<절대 경로>`를 지정할 수 있습니다.

---

## 설정

Claude Code 내장 `/config`는 플러그인이 항목을 추가하는 것을 허용하지 않으므로 전용 명령을 사용합니다:

```
/claude-code-statusline:settings
```

네 가지를 묻고 해당 파일에 기록합니다:

| 설정 | 저장 위치 | 설명 |
|---|---|---|
| 갱신 주기(초) | `~/.claude/settings.json` → `statusLine.refreshInterval` | 1 / 2 / 5 / 15초, **최소 1초** |
| 언어 | `~/.claude/.dspg_config.json` → `language` | `auto`(시스템 표시 언어 따름) / `en` / `zh`. UI 문구는 현재 영어·중국어를 지원하며 그 외 언어는 영어로 폴백 |
| 모델 테마 색상 | `~/.claude/.dspg_config.json` → `model_theme` | `true`(기본): 브랜드 색 / `false`: 단색 |
| provider 강제 지정 | `~/.claude/.dspg_config.json` → `provider` | `auto`(기본) 또는 provider 키(`deepseek`, `moonshot`, `anthropic`, `openai`, `gemini`, `glm`, `xiaomi`) |
| 추가 작업 | `~/.claude/.dspg_override.json` | 30분 임시 허용 / 현재 허용 취소 |

## 상태 표시줄 조정 항목

| 바꾸고 싶은 것 | 바꿀 위치 |
|---|---|
| 경고 임계값 | `scripts/deepseek-status.py` 상단 `BALANCE_YELLOW_AT` / `CACHE_WARN_PCT` / `RATE_WARN_TPS` / `CTX_ALERT_RATIO` |
| 팔레트 | 상단 `PALETTES`(`vivid` 고채도 / `soft` 어두운 터미널용), 또는 환경 변수 `DSPG_PALETTE=soft` |
| 점멸 횟수 | `BLINK_FRAMES`(기본 6프레임 = 3회) |
| 잔액 캐시 유효 시간 | `BALANCE_TTL`(기본 30초) |

---

## 엔지니어링 노트: Windows에서 Claude Code 상태 표시줄의 함정

모두 실측으로 부딪힌 것들입니다. 뒤에 오는 사람을 위해:

1. **CC는 항상 Git Bash를 거쳐 상태 표시줄 명령을 실행합니다**(현장에서 잡은 프로세스 트리: `bash.exe -c "\"python.exe\" -X utf8 \"script.py\""`). `bash -c true` 하나만으로도 **103 ms**가 걸립니다. MSYS2 POSIX 에뮬레이션 계층 비용입니다(Linux에서는 2 ms). .NET/Go로 바꿔도 이 계층은 사라지지 않습니다. "매 프레임 프로세스를 띄우지 않기"만이 해결책입니다.
2. **상태 표시줄 출력은 프로세스가 종료된 뒤에야 표시됩니다**. 게다가 **새 갱신이 진행 중인 것을 취소합니다**. 따라서 상주 프로세스가 직접 print하는 것은 무의미하고(내용이 영원히 표시되지 않음), **렌더링이 느림 = 취소 = `taskkill`**입니다.
3. **1초 갱신의 눈사태**: 분당 수십 번의 프로세스 생성. 컴파일·백신 검사·메모리 압박 등으로 조금만 느려져도 한 프레임이 주기를 넘기고 → 취소 → `taskkill`로 트리 정리 → 멈춘 `taskkill`이 쌓임(실측 300개 이상, 7.5 GB) → 메모리 고갈 → 더 느려짐. **근본 해결은 네트워크 호출과 모든 느린 작업을 렌더 경로 밖으로 옮기는 것**이며, 갱신 주기를 낮추는 것이 아닙니다.
4. `refreshInterval`의 단위는 초이고 **최소 1**입니다. `0.5`는 클램프되지 않고 **상태 표시줄이 완전히 멈춥니다**(실측: 14초 동안 0프레임).
5. **Windows의 `SO_REUSEADDR`는 같은 포트의 중복 바인딩을 허용합니다**(Linux와 반대). "포트가 사용 중이면 종료"하는 단일 인스턴스 보호에 쓰면 실패하고 데몬이 늘어납니다. `SO_EXCLUSIVEADDRUSE`를 쓰세요.
6. **`-mwindows`(GUI 서브시스템)에서는 CRT의 stdout이 상속된 파이프에 연결되지 않습니다**. `printf`는 조용히 출력을 버립니다. 표준 핸들에 `ReadFile`/`WriteFile`을 쓰거나, 콘솔 서브시스템을 유지하세요(shim은 bash의 기존 콘솔에 붙어 **conhost를 추가하지 않습니다**).
7. transcript 증분 집계에서 한 번의 API 응답은 여러 assistant 레코드로 쪼개지고(content block마다 1개), **각각 동일한 전체 usage를 담고 있습니다**. 단순 합산하면 output이 3~5배로 부풀려집니다(실측 115,703 대 실제 37,302). 레코드는 엄격히 인접하므로 구간 머리만 취합니다.
8. **PreToolUse의 "확인"은 `hookSpecificOutput.permissionDecision`을 써야 합니다**(최상위 `decision`/`reason`은 폐기됨). `"ask"`는 CC 자체 권한 대화상자를 띄우고 `permissionDecisionReason`을 그 안에 표시합니다. JSON이 해석되려면 훅이 **exit 0**으로 끝나야 합니다(exit 2는 즉시 차단이며 JSON은 전혀 무효).
9. **PreToolUse 훅은 타임아웃되어도 기본적으로 차단하지 않습니다**. 훅을 게이트로 쓰려면(이 가격 가드처럼) `hooks.json`에 `"onFailure": "block"`을 써야 합니다. 그렇지 않으면 훅이 멈추거나 타임아웃될 때 도구 호출이 그대로 통과합니다.
10. **훅 페이로드에는 "턴의 출처" 필드가 없습니다**: 예약 작업이 시작한 턴인지 판단하려면 `transcript_path`의 jsonl을 다시 읽어야 합니다. 주입된 user 메시지에는 `turnOrigin: "scheduled"` / `scheduledTaskId` / `promptSource: "system"`이 붙고, 그 `promptId`는 훅 페이로드의 `prompt_id`와 같습니다. 함정 두 가지: 같은 `promptId`가 **모든 도구 결과** user 레코드에도 붙어 있고( `turnOrigin` 없음, 역순 스캔에서 먼저 걸림 → `toolUseResult`가 있는 레코드는 건너뛰기), 긴 턴에서는 트리거 레코드가 끝 윈도우 밖으로 밀려납니다 → 판정 결과를 `prompt_id`별로 `.dspg_turn_cache.json`에 캐시합니다. 파일 끝 4 MB만 읽고, 찾지 못하면 일반 턴으로 처리합니다(페일 클로즈).

---

## 파일 구조

```
.claude-plugin/marketplace.json          # marketplace 매니페스트
plugins/claude-code-statusline/
├── .claude-plugin/plugin.json
├── commands/balance.md                  # /balance 명령
├── commands/settings.md                 # /claude-code-statusline:settings 명령
├── hooks/                               # PreToolUse 피크 게이트
├── scripts/
│   ├── deepseek-status.py               # 상태 표시줄 (색상/점멸/카운트다운/비동기 잔액)
│   ├── providers.py                     # provider 식별 / 잔액 API / 가격표
│   ├── i18n.py                          # UI 문구 (영어/중국어, 시스템 언어 따름)
│   ├── demo-statusline.py               # 데모 렌더러 (터미널에서 직접 실행)
│   ├── statusd.py                       # 선택: 상주 데몬
│   ├── dspg-status.c                    # 선택: 네이티브 shim 소스 (build.cmd)
│   ├── fetch-holidays.py                # 중국 법정 공휴일 수집
│   ├── ds-count.py                      # 로컬 tokenizer 카운트
│   └── price-watch.py                   # 가격 페이지 지문 자체 점검
├── demo-shot.png
├── README.md / README.zh-CN.md / README.ja.md / README.ko.md
```

## License

MIT
