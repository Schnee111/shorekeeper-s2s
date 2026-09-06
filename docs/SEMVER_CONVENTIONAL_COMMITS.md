# Semantic Versioning & Conventional Commits Guide

This repository enforces **Semantic Versioning (SemVer 2.0.0)** managed autonomously via **Google Release Please**.

## Format Specification

```text
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

### Commit Types & Release Triggers

| Commit Type | SemVer Impact | Description |
| :--- | :--- | :--- |
| `feat:` | **MINOR** (`x.Y.z`) | A new feature or capability introduced to the codebase |
| `fix:` | **PATCH** (`x.y.Z`) | A bug fix or corrective implementation |
| `docs:` | **PATCH** (`x.y.Z`) | Documentation improvements or architectural guides |
| `perf:` | **PATCH** (`x.y.Z`) | Performance optimizations |
| `refactor:` | **PATCH** (`x.y.Z`) | Code changes that neither fix bugs nor add features |
| `feat!:` or `BREAKING CHANGE:` | **MAJOR** (`X.y.z`) | Breaking changes that alter public APIs or schemas |
| `chore:` / `ci:` / `style:` | *No release* | Maintenance or CI configuration updates |

## Automated Release Lifecycle

1. **Commit & PR**: Open PRs targeting `main` formatted with conventional commit titles.
2. **Release Draft PR**: Release Please autonomously aggregates commits into an active release PR updating `CHANGELOG.md` and `package.json`.
3. **Autonomous Merge & Tag**: The `release-pr-monitor` watchdog reviews green check gates and rebase-merges the release PR.
4. **Container Build**: GHCR containers are automatically built and published to `ghcr.io/schnee111/shorekeeper-s2s-*`.
5. **Continuous Deployment**: VPS pulls the verified images and updates the live service.
