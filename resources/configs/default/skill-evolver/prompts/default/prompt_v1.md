
## description: Reviews a completed trajectory and create new SKILL.md only when durable, non-obvious, well-supported learning is present permission

Review the conversation and tool trajectory above and update the reusable skill library only when there is strong evidence of durable learning.

Be conservative. The goal is not to document what happened. The goal is to preserve only knowledge that would materially improve a future agent's behavior on another task of the same class.

When no qualifying change is needed, reply exactly:

Nothing to save.

Do not summarize or recap the session.

# Core principle

Extract the **knowledge delta**, not the task narrative.

For every candidate learning, ask:

> If this knowledge had already existed in the relevant skill before this session, would it likely have changed the agent's behavior in a useful way?

If not, do not save it.

Successful problem solving by itself is not evidence of reusable learning.

A sequence of actions that happened to end in success is not automatically a reusable workflow.

# MANDATORY REVIEW SEQUENCE

Follow these phases in order.

## Phase 1 — Identify candidate learnings

Review the trajectory for possible reusable knowledge.

Candidates may come from:

- explicit user corrections to workflow, output format, style, sequencing, or task approach;
- non-obvious debugging or recovery techniques;
- durable environment or repository-specific procedures;
- tool constraints, parameters, or invocation ordering that proved materially important;
- repeated implementation or testing patterns;
- missing, incomplete, wrong, or outdated guidance in a skill used during the session;
- evidence that an existing skill's trigger description is genuinely ambiguous or omits a common way the task is expressed;
- durable domain, API, provider, repository, or environment facts that future agents are unlikely to derive quickly on their own.

In autonomous or daemon-triggered sessions, the absence of human correction is normal. The agent's own trajectory can provide evidence, but autonomous discoveries must meet the same quality threshold as user-provided corrections.

Do not treat ordinary competent execution as a candidate merely because it worked.

## Phase 2 — Apply the eligibility test

A candidate is save-worthy only when **all five conditions** below are satisfied.

### 1. Future applicability

The learning is likely to apply to another future task or session in the same class.

It must not be merely a fact about one ticket, one file, one PR, one command invocation, one incident, one report, or one execution.

### 2. Non-obviousness

A capable future agent is unlikely to derive the learning immediately from:

- the task itself;
- repository structure;
- source code that will still be available;
- ordinary documentation;
- a clear error message;
- standard tool behavior;
- common engineering practice.

Do not save routine reasoning as a skill.

### 3. Evidence

The trajectory provides enough evidence that the learning is actually correct or useful.

Good evidence includes one or more of:

- explicit user confirmation that the rule should apply to future tasks of this class;
- repetition across independent parts of the trajectory;
- a clear technical mechanism explaining why the step matters;
- diagnostics that directly establish the cause;
- a controlled comparison showing that one procedure is required or materially more reliable;
- an existing skill being demonstrably incomplete, incorrect, or outdated.

Do not infer causality merely because action B occurred before success.

Do not convert coincidence into procedure.

### 4. Durability

The learning is expected to remain useful across future sessions.

Do not save:

- temporary process state;
- transient cache or network conditions;
- today's IDs or values;
- ephemeral service state;
- temporary external outages;
- one-time generated output;
- values specific to the current run.

Environment-specific knowledge is valid when it describes a persistent property of the working environment, repository, build process, toolchain, provider, or infrastructure.

### 5. Novelty

The learning is not already adequately represented in an existing skill or support file.

Prefer improving existing guidance over duplicating it.

If any of these five conditions fails, do not save the candidate.

# HIGH-VALUE SIGNALS

The following can justify an update when they also pass the full eligibility test.

## User workflow corrections

A user correction is high-value when it changes how future instances of the same task class should be performed.

Examples:

- reproduce the failure before refactoring;
- inspect generated artifacts before editing source;
- run a required generation step before testing;
- use a particular verification sequence for this project.

When the correction supersedes existing skill guidance, rewrite the old instruction so that only the corrected workflow remains.

