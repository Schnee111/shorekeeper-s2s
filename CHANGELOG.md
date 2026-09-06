# Changelog

## [0.3.0](https://github.com/Schnee111/shorekeeper-s2s/compare/shorekeeper-s2s-v0.2.1...shorekeeper-s2s-v0.3.0) (2026-09-06)


### Features

* **ci:** register and execute comprehensive e2e test suite in ci-cd workflow ([521cf11](https://github.com/Schnee111/shorekeeper-s2s/commit/521cf11e23ca8e687f267e4171ed4bc5c25232bf))


### Bug Fixes

* **agent:** handle empty string overrides properly in search_mempalace_mcp to fix test_search_mempalace_unconfigured ([1d49774](https://github.com/Schnee111/shorekeeper-s2s/commit/1d497747d1f6ea12b196a802916dc8fab4c2884e))
* **ci:** point pytest directly to tests folder inside apps/agent to eliminate ambiguous test discovery ([fa730b2](https://github.com/Schnee111/shorekeeper-s2s/commit/fa730b251d305603fbff9e34117b274de0a4614e))

## [0.2.1](https://github.com/Schnee111/shorekeeper-s2s/compare/shorekeeper-s2s-v0.2.0...shorekeeper-s2s-v0.2.1) (2026-09-06)


### Bug Fixes

* **client:** pin pnpm 9.15.9 via npm in Dockerfile to eliminate corepack undici assertion crash ([07b0451](https://github.com/Schnee111/shorekeeper-s2s/commit/07b0451ea1e188306e65013f807d9c523f950628))

## [0.2.0](https://github.com/Schnee111/shorekeeper-s2s/compare/shorekeeper-s2s-v0.1.0...shorekeeper-s2s-v0.2.0) (2026-09-06)


### Features

* **agent,omp-bridge:** complete Fase 5 (async consult) & Fase 7 (reconciliation) ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([e676ef1](https://github.com/Schnee111/shorekeeper-s2s/commit/e676ef17b7b0411fbea06356104c0df4904ea0c8))
* **agent:** add proactive outbox polling loop for voice completion push ([b101766](https://github.com/Schnee111/shorekeeper-s2s/commit/b101766fdd0c178dbdd792962c13b64248e68c64))
* **agent:** enhance task status and voice delivery with substantive real data results ([#17](https://github.com/Schnee111/shorekeeper-s2s/issues/17)) ([e3f012f](https://github.com/Schnee111/shorekeeper-s2s/commit/e3f012fa0db5434c580d5b425cb34dfd6a7be139))
* **agent:** implement Gemini 3.1 Flash Live native realtime voice front and 30 celestial voices registry ([95cb976](https://github.com/Schnee111/shorekeeper-s2s/commit/95cb976e87f88fbb1ebb5d455486b0530d1cecbd))
* **agent:** implement VoiceNotificationPolicy and state machine gate (Fase 4) ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([cbc8c6e](https://github.com/Schnee111/shorekeeper-s2s/commit/cbc8c6e07b22ce2c15f979150597fb56ba8325c4))
* **agent:** integrate SearXNG web search tool into Gemini Live voice agent ([c07c3bb](https://github.com/Schnee111/shorekeeper-s2s/commit/c07c3bbb6157f6828fe54272084ebb4dbb8ff0d3))
* **agent:** replace periodic 2s outbox polling with asyncio event trigger ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([8550c12](https://github.com/Schnee111/shorekeeper-s2s/commit/8550c12a45ec93bd6bd5904601aa2c7bafa56ac2))
* **agent:** update check_task_status to fetch and format substantive findings ([b234df7](https://github.com/Schnee111/shorekeeper-s2s/commit/b234df70a7e2ce4683d978e9b985226ea1c348ed))
* **conflict-map:** implement resource and dependency-aware admission control (Fase 6) ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([9387741](https://github.com/Schnee111/shorekeeper-s2s/commit/93877412b9dc85c06ce1343fbd003c73b3e3ddad))
* **contracts,task-store:** add waiting_input & unknown states and root_task_id lineage (Fase 2) ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([b738ba4](https://github.com/Schnee111/shorekeeper-s2s/commit/b738ba449f4b9ff81e2124256c3fb36db4fa7587))
* **daemon:** enhance Hermes WS stream collection and output clipping ([#16](https://github.com/Schnee111/shorekeeper-s2s/issues/16)) ([d1a1057](https://github.com/Schnee111/shorekeeper-s2s/commit/d1a1057046d3000f0c4bf7e30c50846bc05f159a))
* **devops:** introduce multi-stage containerization, CI/CD pipeline, and SemVer release workflow ([b53f747](https://github.com/Schnee111/shorekeeper-s2s/commit/b53f74780ba1a7156a23df9b06d9d6472403a1fc))
* **event-bus:** implement in-process typed TaskEventBus with sequence check ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([7f4fab0](https://github.com/Schnee111/shorekeeper-s2s/commit/7f4fab0c39617f5c7b5caa4647d2023ff483aeba))
* **monorepo:** initial commit of Shorekeeper realtime voice agent and orchestration platform ([d35b84c](https://github.com/Schnee111/shorekeeper-s2s/commit/d35b84ce1f4806bfcc40a36c28e065bf43e593b6))
* **omp-bridge,contracts:** extract TaskSupervisor and Command/Receipt contracts (Fase 3) ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([3801dca](https://github.com/Schnee111/shorekeeper-s2s/commit/3801dcaaf91f02c63b86eb8c61f9b11778f46348))
* **orchestrator:** add background worker daemon and fix front agent tool invocation guardrails ([8668a83](https://github.com/Schnee111/shorekeeper-s2s/commit/8668a834d219fb052f102bbd295c6c43aa4cabfd))
* **runtime:** integrate Hermes WS token, switch off mock mode, and harden systemd threads ([#1](https://github.com/Schnee111/shorekeeper-s2s/issues/1), [#2](https://github.com/Schnee111/shorekeeper-s2s/issues/2), [#3](https://github.com/Schnee111/shorekeeper-s2s/issues/3)) ([2e9402f](https://github.com/Schnee111/shorekeeper-s2s/commit/2e9402f5d55601d63993ae585deb90e8d01208a1))
* **task-store:** add task_outbox table and atomic event recording ([#4](https://github.com/Schnee111/shorekeeper-s2s/issues/4)) ([57fc676](https://github.com/Schnee111/shorekeeper-s2s/commit/57fc6768836a896759f048191323a752cc701c3f))


### Bug Fixes

* **agent:** correct google.TTS constructor signature in agent_gemini_live.py ([f471a07](https://github.com/Schnee111/shorekeeper-s2s/commit/f471a0703a03a8d746a657bdb762c58c3b121a30))
* **agent:** implement JSON-RPC tools/call client connector for MemPalace L2 ([#18](https://github.com/Schnee111/shorekeeper-s2s/issues/18)) ([c29dea4](https://github.com/Schnee111/shorekeeper-s2s/commit/c29dea4ddf11d9d9fc78c8a1bcf464280135b644))
* **agent:** properly attach GoogleTTS to AgentSession for session.say push notifications ([03ff172](https://github.com/Schnee111/shorekeeper-s2s/commit/03ff1724643a0afc96159fa5aeb99a58de62031b))
* **agent:** shutdown callback must be awaitable — cancel() returning bool caused TypeError on room disconnect ([734c7a2](https://github.com/Schnee111/shorekeeper-s2s/commit/734c7a219ecfa999064d5198416acc14ef43c2fa))
* **agent:** standardize Gemini API keys and mitigate AFC warning ([#10](https://github.com/Schnee111/shorekeeper-s2s/issues/10)) ([a4b46eb](https://github.com/Schnee111/shorekeeper-s2s/commit/a4b46eb6a59b22073695e3e4ff7186319ec2a60b))
* **agent:** tune search_mempalace timeout to 3.5s for peak load safety ([9c83995](https://github.com/Schnee111/shorekeeper-s2s/commit/9c8399530dbae9307237bfa95d9c9ed8a4dd9dc5))
* **agent:** use beta.GeminiTTS to avoid Vertex ADC credential error on VPS ([2d4428c](https://github.com/Schnee111/shorekeeper-s2s/commit/2d4428cfed004e4361c69a07bf5e5a3eea35aec8))
* **agent:** use session.say() for proactive voice notification delivery ([07f5cd5](https://github.com/Schnee111/shorekeeper-s2s/commit/07f5cd5a49524dd3bc4d85f0ea2066fc0b3d5227))
* **client:** enforce 48kHz audio capture constraints on microphone track ([#11](https://github.com/Schnee111/shorekeeper-s2s/issues/11)) ([8c08d05](https://github.com/Schnee111/shorekeeper-s2s/commit/8c08d05ff51cd8276d133a5de019b830be7547e1))
* **daemon:** enable MOCK mode as production default for fast task execution ([0e53d8a](https://github.com/Schnee111/shorekeeper-s2s/commit/0e53d8aa058e892cd25ab6bec9006a92de363929))
* **deploy:** livekit-api 1.2.x token API compat + conflict-free agent HTTP port ([42bba82](https://github.com/Schnee111/shorekeeper-s2s/commit/42bba82c2bb762741373670bc39d3d5680a8a7e1))
* **systemd:** resolve invalid environment syntax in shorekeeper-daemon.service ([#9](https://github.com/Schnee111/shorekeeper-s2s/issues/9)) ([075e8af](https://github.com/Schnee111/shorekeeper-s2s/commit/075e8af70afde6802edc41c6aae9010e3a771b8e))
* wrap it in an async function that cancels + awaits + swallows CancelledError. ([734c7a2](https://github.com/Schnee111/shorekeeper-s2s/commit/734c7a219ecfa999064d5198416acc14ef43c2fa))


### Performance Improvements

* **agent:** eliminate voice room cold start via prewarmed process pool ([#8](https://github.com/Schnee111/shorekeeper-s2s/issues/8)) ([83330c4](https://github.com/Schnee111/shorekeeper-s2s/commit/83330c43ee22dfe1f3e4c84c46906ac0a2896373))
