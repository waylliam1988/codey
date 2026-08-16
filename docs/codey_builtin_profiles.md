# Codey Built-in Profiles v1

This document describes Codey's built-in default-profile catalog. The catalog is
metadata only in v1. It is not a user-facing UI, configuration platform, plugin
system, router, permission engine, prompt patch layer, or provider selector.

Profiles describe conservative default tendencies that future code may use only
after preserving these rules:

- Explicit user provider and mode choices win.
- Permission profiles cannot be relaxed.
- Prompt text cannot be patched by profile metadata.
- Research network access cannot be enabled outside the normal Research path.
- Local context update settings cannot override explicit user choices.
- UI wording must stay quiet and avoid internal implementation terms.

| profile_id | purpose | mode_bias | provider_scope | research_network | review_enabled | local_context_updates_default | ui_detail_level | boundary |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| default | Current behavior as explicit metadata | chat, project, research, review | all | true | true | existing_default | quiet | Balanced catalog entry; no runtime dispatch in v1 |
| research_heavy | Prefer Research when a task clearly needs outside evidence | research, hybrid, project, review | all | true | true | existing_default | quiet | Does not auto-network or override explicit provider choice |
| review_strict | Prefer stricter read-only review defaults | review, planning | all | false | true | existing_default | quiet | Does not declare a writer write default |
| local_only | Prefer local model surfaces and avoid web Research | chat, project, review, planning | local | false | true | existing_default | quiet | Provider scope is local only; no network Research or research permission default |
| beginner | Keep explanations quiet and avoid internal implementation terms | chat, project, research, review | all | true | true | existing_default | beginner | Metadata only; no new UI or visible switch in v1 |

## V1 Non-Goals

- No profile picker in the main UI.
- No user plugin directory.
- No dynamic imports, entry points, or third-party package loading.
- No prompt patching.
- No provider fallback changes.
- No Router changes.
- No permission changes.
- No SSE, receipt, or task event shape changes.

## Test Contract

`tests/test_builtin_profiles.py` treats `codey/builtin_profiles.py` as the
source of truth and locks:

- fixed profile ids,
- stable JSON export and fingerprint,
- valid capability ids,
- explicit permission profile names,
- known provider scopes,
- no user-provider or user-mode override flags,
- no permission relaxation,
- empty prompt patches,
- local-only network Research disabled,
- local-only research permission default absent,
- review-strict writer write defaults absent,
- beginner-facing copy free of internal terms.