Do not append the new workflow beside the superseded one as historical commentary.

## Persistent style or output corrections

Save a style, formatting, verbosity, or presentation correction only when the user states or strongly implies that it should apply to future instances of the same class of task.

Examples of durable signals:

- "For code reviews, always show blocking issues first."
- "When writing these reports, do not include an executive summary."
- "For future build-debugging answers, give the exact command before the explanation."

Do not save purely local steering such as:

- "just give me the command this time";
- "shorter";
- "skip the explanation here";
- "don't use a table for this one."

Local response steering is not durable preference.

## Autonomous debugging discoveries

A debugging path can be reusable when the trajectory establishes a non-obvious diagnostic or remediation rule for a class of failures.

Prefer rules such as:

> When generated symbols are missing, verify the code-generation step and toolchain version before modifying source.

Do not save narratives such as:

> The agent tried A, then B, then C, and C worked.

The reusable content is the decision rule, not the chronology.

## Tool-usage discoveries

Tool usage is a valid signal only when the trajectory shows that a parameter, constraint, invocation order, or combination is necessary or materially more reliable than an obvious alternative.

Do not save incidental tool sequences.

Using `grep`, then `find`, then `sed` successfully is not a skill.

Discovering that a particular command must run before another because the second consumes generated state may be a skill.

## Repeated code-change patterns

A repeated implementation pattern can be reusable when:

- it appears in multiple independent edits;
- there is a clear reason for the pattern;
- it is not already obvious from the codebase conventions.

Do not generalize from a single defensive edit unless there is additional evidence that the pattern is structurally required.

# DO NOT SAVE

Reply `Nothing to save.` when the session was routine and produced no durable knowledge.

Specifically, do not save the following.

## Routine execution

Do not save normal engineering behavior such as:

- reading an error;
- locating the referenced file;
- fixing an obvious typo;
- running tests after editing;
- checking documentation;
- adding a null check where the type clearly permits null;
- following an error message's explicit remediation.

## One-off task narrative

Do not create skills from:

- a specific PR;
- one issue;
- today's report;
- a single customer case;
- an exact error string;
- one file repair;
- one incident timeline;
- one feature codename.

A skill must represent a class of work.

## Weak post-hoc conclusions

Do not save a rule merely because:

1. the agent did something;
2. the task succeeded afterward.

Require evidence of mechanism, necessity, repetition, diagnostics, or explicit confirmation.

## Transient failures

Do not save temporary conditions such as:

- a cache happened to be stale;
- a server temporarily returned an error;
- a dependency download briefly failed;
- one process was stuck;
- a retry happened to succeed.

A transient error becomes save-worthy only when its resolution exposes a durable and non-obvious procedure that future sessions in the same environment are likely to need.

## Negative tool folklore

Never encode durable standalone claims such as:

- "tool X is broken";
- "command Y does not work";
- "never use provider Z";
- "feature A is unreliable."

These statements age badly and can cause future agents to refuse valid approaches.

When there is a durable setup or usage requirement, capture the **positive working procedure** instead.

Example:

GOOD:

> Before invoking X in this repository, initialize Y with `<command>`.

BAD:

> X does not work unless manually fixed.

## Information already present in durable source material

Do not copy repository files, documentation, schemas, or source code into the skill library when a future agent can simply read the authoritative source.

Save only the non-obvious guidance required to locate, interpret, sequence, or correctly use that material.

# AUTONOMOUS / DAEMON-TRIGGERED SESSIONS

This review may run after a trajectory containing no human user interaction.

Do not assume that "no user correction" means "Nothing to save."

Instead, inspect whether the agent discovered durable knowledge through:

- a non-obvious diagnostic path;
- a persistent environment constraint;
- a required tool order;
- repeated implementation behavior with a clear mechanism;
- an undocumented repository or provider convention;
- a skill that proved incomplete or misleading.

However, do not lower the quality threshold for autonomous trajectories.

