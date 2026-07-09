# Skill Development, Deployment, and Troubleshooting

This page is a compact entry point for creating, enabling, and troubleshooting msagent Skills. For the full Skill list, see repository path `skills/README.md`.

## Minimal Development Flow

1. Create a kebab-case directory under `skills/` and add `SKILL.md`.
2. Set `name` and `description` in the `SKILL.md` frontmatter.
3. Put deterministic scripts in `scripts/` and long references in `references/` when needed.
4. Allow the Skill in the target Agent YAML `skills.patterns`.
5. Start `msagent` and verify visibility with `/skills`.

Minimal structure:

```text
skills/
  my-skill/
    SKILL.md
```

Minimal `SKILL.md`:

```md
---
name: my-skill
description: Use this skill for a specific repeatable task.
---

# My Skill

Use this skill when the user asks for this workflow.
```

## Deployment Entry Points

| Scenario | Entry point | Notes |
|---|---|---|
| Source development | `skills/` | Built-in Skill source in the repository root. |
| Project-local extension | `<working-dir>/skills` | Skill source for the current working directory. |
| Runtime install | `.msagent/skills` | Local configuration directory used by runtime installation flows. |

For full loading order, shadowing behavior, and source / wheel differences, see the [Chinese configuration guide](../../zh/user_guide/configuration-and-extension.md).

## Make an Agent See the Skill

The target Agent must allow the Skill in its YAML configuration:

```yaml
skills:
  patterns:
    - default:my-skill
  use_catalog: false
```

For categorized Skills:

```yaml
skills:
  patterns:
    - profiling:my-skill
  use_catalog: false
```

For the full matching rules, see the [Chinese Agent / Tool / Skill filter guide](../../zh/user_guide/agent-tool-skill-filter-rules.md).

## Local Verification

For source development:

```bash
uv sync --dev
uv run msagent --version
```

Inside a session:

```text
/skills
/skills my-skill
/skills profiling/my-skill
```

## Troubleshooting

| Symptom | Check first |
|---|---|
| The Skill does not appear in `/skills` | Path is scanned; file is named `SKILL.md`; target Agent allows it in `skills.patterns`; no higher-priority Skill shadows it. |
| The Skill appears but is not selected automatically | `description` is specific enough; the task matches the Agent domain; the prompt includes required data paths or context. |
| Agent configuration changes do not apply | A stale `.msagent/` directory may already exist in the current working directory. Back it up before regenerating it. |
| Scripts or external tools are unavailable | Document prerequisites in `SKILL.md`, keep deterministic scripts in `scripts/`, and mark placeholder paths clearly. |

## Related Documents

- [Chinese configuration and extension guide](../../zh/user_guide/configuration-and-extension.md)
- [Chinese Agent / Tool / Skill filter rules](../../zh/user_guide/agent-tool-skill-filter-rules.md)
- [Chinese custom Agent guide](../../zh/developer_guide/add-agent.md)
- `skills/README.md`
