# AI Coach Refactoring Intent

## Purpose

This refactoring should produce a smaller, clearer, and more direct AI Coach
without weakening its central value: dependable, data-driven coaching that
adapts to each user over time.

The work should favor deletion and simplification over new abstraction. It is
not a rewrite unless concrete evidence shows that incremental simplification
would be riskier or more complex than replacement.

## Core Product Promise

The AI Coach should enable users to interact reliably with their personal data
and training through one continuous coaching relationship. It should use the
user's data, established training formats, and recognized training
methodologies to:

- answer questions about training data and make that data understandable;
- create and modify training sessions;
- provide useful workout guidance and support;
- accept feedback and incorporate it into later guidance;
- provide dependable, individualized recommendations;
- help users pursue their goals and training plans;
- track progress over time; and
- adapt guidance according to progress toward those goals.

The most important capability to preserve is the adaptive coaching loop:
personal data and goals inform recommendations, the user trains and provides
feedback, progress is evaluated, and subsequent guidance adapts.

## Desired Experience

The Coach should feel direct, lightweight, and useful. Querying data, creating
or changing workouts, giving feedback, and receiving guidance should feel like
parts of one coherent conversation rather than separate modes or bureaucratic
workflows.

Analysis, recommendations, and draft creation should not be slowed by
unnecessary confirmation, review, or revision stages. Explicit confirmation
should be reserved for consequential actions, such as replacing an accepted
workout or publishing or pushing a workout to an external service.

## Reliability

Reliability should come primarily from relevant personal data, sound training
methods, clear assumptions, and coherent decisions. It should not be simulated
through layers of defensive orchestration.

When information is missing or contradictory, the Coach should:

- identify material uncertainty;
- ask one focused question when the answer could materially change its advice;
  and
- otherwise proceed usefully while making important assumptions explicit.

The Coach should remain grounded and dependable without becoming so cautious
that reasonable training guidance is blocked.

## Safeguards And User Control

The product is intended for a small, trusted group. Safeguards should address
concrete risks rather than hypothetical misuse.

The following protections remain important:

- authentication and isolation of each user's data;
- protection against invalid or unconfirmed workouts reaching external
  services;
- explicit user control over consequential external actions; and
- honest communication of material uncertainty.

Broad refusals, redundant AI review and revision stages, excessive
confirmation gates, and restrictions that do not mitigate a concrete risk
should be removed or substantially simplified.

## Scope Decisions

An existing capability should survive only when it directly strengthens the
adaptive coaching loop or satisfies a demonstrated need among the actual
users. Existing complexity is not, by itself, a reason to preserve a feature.

Prefer deleting or simplifying:

- duplicate ways to achieve the same user outcome;
- speculative flexibility without a demonstrated use;
- rarely useful features outside the core coaching loop;
- defensive machinery aimed at implausible scenarios for the trusted user
  group;
- redundant validation, review, revision, and verification stages; and
- process or UI steps that make coaching slower without improving its outcome.

## Maintainability Direction

The coaching flow should have clear, understandable responsibilities and as
little orchestration as necessary. A developer changing one coaching
capability should normally need to understand that capability and the shared
coaching context, not unrelated parts of the system.

When behavior is wrong, it should be reasonably apparent:

- which user data and context informed the result;
- which coaching operation made the decision;
- which meaningful action was taken; and
- where a failure or incorrect decision originated.

This calls for a short, traceable decision path rather than deeply interwoven
gates, validation layers, and control loops. Reduced code size is useful
evidence of simplification, but understandable behavior and localized change
are the actual goals.

## Compatibility

Preserve durable user value:

- stored user data and training history;
- goals, plans, progress, and feedback;
- the essential adaptive coaching outcomes; and
- essential external integrations.

Do not preserve internal APIs, incidental conversational behavior, redundant
workflows, or existing UI steps solely for backward compatibility. These may
change when doing so creates a more direct and maintainable product.

## Rewrite Threshold

A full rewrite is justified only by concrete evidence that the existing
foundation prevents a clear, testable adaptive coaching flow and that
incremental simplification would be riskier or more complex than replacement.
Code size, untidiness, or architectural preference alone are not sufficient
reasons.

## Success Criteria

The refactoring is successful when:

- the data-driven adaptive coaching loop remains dependable and useful;
- the user experience is noticeably more direct and lightweight;
- unnecessary features, gates, and orchestration have been removed;
- consequential external actions remain under explicit user control;
- common changes are localized to clear responsibilities;
- decisions and failures can be traced without reconstructing a large control
  graph; and
- focused behavioral tests protect the core coaching outcomes rather than
  incidental implementation details.

## Out Of Scope

This intent does not require preserving every existing feature, internal
boundary, workflow, UI step, or incidental behavior. It does not call for a
general-purpose safety framework, speculative extensibility, or a full rewrite
without the evidence described above.
