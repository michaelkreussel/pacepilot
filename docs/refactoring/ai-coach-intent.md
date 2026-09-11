# Coaching And Training Intelligence Intent

## Purpose And Authority

Updated **11 September 2026** to reflect the training-first coaching upgrade. This document
describes the desired outcome, not shipped behavior. The filename remains stable for existing
agent instructions and links.

- [Current-state context](ai-coach-current-state.md) maps the inspected implementation.
- [Upgrade execution prompts](../plans/coaching-intelligence-upgrade.md) define two implementation
  packages. Their implementation is pending.

The application already works. Improve the coaching experience substantially while preserving the
dashboard, Garmin integration, authentication, historical data, workout builder and deployment.
The Runna benchmark concerns clarity and usefulness of coaching, not copying its implementation.

## Core Product Promise

Opening the Coach should answer: **What training makes sense today, and why?**

Recommendations should combine athlete state, recent training, goal, recovery and available time
to choose a stimulus and an existing workout archetype, individualize the session, and explain:

- why this workout and why today;
- why this intensity and amount of work;
- how it fits recent training and the athlete's goal;
- what changed after completed sessions or feedback; and
- which evidence or assumptions limit confidence.

Different athlete levels should produce materially different sessions, not merely different paces.
Available time is a ceiling for an automatic recommendation, not the amount of training to fill.

## Desired Experience

Make `/coach` training-first, with chat secondary:

- **Heute:** recommended, scheduled, completed or rest state, with structured workout steps.
- **Warum diese Einheit?:** a few dated personal reasons and explainable intensity guidance.
- **Diese Woche:** actual training and existing planned work, clearly labeled by state.
- **Anpassen:** shorter/easier choices and explanations of changes.
- **Coach fragen:** optional conversation for questions, feedback and explicit planning changes.

Users should not need to ask what to do today when the available data supports a recommendation.
The daily overview should work without an LLM call. Viewing it should not create drafts, change
accepted work or contact Garmin. Persist a chosen workout through the existing proposal workflow.

Retain useful chat capabilities and historical conversations. The continuity of coaching comes
from durable athlete data, plans, activities and feedback, not a requirement to make chat primary.

## Training Intelligence

Reuse the six existing workout formats, running baselines, planning inputs, feedback and recovery
analytics. Improve selection, dose and target personalization before expanding the taxonomy.

Use deterministic code for numerical consistency: duration, repetitions, work volume, recovery,
preparation, pace/HR bounds and structured validity. AI can interpret intent and explain or discuss
the grounded result; it cannot invent executable workout parameters.

Use recent appropriate performances or threshold evidence where reliable, with source-aware HR and
RPE/talk-test fallbacks. Race predictions and VO2max are supporting context, not precise prescriptions.
A single performance is not Critical Speed; unsuitable performance pairs must not appear robust.

Respect recent hard/long work, habitual frequency, interruptions and current volume. Progress only
when observed training supports it, avoid catch-up stacking, and preserve accepted plan intent.
Weekly/cycle generation and single-workout recommendations should share dose and target logic;
persisted sessions must match their previews.

Recovery modifies training rather than dictates it from one score. Distinguish corroborated adverse
evidence from missing health coverage. Missing metrics alone must not imply poor recovery or trigger
automatic reductions. Do not encode an ACWR safe zone or rigid universal intensity distribution.

## Reliability And User Control

Make uncertainty explicit without refusing ordinary useful guidance. Ask one focused question only
when a missing answer materially changes the recommendation; otherwise proceed with a stated
assumption. Never infer missed training solely from incomplete synchronization or workout linkage.

Preserve the existing distinction between an explicitly requested advisory draft and the workout
PacePilot automatically recommends. Sparse history need not hide a representable requested draft,
but automatic selection should choose appropriate easier work or rest when warranted.

Preserve user isolation, CSRF, immutable revisions and accepted-revision execution. Acceptance,
scheduling, replacement, publication and device push remain explicit existing actions. Model prose
is not authorization. Do not add redundant confirmation stages or AI review loops.

## Scope And Implementation Constraints

Preserve the existing architecture unless a change is necessary for this feature. Prefer modifying
and reusing existing code over introducing parallel systems. Do not introduce abstractions,
services, repositories, interfaces, agents, infrastructure or migrations unless they solve a
concrete problem identified in the implementation.

**Do not add libraries, databases, external services or runtime dependencies.** Use the existing
stack, database schema and workout models. Existing revision JSON fields are sufficient for the
planned context, parameters and explanation metadata. No schema migration is planned. If a concrete
blocker appears to require a dependency or schema change, explain it and ask before proceeding.

External datasets, Garmin FIT documentation, scientific papers and other platforms are development
references by default, not production dependencies. No new recommendation database, background
coaching job, vector store, model infrastructure or large onboarding questionnaire is needed.

Keep work focused on measurable coaching quality. Simplify code only where it directly enables
the feature; avoid unrelated cleanup or a broad rewrite. Complete each package end-to-end rather
than leaving scaffolding or production TODOs.

## Execution And Success Criteria

Use two meaningful vertical packages: individualized daily recommendations with a visible card
(Astra High), then consistent weekly intelligence and training-first UX (Astra Medium). The limited
Astra credit window favors understand -> implement -> test -> concise review, not additional phases.

Success means a user can understand and reasonably trust tomorrow's recommendation: its stimulus,
dose and intensity respond to actual training and evidence, alternatives are useful, and the
workout remains valid through persistence and Garmin compilation. Focused behavioral tests must
protect these outcomes and existing lifecycle/integration behavior. The dashboard remains intact.