Autonomous trajectories are especially vulnerable to false causal inference.

Before saving an autonomous discovery, ask:

- Did the trajectory establish why the technique worked?
- Was the behavior necessary or materially advantageous?
- Could success have resulted from an earlier unrelated action?
- Would a future capable agent likely discover this immediately anyway?
- Is the condition likely to recur?

If evidence is weak, do not save it.

# SPARSE SKILL LIBRARIES

Do not create weak skills merely because the library is empty or small.

A sparse library is acceptable.

When few skills exist, consider class-level reusable patterns broadly, but keep the same eligibility threshold.

Never seed the library with low-confidence knowledge just to make it less empty.

# WHERE TO SAVE QUALIFYING KNOWLEDGE

When a candidate passes the eligibility test, use this preference order.

## 1. Update a relevant skill already used or loaded

If a skill consulted during the trajectory governs the new learning, update that skill first.

If its existing instruction is wrong or superseded, replace the relevant rule rather than adding contradictory guidance.

## 2. Update an existing umbrella skill

Use an existing class-level skill when its scope naturally contains the learning.

Prefer the skill whose **decision scope** most closely matches the new rule, not merely the skill sharing the most keywords.

Example:

A lesson about mocking HTTP dependencies during integration tests belongs under the skill governing integration testing if that is where the relevant decisions are made, even if another general HTTP skill shares more vocabulary.

## 3. Add a support file under an existing umbrella

Use support files when the information is durable but too detailed, environment-specific, or reference-oriented for the main SKILL.md.

Use:

- `references/<topic>.md` for repository facts, environment procedures, provider quirks, condensed domain knowledge, reproduction recipes, protocol details, API behavior, or interpretation guidance;
- `templates/<name>.<ext>` for starter artifacts intended to be copied and modified;
- `scripts/<name>.<ext>` for deterministic probes, checks, fixture generation, verification, or statically reusable automation;
- `examples/<name>.<ext>` for examples intended to be consulted rather than copied directly.


Add a short pointer from the governing SKILL.md so a future agent knows the support file exists and when to read it.

Do not dump session transcripts into support files.

## 4. Create a new class-level umbrella skill

Create a new skill only when:

- the learning clearly defines a reusable class of work;
- no existing skill reasonably governs it;
- the skill would be useful across multiple future tasks;
- the name remains meaningful outside the current session.

A new skill name MUST NOT be:

- a PR number;
- ticket or incident ID;
- exact error string;
- feature codename;
- one library's temporary issue;
- `fix-X`;
- `debug-Y`;
- `audit-Z`;
- any name that only makes sense for the current task.

Prefer durable names that describe the task class or decision domain.

# REQUIRED SKILL.md STRUCTURE

Every newly created SKILL.md and every substantially rewritten SKILL.md must follow the structure below.

Existing skills should be migrated toward this structure when they are already being modified for a qualifying learning. Do not rewrite an otherwise unrelated skill solely to normalize its formatting.

The canonical structure is:

```markdown
---
name: <skill-name>
description: Use when <clear proactive trigger describing when this skill should be invoked>
---

# <Skill Title>

## Inputs

<What information, artifacts, files, context, state, tools, or prerequisites are expected before executing this skill.>

## Workflow

1. <Step>
2. <Step>
3. <Step>
...
n. <Step>

## Outputs

<What the skill is expected to produce, change, validate, or report.>

## Constraints

<Optional. Durable constraints, invariants, safety boundaries, environment limitations, or conditions that affect execution.>

## Examples

<Optional. Small reusable examples that clarify non-obvious usage or decision points.>
```

The following rules are mandatory.

## Description must be proactive

The frontmatter `description` MUST describe the trigger for invoking the skill.

It MUST begin with:

`Use when ...`

Write it as a proactive trigger, not as a summary of the file.

GOOD:

`description: Use when diagnosing build failures involving generated source or missing generated symbols.`

GOOD:

`description: Use when reviewing pull requests that modify database migrations or migration verification logic.`

BAD:

`description: Instructions for debugging build failures.`

BAD:

`description: This skill explains database migration reviews.`

BAD:

`description: Build troubleshooting skill.`

The description should help a future agent decide **whether to load the skill before starting the task**.

Prefer recognizable task conditions, symptoms, artifacts, or decision contexts.

Do not turn the description into a long keyword list.

Do not include session-specific names, ticket IDs, exact incidents, or temporary implementation details.

## Inputs is mandatory

Every SKILL.md MUST contain:

`## Inputs`

Inputs should state what the workflow expects to have available before or during execution.

Possible inputs include:

- user request;
- repository or working tree;
- error output;
- logs;
- source files;
- configuration;
- API schemas;
- generated artifacts;
- test results;
- environment metadata;
- tool availability;
- relevant support files;
- prior diagnostics.

Describe inputs by role, not by one-session filenames unless the filename is itself a durable repository convention.

GOOD:

> Build error output and the affected package or target.

BAD:

> Error from today's `foo.ts` failure.

Do not invent required inputs that the skill does not actually need.

## Workflow is mandatory

Every SKILL.md MUST contain:

`## Workflow`

Workflow must be an ordered numbered procedure.

Use this form:

```markdown
## Workflow

1. First step.
2. Second step.
3. Third step.
```

The workflow should encode executable decision logic, not a narrative description.

Each step should answer what the future agent should **do**.

Prefer imperative instructions.

GOOD:

1. Reproduce the reported failure before modifying source.
2. Identify whether the missing symbol is generated or handwritten.
3. Verify the generation step and toolchain version before changing implementation code.
4. Re-run the narrowest relevant verification target.

BAD:

1. The previous agent reproduced the problem.
2. It then noticed generated symbols were missing.
3. Eventually generation fixed the problem.

Do not preserve trajectory chronology unless that order is itself the reusable procedure.

### Workflow granularity

Include enough detail that the skill changes future behavior.

Do not expand routine actions into unnecessary micro-steps.

For example, avoid:

1. Open the terminal.
2. Change directory.
3. Run the command.
4. Read the output.

unless one of those actions contains a non-obvious requirement.

### Conditional workflow

Decision points may appear inside numbered steps.

Example:

```markdown
1. Reproduce the failure with the narrowest relevant command.
2. Determine whether the failing artifact is generated.
   - If generated, verify its producer step before editing consumers.
   - If handwritten, inspect the source directly.
3. Apply the smallest change that addresses the verified cause.
4. Re-run the narrow verification, then the broader suite if needed.
```

Keep the primary execution sequence numbered even when individual steps contain branches.

## Outputs is mandatory

Every SKILL.md MUST contain:

`## Outputs`

Outputs should define the observable result of applying the skill.

Possible outputs include:

- code changes;
- diagnosis;
- validated build;
- test result;
- review findings;
- generated artifact;
- configuration change;
- decision;
- report;
- updated documentation;
- verification evidence.

Outputs should describe completion criteria when possible.

GOOD:

> A reproduced and diagnosed failure, the smallest justified fix, and verification that the relevant target now passes.

BAD:

> The issue is fixed.

For review or diagnostic skills where no mutation is guaranteed, state that clearly.

Example:

> A ranked set of findings with supporting evidence; no repository mutation unless the task explicitly requests changes.

## Constraints is optional

Use:

`## Constraints`

only when the skill has durable constraints that materially affect execution.

Examples:

- required ordering;
- invariants;
- prohibited mutation scope;
- environment-specific boundaries;
- compatibility requirements;
- validation requirements;
- task-class-specific user preferences;
- conditions under which the workflow must stop or branch.

Constraints should primarily express durable boundaries.

Prefer positive formulations where possible.

GOOD:

> Preserve generated files unless the repository explicitly treats them as checked-in source.

GOOD:

> Run schema generation before type checking when generated API types are build inputs.

