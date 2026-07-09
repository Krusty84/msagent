# Minos Documentation Assistant

`Minos` is the Agent for documentation UX review and code review assistance. It checks README, installation guides, Quick Start, onboarding paths, and GitCode PR quality from a new-user and maintainer perspective.

## Positioning

- Handles README walkthroughs, installation flow validation, onboarding review, and GitCode PR review.
- Focuses on documentation usability, missing prerequisites, blockers, regression risk, and change quality.
- Fits maintainer-facing improvement lists and evidence-based review comments.

## Start

```bash
msagent --agent Minos
```

From source:

```bash
uv run msagent --agent Minos
```

## Prerequisites and Recommended Input

- For documentation review, provide repository path, target document path, user persona, and the flow to check.
- For PR review, provide the PR link, changed scope, review focus, and local verification results.
- If Minos should execute installation or build commands, state the allowed command scope clearly.

Example:

```text
Review docs/zh/getting_started/quick_start.md from a first-time user perspective, then list blockers, evidence, and suggested fixes.
```

## Expected Output

Minos usually returns a documentation usability conclusion, blockers, evidence, priority, and suggested edits. For PR review, it should prioritize evidence-backed risks, behavioral regressions, compatibility issues, and missing verification.

## Notes

- Minos is for documentation UX and code review, not for performance, accuracy, or quantization analysis.
- Execution-based reviews should record commands, outputs, and failure points.
- If only a document snippet is provided, the conclusion should be limited to that material.
