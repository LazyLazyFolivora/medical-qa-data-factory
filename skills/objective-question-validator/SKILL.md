---
name: objective-question-validator
description: Audit final biomedical questions for query-planner suitability, objective correctness, self-contained public scope, answer support and closure, semantic fidelity, and requested difficulty before release.
---

# Objective Question Validator

Validate whether a proposed question and answer can be published as an
objective medical QA item.

The intended consumer is query-planner. Check both whether the question is an
appropriate retrieval and evidence-integration task and whether its answer is
objectively supported under the public meaning. Neither condition substitutes
for the other. This skill is already loaded by the release-review runtime;
updating it changes review instructions, but does not add missing runtime inputs
or schema fields. See [references/runtime-integration.md](references/runtime-integration.md)
when integrating or diagnosing the staged review.

This is a release gate, not a writing skill. Do not improve a failing question
and then validate the improved version. Judge the submitted public question and
proposed answer exactly as supplied. A suggested replacement may be reported
separately, but it does not change the verdict.

## Core standard

A question passes only when a reader can determine what is being asked from the
public question alone and the proposed answer is a fact about the world that can
be independently verified.

Knowledge graphs, databases, search results, retrieved passages, generation
traces, and private prompts may help discover or verify the fact. They must not
silently define what the question means or which answers count.

Apply these invariants:

- The data source is evidence, not the question's world.
- Hidden generation context cannot repair an incomplete public question.
- Absence from a source is not evidence that a fact is false.
- One supporting record is not proof that an answer set is complete.
- The question defines the task; retrieval supplies answer evidence.
  Self-contained wording does not require putting the evidence into the question.
- A successful graph query is not proof of real-world correctness; factual
  correctness is not proof that the configured Planner can obtain the evidence.
- Uncertainty must block release; it must not be converted into a confident
  answer.
- Adding phrases such as "现有研究是否支持", "根据当前证据", or "在某知识图谱中"
  does not turn a source-dependent item into an objective fact question.

## Expected input

Use all available fields, but keep their roles separate:

- `question`: the exact public question shown to the answerer;
- `proposed_answer`: the answer intended for release;
- `answer_type`: optional declaration such as boolean, entity, category, value,
  date, or ordered list;
- `supporting_evidence`: hidden material offered to support the answer;
- `provenance`: optional source, snapshot, retrieval, graph, or generation data;
- `intended_claim`: optional internal statement of the fact the item is meant to
  test.
- `frozen_question_contract`: verified semantics to compare with the final text,
  available only after the isolated public audit;
- `planner_environment`: available tools, knowledge sources and access evidence,
  used to assess practical solvability, not to define truth;
- `design_requirements`: requested difficulty and retrieval/integration demands,
  when specified by the task.

For a full review, if the question or proposed answer is missing, return
`INDETERMINATE` and block release. An isolated public-question stage intentionally
does not receive answers or evidence: audit only its assigned scope, and never
treat a public-stage PASS as full release approval.
If independent verification material is unavailable or insufficient,
do not infer correctness from provenance metadata; return `INDETERMINATE`.

## Validation procedure

### 1. Audit the public question in isolation

First inspect only `question`. Do not look at hidden evidence yet.

When invoked as `public_question_audit`, perform only checks possible from that
text: task clarity, public context, source dependence, entity and answer-demand
clarity. Do not attempt answer normalization, evidence verification, contract
comparison, environment verification or target-difficulty matching in this stage.
Those checks belong to the subsequent full-context stage.

Recover, if possible:

- the subject;
- the predicate or property being asked about;
- the expected answer type;
- every condition that changes the answer;
- whether one answer or multiple answers are requested;
- any required time, population, geography, definition, comparison set, unit,
  or threshold.

Reject the item if a reasonable reader must know an unstated database, graph,
corpus, retrieval result, candidate set, time point, population, or internal
schema to interpret it.

Do not use hidden evidence to fill a missing condition. Record the missing
condition as a failure.

### 2. Reject source-relative and meta-evidence questions

Reject questions whose main predicate is about what a source contains, reports,
supports, suggests, or has evidence for instead of directly asking about the
underlying fact.

Typical failures include:

- "某知识图谱中……是什么？"
- "数据库记录了哪些……？"
- "现有研究是否支持……？"
- "根据检索到的文献……？"
- "当前证据范围内……？"
- "哪篇论文提到……？" when the intended task is objective biomedical QA rather
  than bibliographic retrieval.