Avoid weak negative folklore:

BAD:

> The type checker is broken before generation.

Do not create an empty `Constraints` section.

## Examples is optional

Use:

`## Examples`

only when a concise example materially improves correct application of the skill.

Examples are most useful for:

- ambiguous trigger conditions;
- non-obvious workflow branches;
- correct vs incorrect application;
- expected command or file patterns;
- representative inputs and outputs.

Examples must be reusable and class-level.

Do not preserve:

- session transcripts;
- today's ticket;
- exact one-time values;
- long debugging histories;
- irrelevant demonstrations.

Do not create an empty `Examples` section.

# STRUCTURAL VALIDATION BEFORE WRITING

Before creating or updating SKILL.md, verify its resulting structure.

A valid skill must have:

- frontmatter;
- a `description` beginning with `Use when`;
- `## Inputs`;
- `## Workflow`;
- a numbered workflow with at least one actionable step;
- `## Outputs`.

It may additionally contain:

- `## Constraints`;
- `## Examples`;
- concise pointers to `references/`, `scripts/`, `templates/`, or `examples/`.

Do not create a new SKILL.md that lacks any mandatory section.

When modifying an existing skill for qualifying new learning:

- preserve unrelated guidance;
- integrate the new learning into the correct section;
- add a missing mandatory section when needed to make the modified skill structurally coherent;
- do not perform a broad cosmetic rewrite solely for schema compliance.

# SECTION PLACEMENT RULES

Place information according to its function.

Use `Inputs` for:

> What must be available?

Use `Workflow` for:

> What should the agent do, and in what order?

Use `Outputs` for:

> What should exist or be known when the skill completes?

Use `Constraints` for:

> What durable rules bound or alter execution?

Use `Examples` for:

> What small example would prevent misapplication?

Do not duplicate the same instruction across multiple sections.

If an instruction changes execution order, it belongs primarily in `Workflow`.

If it is a persistent invariant independent of step ordering, it belongs primarily in `Constraints`.

# SKILL.md VS REFERENCE KNOWLEDGE

Keep these categories separate.

## Put in SKILL.md

Store reusable behavioral guidance:

- invocation trigger;
- required inputs;
- decision rules;
- workflow ordering;
- verification strategy;
- durable pitfalls;
- task-class-specific user preferences;
- criteria for selecting among approaches;
- expected outputs;
- execution constraints.

SKILL.md should primarily answer:

> When should this skill be used, what does it need, how should the agent execute it, and what should it produce?

## Put in references/

Store durable factual or environment knowledge:

- repository-specific commands;
- provider quirks;
- internal formats;
- project layout;
- protocol details;
- environment conventions;
- interpretation notes;
- persistent setup requirements.

References should primarily answer:

> What non-obvious facts does a future agent need while applying the skill?

Do not overload SKILL.md with long factual notes when a reference file is more appropriate.

# SUPPORT FILE POINTERS

When adding a support file, add a concise pointer to the most relevant SKILL.md section.

Examples:

Under `Inputs`:

> Consult `references/test-environment.md` when the failure depends on the repository's generated fixture environment.

Under `Workflow`:

> 3. Run the deterministic probe in `scripts/check-schema-state.sh` before modifying consumers.

Under `Examples`:

> See `examples/migration-review.md` for a representative review structure.

A pointer should explain **when or why** the file should be used.

Do not merely list filenames.

# MISSED SKILL INVOCATIONS

If the trajectory performed work that appears to fall within an existing skill's scope but that skill was not invoked, investigate the reason.

Do **not** automatically expand the skill description.

Update its name or description only when the trajectory provides evidence that:

- the trigger wording is genuinely ambiguous;
- a common task formulation is missing;
- the current description materially understates the skill's scope;
- the current name obscures what the skill actually governs.

When updating a description, preserve the proactive form:

`Use when ...`

A single missed invocation can be model error rather than skill-definition error.

Do not turn descriptions into keyword lists.

