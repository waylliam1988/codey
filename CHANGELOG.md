# Changelog

[中文版本](CHANGELOG.zh-CN.md)

This file records Codey's release history. The newest release appears first.

## 0.1.33 - Read-before-edit Guard

Added a run-scoped guard that rejects replacement edits to existing files until
the Writer has successfully read that file in the current agent run. Full
`content` writes are limited to new-file creation; existing files must use exact
replacements. Files created or changed during the run become known for follow-up
replacement edits. This keeps Symbol overview as navigation help without
letting it become a substitute for inspecting real file contents. DeepSeek and
GLM also auto-click their visible rate-limit retry buttons after a short
cooldown. The initial project prompt now omits absolute temporary paths and the
empty instructions section; repository instructions are included only when an
`AGENTS.md` or `CLAUDE.md` file exists.

## 0.1.32 - Bounded Symbol Overview

Added a task-aware Symbol overview inside the existing Project Map so the
Writer starts with better file and symbol navigation hints before its first
read. It remains bounded and local-only: no new UI, public tool, cache, index,
embedding, LSP, or source body injection. Qwen also gained a narrow recovery
for redirect aborts and one stalled-response retry.

## 0.1.31 - Structured Execution Evidence

Added a bounded in-memory execution ledger so Verification Map, Review, receipts, and successful project facts use the same read, search, edit, truncation, and post-edit check evidence.

## 0.1.30 - Simplified Navigation Tooling

Removed the withdrawn `outline_file` tool after live evaluation showed that Project Map, literal `grep`, `find_references`, and offset `read_file` formed the more reliable navigation path.

## 0.1.29 - Verification Map

Added a hidden, bounded map of test candidates and checks observed after the latest edit for the Reviewer. It is evidence for verification decisions, not impact or coverage proof.

## 0.1.28 - Durable Execution Checkpoint

Added session-scoped recovery facts for unfinished project work: changed-file hashes, fresh successful checks, the last edit or run action, and the stop reason.

## 0.1.27 - Find References and Bounded Scans

Added bounded lexical reference hints and a shared streaming scanner for references, grep, and hidden audits, with explicit incomplete-result reporting.

## 0.1.26 - Outline File Experiment

Introduced `outline_file` as a bounded navigation experiment. Natural-use evaluation later showed weak adoption, and the tool was fully removed in 0.1.30.

## 0.1.25 - Hidden Project Map

Added a bounded, read-only project structure map for Writer, hidden advisors, and Reviewer without indexing source, adding RAG, or exposing a new UI.

## 0.1.24 - Hidden Change Briefs

Added a private, bounded ChangeBrief shared by Writer and Reviewer, plus verified successful-change facts derived from real edits and checks.

## 0.1.23 - Browser Launch Robustness

Added Edge-first browser discovery with Chrome fallback, clearer WebView startup failure handling, and explicit truncation markers for tool and review results.

## 0.1.22 - Durable Conversation Handoff

Added a bounded visible-conversation excerpt to factual handoff when a browser-model context is no longer trusted.

## 0.1.21 - Durable Chat State

Persisted bounded sidebar and chat state, added quiet copy controls, and reconciled Send/Stop state across restarts.

## 0.1.20 - Quiet Chat Controls

Refined the compact chat controls and interaction states without adding a new workflow or mode.

## 0.1.19 - MiMo Answer Completion

Separated MiMo send-button detection from answer-completion detection and used the answer DOM to avoid premature completion.

## 0.1.18 - Provider Reliability

Tightened MiMo, Qwen, and GLM browser-state handling, local JSON protocol validation, and review-repair check freshness.

## 0.1.17 - Hidden MoA Layer

Added hidden owner-first multi-model advice for normal chat and new projects, plus bounded read-only advisor audits for existing projects.

## 0.1.16 - Plain Chat and Project Discussion

Kept New Chat project-free while allowing one project conversation to move naturally from discussion to reading and editing.

## 0.1.15 - GLM Provider

Added GLM as the fourth supported web model and consolidated provider registration and smoke selection.

## 0.1.14 - Protocol Efficiency and Safety

Unified the local tool contract, bounded safe parallel reads, paged large files, and made multi-replacement edits atomic.

## 0.1.13 - Runtime Ownership Cleanup

Unified Git and snapshot change handling, centralized runtime storage, and made provider-session ownership explicit.

## 0.1.12 - Resilient Run Reconciliation

Added bounded backend run snapshots and ordered UI reconciliation across refreshes and short connection interruptions.

## 0.1.11 - Responsive Stop

Made Stop interrupt provider waits, recovery, review, and controlled commands, and preserved both ends of long command output.

## 0.1.10 - ProfileDoctor Recovery

Added a bounded, sanitized second recovery step that can ask an already-open model to choose among structural browser-control candidates.

## 0.1.9 - Bounded Provider Recovery

Added versioned provider profiles and conservative, verified rediscovery for changed message boxes, send buttons, and answers.

## 0.1.8 - Durable Local Continuity

Persisted a small set of proven project commands, bounded factual chat snapshots, and non-Git recovery baselines.

## 0.1.7 - Structured Runtime

Introduced structured tool outcomes and events, separated task orchestration from HTTP transport, and removed UI parsing of prose logs.

## 0.1.6 - Hidden Context Handoff

Added bounded factual summarization and fresh-chat continuation near the shared context budget.

## 0.1.5 - Control Teaching Cleanup

Refined recovery and cleanup around user-taught browser controls while keeping teaching as a quiet last resort.

## 0.1.4 - Task Receipts

Added compact task receipts showing changed files, check status, and restore availability.

## 0.1.3 - Durable CDP Browser Reuse

Reused an existing Edge CDP browser and model tabs across Codey UI restarts before launching a new browser.

## 0.1.2 - Provider Status and Composer Shortcuts

Improved provider status feedback and keyboard-oriented message composer controls.

## 0.1.1 - Stability Smoke

Added release-level stability smoke coverage for the initial local browser-model workflow.

## 0.1.0 - Initial Bilingual Release

Published the first bilingual Codey release: a local-first bridge from supported web AI chats to controlled file editing, checks, diffs, and restore.