An explicitly named source does not automatically make the item suitable for
this objective-fact dataset. Questions about the contents of a source are a
different task class and fail this skill.

### 3. Normalize the claim without strengthening it

Represent the question and proposed answer as the smallest factual claim needed
for the answer to be correct. Preserve modality and relation strength.

Distinguish at least:

- identity or classification;
- measured value or date;
- anatomical, molecular, or taxonomic relation;
- association;
- risk difference;
- causation;
- mechanism;
- treatment effect or recommendation.

Reject if the question or answer upgrades an association into causation,
mechanism, prevention, diagnosis, prognosis, or treatment benefit.

Do not reinterpret a stronger question as a weaker claim merely because the
weaker claim has evidence.

### 4. Check objective decidability

Determine whether the claim has a stable truth condition outside the generation
system.

The item must have:

- a clear criterion for deciding whether an answer is correct;
- terminology with a resolvable biomedical meaning;
- a truth value that is not created by one provider's coverage policy;
- enough public scope to distinguish the proposed answer from plausible
  alternatives;
- an answer that remains the same when the discovery source is replaced by
  another reliable source using the same public meaning.

Reject value judgments, open-ended opinions, underspecified comparisons, and
claims established to lack a stable answer. If material scientific disagreement
cannot be resolved, or the claim may be
true but the available material cannot establish a stable answer, return
`INDETERMINATE`, not `PASS`.

### 5. Verify the answer independently

Only after the public-question audit, inspect the evidence and provenance.

Verify the normalized claim against evidence independent of the source or
record that generated the candidate. If independence cannot be established,
record the gap instead of claiming independent verification. Prefer direct,
authoritative, and current sources appropriate to the fact type. For research
claims, check whether the evidence directly studies the stated entities,
population, relation, and direction.

Look for disconfirming evidence and incompatible definitions, not only support.
Do not treat repeated copies of the same upstream record as independent
confirmation.

The current release reviewer has no retrieval tools. Inspect the supplied
materials and actual search records; request targeted follow-up through the host
when they are inadequate. Do not describe a new search as performed unless a
permitted tool was actually used. A previous VERIFIED status is not evidence that
all necessary checks were completed.

The following issues prevent a full PASS; distinguish established defects from
missing evidence rather than automatically assigning REJECT to every case:

- it is not directly entailed by the evidence;
- the evidence supports only a weaker claim;
- reliable evidence contradicts it;
- entity identity or relation direction is mismatched;
- the answer depends on a source-specific ID or modeling convention;
- the evidence is too old for a time-sensitive question whose time boundary is
  not public.

### 6. Check answer uniqueness and closure

For single-answer questions, inspect the available evidence and records of
searches for other answers satisfying the same public conditions. If these
checks are missing and cannot be performed with permitted tools, report the gap.
Reject if multiple answers qualify and the question neither allows
multiple answers nor distinguishes one of them.

For questions asking for all, only, primary, most common, first, highest, or
lowest, require evidence that justifies the corresponding completeness,
uniqueness, ordering, or comparison. A sampled evidence bundle or one graph
neighborhood cannot establish closure.

A finite option set written in the public question may provide a valid closed
comparison set. A hidden candidate list may not.

For an explicitly example-seeking question, other valid examples do not invalidate
the proposed example. Check every supplied answer and do not treat the stored
list as exhaustive. Distinguish equivalent names from genuinely different answers.

### 7. Check final wording against verified semantics

After the isolated public audit is recorded, compare the actual question with
the frozen intent, scope, answer type, cardinality, conditions and relation
strength. Do not trust a writer's copied semantic_readback as proof of equivalence.
Reject omitted conditions, invented premises, changed comparison sets or stronger
relations. If the contract itself is defective, return to design or verification;
do not repair it during the audit.

Check answer hints in context. A direct-answer question must not state its answer
through a parenthesis, synonym or category label. Choice questions may contain
the correct option and boolean questions may contain the proposition being judged;
they must not signal the result. A matching substring alone does not prove leakage.
If an answer explanation is supplied, reject unsupported additions or contradictions;
if it is absent, do not claim to have audited it.

### 8. Check query-planner suitability and difficulty

When independent sources fail to link the target to the asked property, return INSUFFICIENT_VALIDATION_EVIDENCE with evidence_need describing the missing subject, relation and public conditions. Keep it neutral: omit expected values and competing guesses so another search can test the same draft independently.