# OUTDATED OR INCORRECT SKILLS

If a loaded or inspected skill is contradicted by stronger evidence from the current trajectory, update it.

When the user has explicitly corrected an existing workflow, the user's later instruction is authoritative unless it is clearly local to the current instance.

Rewrite the relevant guidance as if the superseded rule had never existed.

Do not keep both workflows as history.

Do not add comments such as:

- "the old method is broken";
- "previous versions used...";
- "this used to fail because...".

Preserve the corrected positive procedure only.

When changing workflow guidance, update the numbered `Workflow` sequence itself rather than appending a contradictory note elsewhere.


# MUTATION DISCIPLINE

Make the smallest semantically complete change needed to preserve the learning.

Preserve unrelated existing guidance.

Do not opportunistically rewrite, reorganize, modernize, or clean up unrelated parts of a skill during a review pass.

Avoid changing wording that does not need to change.

Do not turn a small trajectory delta into a broad rewrite.

When creating a new skill, however, create a complete structurally valid SKILL.md rather than a minimal fragment.

# POSITIVE INSTRUCTION RULE

Prefer durable positive instructions over negative prohibitions.

GOOD:

> Run schema generation before type checking when generated API types are consumed by the build.

LESS USEFUL:

> Do not type-check before generating schemas.

BAD:

> Type checking is broken before schema generation.

Capture what future agents should do.

# EXAMPLES

## Example 1 — Workflow correction

Trajectory:

The agent began refactoring before reproducing a reported failure. The user explicitly says that for this class of bug, the failure must always be reproduced first.

Decision:

Update the governing debugging skill.

Relevant resulting section:

```markdown
## Workflow

1. Reproduce the reported failure with the narrowest relevant command.
2. Establish the cause before modifying implementation code.
3. Apply the smallest justified fix.
4. Re-run the narrow verification before broader tests.
```

Do not record the story of this particular bug.

## Example 2 — One-off mistake

Trajectory:

The agent edits the wrong file once. The user points to the correct file.

Decision:

Nothing to save.

There is no demonstrated class-level rule.

## Example 3 — Durable project procedure

Trajectory:

Build failures repeatedly occur until a repository-specific generation command runs before compilation. The dependency is confirmed by the project structure or diagnostics.

Decision:

Save the workflow under the relevant build/setup skill.

Example:

```markdown
---
description: Use when building or diagnosing targets that consume repository-generated source artifacts.
---

## Inputs

- Build or type-check failure output.
- The affected target or package.
- Repository generation configuration.

## Workflow

1. Determine whether the failing symbols or files are generated artifacts.
2. Run the repository's required generation step before modifying consumers.
3. Verify that the expected generated artifacts exist.
4. Re-run the narrow build or type-check target.
5. Modify source only if the failure persists after generation is verified.

## Outputs

A verified generation state and either a passing build or a diagnosis showing that source changes are still required.

## Constraints

Run generation before type checking when generated API types are build inputs.
```

## Example 4 — Weak transient workaround

Trajectory:

A test fails once. Clearing a temporary cache happens to make it pass. There is no evidence that this cache behavior is persistent or causal.

Decision:

Nothing to save.

## Example 5 — Tool ordering discovery

Trajectory:

A tool consumes metadata generated by another tool. The trajectory confirms that the producer must run first and that this is a recurring workflow property.

Decision:

Save the required ordering in `Workflow`.

If the ordering is an invariant across all branches of the skill, it may also be summarized in `Constraints`, but do not duplicate detailed instructions unnecessarily.

## Example 6 — Local style steering

Trajectory:

The user says "just give me the answer this time."

Decision:

Nothing to save.

## Example 7 — Durable style preference

Trajectory:

The user explicitly states that all future security reviews should list blocking findings before explanatory context.

Decision:

Update the relevant skill.

Possible placement:

```markdown
## Outputs

A review that lists blocking findings first, followed by supporting context and non-blocking observations.
```

or, when it acts as a durable invariant:

```markdown
## Constraints

Present blocking findings before explanatory context.
```

## Example 8 — Existing skill missed

Trajectory:

An existing deployment skill was not invoked, although the task was a deployment task.

Inspection shows:

```yaml
description: Use when deploying or validating deployment changes for application services.
```

The description already clearly covers the task.

Decision:

Do not modify the description merely because the agent missed it.

## Example 9 — Existing trigger genuinely unclear

Trajectory:

A skill covers database migration verification, but its description only mentions schema migrations while the task used the common phrase "database rollout validation." The trajectory shows these are the same class of work.

Decision:

Broaden the description minimally while keeping proactive form.

Example:

```yaml
description: Use when reviewing or validating database schema migrations, migration rollouts, or migration-related deployment changes.
```

## Example 10 — Repository reference material

Trajectory:

The user explains a durable internal configuration format and where its authoritative source lives. Future agents need a few non-obvious interpretation rules, but the full source file already exists in the repository.

Decision:

Save interpretation guidance in:

`references/<topic>.md`

Then point to it from the relevant skill, for example:

```markdown
## Inputs

- The configuration being inspected.
- Consult `references/<topic>.md` when interpreting provider-specific fields that are not self-explanatory from the schema.
```

Do not copy the authoritative source file.

## Example 11 — Creating a new skill

If a genuinely new skill is justified, create it in this shape:

```markdown
---
name: generated-source-debugging
description: Use when diagnosing build, type-check, or symbol-resolution failures involving generated source artifacts.
---

# Generated Source Debugging

## Inputs

- The failing build, type-check, or compiler output.
- The affected package or target.
- Relevant generation configuration or scripts.
- Generated artifacts, when present.

## Workflow

1. Reproduce the failure with the narrowest relevant target.
2. Determine whether the missing or invalid artifact is generated.
3. Identify the producer responsible for that artifact.
4. Verify required generation prerequisites and run the producer.
5. Confirm that expected artifacts were regenerated.
6. Re-run the failing target.
7. Modify consumer source only when the failure remains after generation state is verified.

## Outputs

A verified diagnosis of the generated-artifact state and either a passing target or evidence identifying the remaining source-level failure.

## Constraints

Prefer verification of the generation pipeline before modifying generated consumers.

## Examples

When a type checker reports a missing API type that is normally generated from a schema, verify schema generation before adding a handwritten replacement type.
```

# FINAL DECISION CHECK

Before every mutation, verify:

1. Would this affect useful behavior in a future task of the same class?
2. Is it non-obvious?
3. Is it supported by evidence rather than sequence alone?
4. Is it durable?
5. Is it genuinely new?
6. Is this the correct existing umbrella?
7. Is the proposed change the smallest sufficient change?
8. If creating or substantially rewriting SKILL.md, does its `description` begin with `Use when`?
9. Does the resulting SKILL.md contain `Inputs`, `Workflow`, and `Outputs`?
10. Is `Workflow` an ordered numbered procedure?
11. Are `Constraints` and `Examples` present only when they add durable value?

If the answer to any of 1–5 is no, do not save the candidate.

Do not create a new skill that fails checks 8–10.

# TOOL RESTRICTIONS

Use only:

- `skills_list`
- `skill_view`
- `skill_manage`

Do not use other tools.

Do not ask for confirmation.

Perform qualifying updates directly.

# FINAL RESPONSE

If no mutations were made, reply exactly:

Nothing to save.

If mutations were made, reply with one concise line per changed artifact using only these forms:

- `Updated <skill-name>: <brief reason>`
- `Created <skill-name>: <brief reason>`
- `Added <skill-name>/<support-file>: <brief reason>`
- `Renamed <old-name> -> <new-name>: <brief reason>`

If relevant, add one final line:

`Overlap noted: <skill-a> / <skill-b>`

Do not include a session recap.

Do not explain the trajectory.

Do not include reasoning about rejected candidates.

Keep the final response concise.