# Shorekeeper 🦋

[![CI/CD Pipeline](https://img.shields.io/github/actions/workflow/status/Schnee111/shorekeeper-s2s/ci-cd.yml?branch=main&style=flat-square&label=CI%2FCD)](https://github.com/Schnee111/shorekeeper-s2s/actions)
[![Release](https://img.shields.io/github/v/release/Schnee111/shorekeeper-s2s?style=flat-square&color=c5a86a)](https://github.com/Schnee111/shorekeeper-s2s/releases)
[![GHCR Image](https://img.shields.io/badge/GHCR-Containerized-6ee7b7?style=flat-square&logo=docker)](https://github.com/Schnee111/shorekeeper-s2s/pkgs/container/shorekeeper-s2s-agent)
[![Quality Gates](https://img.shields.io/badge/Quality%20Gates-Passed%20(Exit--0)-emerald?style=flat-square)](scripts/gates/)
[![Architecture](https://img.shields.io/badge/Architecture-Event--Driven%20Multi--Agent-blue?style=flat-square)](docs/ARCHITECTURE.md)
[![License](https://img.shields.io/badge/License-MIT-purple?style=flat-square)](LICENSE)

> **Shorekeeper** is a production-grade, voice-first multi-agent autonomous engineering monorepo. Featuring a real-time WebRTC conversational interface (LiveKit + Gemini Live Speech-to-Speech), an intelligent orchestrator (Hermes Agent Gateway), and asynchronous worker execution engines (oh-my-pi / omp) — unified by an atomic SQLite WAL task store, cross-wing semantic memory (MemPalace L2), and full self-hosted telemetry (OpenTelemetry + Jaeger + Prometheus).

---

## 🌌 The Shorekeeper Ecosystem

Shorekeeper is partitioned into specialized repositories tailored for distinct operational models:

| Repository | Role | Architecture | Latency / Stack | Status |
| :--- | :--- | :--- | :--- | :--- |
| [**shorekeeper-s2s**](https://github.com/Schnee111/shorekeeper-s2s) | Flagship Monorepo | Native Speech-to-Speech (Gemini Live) | Sub-second bidirectional WebRTC, SQLite WAL, OTel | `Active (v0.1.0)` |
| [**shorekeeper-cascade-client**](https://github.com/Schnee111/shorekeeper-cascade-client) | Modular Client HUD | Svelte 5 Web HUD + 3D Spectro Particle Orb | Vite, Web Audio Worklet, TailwindCSS | `Active (v2.2.0)` |
| [**shorekeeper-cascade-agent**](https://github.com/Schnee111/shorekeeper-cascade-agent) | Modular Pipeline Agent | Decoupled Cascade (STT ➔ LLM ➔ TTS) | Groq Whisper + Hermes LLM + Fish Audio 48kHz | `Active (v1.7.2)` |

---

## 🌟 Core Highlights

- **Native Speech-to-Speech (S2S):** Direct duplex WebRTC audio streaming with Gemini 3.1 Live. Instant interruption recovery and natural prosody with sub-second turnaround.
- **Zero-Cost Sovereign Stack:** 100% built on free tiers and sovereign self-hosted software. Zero paid subscription APIs.
- **Persistent Semantic Recall:** Seamless integration with MemPalace L2 over JSON-RPC 2.0 (`POST /mcp`), retrieving historical ADRs, project context, and user preferences in under 300 ms.
- **Crash-Resilient Task Store:** SQLite WAL with single-writer orchestrator architecture, atomic outbox dispatching, and deterministic deduplication surviving host restarts.
- **Self-Hosted Telemetry:** Distributed tracing via OpenTelemetry SDK, OTLP collectors, Jaeger, and Prometheus.
- **Automated DevOps & SemVer:** Continuous container builds published to GitHub Container Registry (`ghcr.io/schnee111/shorekeeper-s2s-*`), managed via Google Release Please.

---

## 🏗️ Monorepo Structure

```text
shorekeeper/
├── apps/
│   ├── agent/             # LiveKit S2S Python Agent (Gemini Live WebRTC)
│   ├── client/            # Svelte 5 + Vite Voice HUD & Spectro Visualizer
│   └── token-server/      # Ephemeral LiveKit JWT Token Server (aiohttp :8083)
├── packages/
│   ├── contracts/         # Zod schemas for task handoff & event contracts
│   ├── conflict-map/      # Resource contention & pre-merge conflict detection
│   ├── event-bus/         # In-memory typed pub/sub event bus
│   ├── merge-orchestrator/# Automated sequential rebase/squash verifier
│   ├── observability/     # OpenTelemetry tracing & fail-open metrics
│   ├── omp-bridge/        # Asynchronous worker bridge & process supervisor
│   └── task-store/        # SQLite WAL persistence engine & outbox queue
├── deploy/                # Nginx proxy profiles, systemd units & OTel configs
└── scripts/               # Quality gates, E2E stress suites, and evaluation rubrics
```

---

## 🚀 Quickstart

### Prerequisites
- Node.js >= 22.18 & `pnpm`
- Python 3.11 & `uv`
- Docker & Docker Compose (optional for containerized deployment)

### Local Development Setup

```bash
# 1. Clone repository
git clone https://github.com/Schnee111/shorekeeper-s2s.git
cd shorekeeper-s2s

# 2. Install workspace dependencies
pnpm install

# 3. Build monorepo packages
pnpm -r build

# 4. Sync Python agent dependencies
cd apps/agent
uv sync

# 5. Run quality gates & test suites
pnpm -r test
uv run pytest -v tests/
```

---

## 🛡️ Production DevOps & Containerization

Production deployment is fully automated via GitHub Actions to GitHub Container Registry (GHCR):

```bash
# Pull and start containerized stack
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

### Resource Allocation (3.6GB VPS Guard)
- **Agent Container (`shorekeeper-s2s-agent`)**: Capped at `800MB RAM`
- **Token Server Container (`shorekeeper-s2s-token-server`)**: Capped at `256MB RAM`
- **Client Bundle (`shorekeeper-s2s-client`)**: Multi-stage `nginx:alpine-slim` runtime (`<25MB RAM`)

---

## 📜 License & Author

- **Author**: Muhammad Daffa Ma'arif ([@Schnee111](https://github.com/Schnee111))
- **License**: [MIT License](LICENSE)