In the answer-evidence audit, report assessed_difficulty (easy, medium or hard) from the shortest sound solution. For a medium-only request, an objectively valid easy question passes difficulty review as supplementary output. Do not reject or rewrite it solely for DIFFICULTY_MISMATCH. Only actual medium questions count toward the requested number. All other release criteria still apply. This exception takes precedence over the general mismatch guidance and examples below.

First judge the public query: can the reader identify what to retrieve and the
scope of a satisfactory answer? Prefer natural factual queries about attributes,
relations, mechanisms or explicit conditions. Open advice, vague requests for
"everything" and instructions exposing internal graph paths are not substitutes
for a well-defined biomedical task.

Then assess whether the available Planner environment can retrieve the necessary
evidence and support the required integration. Do not assume that materials seen
by the generator are available to the Planner. Known tool or source incompatibility
blocks release for this consumer without proving the fact false. Missing environment
information or unresolved access is INDETERMINATE for this check, not automatic PASS.

When a difficulty is requested, judge the actual task:

- Easy: a clear target and direct evidence can answer the question.
- Medium: meaningful relation linking, condition matching or evidence combination.
- Hard: substantive multi-step retrieval, intersecting conditions or competing
  candidate comparison with sufficient grounds to decide completion.

Do not grade by hop count, wording length or rare terminology alone. Accept valid
short routes and reassess difficulty instead of forcing unnecessary tool calls.
Distinguish legitimate public conditions from hints that reveal the intermediate
results the design intended the Planner to discover. A demonstrated mismatch
blocks the requested difficulty assignment; uncertain calibration is reported as
unresolved. If no difficulty was requested, do not invent a difficulty gate.

### 9. Produce a fail-closed verdict

Return `PASS` only if every hard gate passes. Use `REJECT` when a concrete defect
is established. Use `INDETERMINATE` when correctness cannot be established from
the available independent evidence. Both `REJECT` and `INDETERMINATE` block
release.

Do not average failures into a score. Do not let fluent wording, source count,
or apparent medical plausibility compensate for a failed hard gate.

Record the affected stage and the concrete corrective action when the output
contract allows findings: wording-only defects go to realization; invalid scope,
cardinality or difficulty design goes to question design; missing support,
independence or closure goes to retrieval and verification. Known environment
incompatibility goes to task/environment selection. A revision is a new item for
review and does not retroactively change this verdict.

## Reason codes

Use one primary reason and any number of secondary reasons:

- `MISSING_PUBLIC_CONTEXT`: a condition needed to interpret or answer the
  question is not in the public question.
- `SOURCE_RELATIVE`: correctness is defined by a knowledge graph, database,
  corpus, search result, or provider rather than the world.
- `META_EVIDENCE_QUESTION`: the question asks whether evidence or research
  supports, reports, or mentions a claim.
- `NOT_OBJECTIVELY_DECIDABLE`: no stable, impersonal correctness criterion is
  available.
- `UNSTABLE_OR_CONTESTED`: material scientific disagreement or temporal
  instability prevents a stable answer.
- `ENTITY_AMBIGUOUS`: an entity cannot be identified from the public wording.
- `TEMPORAL_SCOPE_MISSING`: the answer changes over time and no necessary date
  or period is public.
- `ANSWER_TYPE_MISMATCH`: the proposed answer does not match what the question
  asks for.
- `ANSWER_NOT_UNIQUE`: more than one answer satisfies a single-answer question.
- `INCOMPLETE_CLOSED_SET`: completeness, uniqueness, or ranking is claimed
  without a justified closed comparison set.
- `UNSUPPORTED_ANSWER`: independent evidence does not directly establish the
  proposed answer.
- `CONFLICTING_EVIDENCE`: reliable evidence materially contradicts the answer.
- `CAUSAL_OVERCLAIM`: association or observation is expressed as causation,
  mechanism, prevention, diagnosis, prognosis, or treatment effect.
- `DIRECTION_MISMATCH`: the relation direction in the question, answer, and
  evidence is inconsistent.
- `INSUFFICIENT_VALIDATION_EVIDENCE`: the validator lacks enough independent
  material to establish correctness.
- `SEMANTIC_DRIFT`: the final question changes the frozen task semantics.
- `ANSWER_LEAKAGE`: wording reveals the answer or improperly resolves the task.
- `UNSUPPORTED_EXPLANATION`: a supplied explanation adds unsupported claims.
- `PLANNER_TASK_MISMATCH`: the question lacks a defined factual retrieval task.
- `PLANNER_ENVIRONMENT_MISMATCH`: known tool/source limits prevent the intended
  Planner from obtaining the necessary evidence.
- `PLANNER_SOLVABILITY_UNESTABLISHED`: available information cannot establish
  practical solvability in the intended Planner environment.
- `DIFFICULTY_MISMATCH`: the actual task does not meet a requested difficulty.
- `DIFFICULTY_UNESTABLISHED`: evidence is insufficient to assess a requested difficulty.

## Output contract

The caller's stage-specific structured response schema takes precedence over the
standalone example below. Never add undeclared fields or return the full report
when asked for a public-only or answer-evidence stage. Use the allowed verdict
and reason fields to report blockers within the assigned scope. Only the host
merges stages and authorizes release in the application.

For a standalone full review, return one JSON object and no prose outside it:

```json
{
  "verdict": "PASS | REJECT | INDETERMINATE",
  "release_allowed": false,
  "primary_reason": "PASS | REASON_CODE",
  "secondary_reasons": [],
  "public_question_audit": {
    "self_contained": false,
    "source_independent": false,
    "asks_underlying_fact": false,
    "objective_truth_condition": false,
    "answer_cardinality_clear": false
  },
  "normalized_claim": {
    "subject": "",
    "predicate": "",
    "object_or_answer": "",
    "qualifiers": [],
    "claim_strength": ""
  },
  "answer_audit": {
    "independently_supported": false,
    "unique_or_closed": false,
    "contradiction_checked": false,
    "relation_strength_preserved": false
  },
  "planner_audit": {
    "retrieval_task_clear": false,
    "completion_scope_clear": false,
    "environment_solvability": "PASS | REJECT | INDETERMINATE",
    "difficulty_alignment": "PASS | REJECT | INDETERMINATE | NOT_REQUESTED"
  },
  "blocking_findings": [],
  "evidence_used": [],
  "suggested_replacement": null
}
```

For `PASS`, set `release_allowed` to `true`, set `primary_reason` to `PASS`, and
leave `blocking_findings` empty. Every other verdict must set
`release_allowed` to `false`.

Evidence entries should identify the independently checked source and the exact
claim it supports or contradicts. Do not report a source as checked when only
its citation metadata or a generated summary was available.

`suggested_replacement` is optional. Use it only when a direct, objective fact
question can be written without inventing facts. Never change the verdict of
the submitted item because a replacement is available.

## Calibration examples

Reject:

- Question: "在 PrimeKG 中，阿司匹林与哪种疾病有关？"
  Reason: `SOURCE_RELATIVE`.
- Question: "现有研究是否支持二氯甲烷暴露与某疾病有关？"
  Reason: `META_EVIDENCE_QUESTION`; often also `UNSTABLE_OR_CONTESTED` or
  `MISSING_PUBLIC_CONTEXT`.
- Question: "阿司匹林与哪种疾病有关？"
  Reason: `ANSWER_NOT_UNIQUE` and `INCOMPLETE_CLOSED_SET` when a single disease
  is expected.
- Question: "某暴露会导致某疾病吗？" when evidence establishes association
  only.
  Reason: `CAUSAL_OVERCLAIM`.
- Question: "哪种疾病与该化合物有关？" when "该化合物" depends on hidden
  generation context.
  Reason: `ENTITY_AMBIGUOUS` and `MISSING_PUBLIC_CONTEXT`.

Potentially pass after independent verification:

- "BRCA1 基因位于人类哪条染色体上？"
- "胰岛素由胰岛中的哪类细胞分泌？"
- "在以下四种物质中，哪一种属于有机氯化合物：……？"

These examples illustrate question form only. Do not pass them from wording
alone; the proposed answer still requires independent factual verification.

Additional calibration:

- Correct facts but unspecified Planner tools/sources: factual checks may pass,
  but full consumer-specific release remains INDETERMINATE until solvability is
  established. A public-only audit can still pass its narrower scope.
- A requested hard question directly reveals its bridge entity and leaves only
  a simple lookup: report DIFFICULTY_MISMATCH when the actual task supports that
  conclusion; do not reject a correct answer as factually false.
- An example-seeking question with several valid answers: do not reject merely
  because alternatives exist; ensure the proposed example satisfies the question.
- A question that omits a required species while the hidden contract specifies
  it: reject the submitted text; the contract cannot supply missing public context.
