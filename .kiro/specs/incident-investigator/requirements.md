# Requirements Document

## Introduction

The Incident Investigator is a read-only, multi-agent system that performs post-incident analysis of a fixed, synthetic data snapshot. Given a short incident report, six cooperating agents (Orchestrator, Observability, Dependency, Reasoning, Critic, Reporting) query simulated logs, metrics, deployment and change history, service dependencies and historical incidents through typed tools. The system identifies the probable root cause, or honestly reports an inconclusive outcome, and recommends remediation tied to ranked, evidence-cited hypotheses.

The project also delivers a synthetic dataset generator with 12 scenarios and an answer key that only the evaluator reads, a single-agent ReAct baseline that uses the same model, tools, budgets and data, an evaluation CLI that compares both systems, a pytest suite that runs on a fake LLM, and the written deliverables required by the assignment.

The implementation uses plain Python 3.11+, asyncio and Pydantic v2 with no agent framework. The 19 assumptions in `ASSUMPTIONS.md` are binding on every requirement below.

Out of scope: live data streaming, alerting, executing remediation, a graphical frontend, and LLM-as-judge scoring of root-cause accuracy.

## Glossary

- **Investigator**: The multi-agent incident investigation system as a whole: the Orchestrator plus the five other Agents.
- **Agent**: Any one of the Orchestrator, Observability_Agent, Dependency_Agent, Reasoning_Agent, Critic_Agent or Reporting_Agent.
- **Orchestrator**: The Agent that plans the Task_Graph, dispatches Tasks, runs the Critic feedback loop, enforces Budgets and decides termination and Verdict.
- **Observability_Agent**: The Specialist_Agent that investigates logs and metrics through query_logs and query_metrics.
- **Dependency_Agent**: The Specialist_Agent that investigates deployments, configuration changes, feature-flag changes, service dependencies and historical incidents through get_deployments, get_dependencies and search_incidents.
- **Specialist_Agent**: The Observability_Agent or the Dependency_Agent; the only Agents that make Tool_Requests in the Investigator.
- **Reasoning_Agent**: The Agent that develops, compares, ranks and revises competing Hypotheses from Blackboard contents.
- **Critic_Agent**: The Agent that challenges the current Hypotheses and either accepts the top-ranked Hypothesis or files Evidence_Requests.
- **Reporting_Agent**: The Agent that produces the Investigation_Report and Recommended_Actions from Blackboard contents.
- **Baseline_Agent**: The single-agent investigator that runs one ReAct loop (reason, act with one Tool, observe) and is used for comparison.
- **System_Under_Test**: Either the Investigator or the Baseline_Agent.
- **Incident_Report**: The investigation input: title, symptoms, affected service, start time and severity. Its claims to verify are one per symptom, one for the affected service and one for the start time; title and severity are not claims.
- **Severity**: A value from the closed severity vocabulary defined in the shared models.
- **Incident_Snapshot**: The fixed, generated data set for one incident (logs, metrics, change events, dependency map, past incidents and failure profile) that Tools read.
- **Snapshot_Root**: The configured directory that contains all Incident_Snapshots and nothing else.
- **Failure_Profile**: The per-Incident_Snapshot file that declares forced Tool failures; the Failure_Profile contains no root-cause information.
- **Tool**: One of query_logs, query_metrics, get_deployments, get_dependencies or search_incidents.
- **Tool_Layer**: The component that implements the Tools, the Failure_Injector, the Retry_Policy and the Circuit_Breaker, and records Evidence_Items.
- **Tool_Request**: One Tool invocation that a Specialist_Agent or the Baseline_Agent asks the Tool_Layer to perform. The Tool_Layer runs an admitted Tool_Request as one or more Tool_Calls (the first attempt plus Retry_Policy retries), or rejects it without a Tool_Call when its input fails validation, the Circuit_Breaker is open or the Tool_Call Budget is exhausted.
- **Tool_Call**: One invocation attempt of a Tool admitted by the Tool_Layer; each retry attempt and each Trial_Call is one separate Tool_Call. Rejected Tool_Requests are not Tool_Calls.
- **Tool_Result**: The typed output of one Tool_Call or of one rejected Tool_Request, containing records, an error (code and message, or null), a partial flag, a stale flag with an as-of time, and a truncated flag.
- **Failure_Injector**: The component that injects timeouts, partial results and stale data deterministically from a Seed.
- **Retry_Policy**: The bounded retry rule for failed Tool_Calls using exponential backoff with jitter.
- **Circuit_Breaker**: The per-Tool, per-Run component that rejects Tool_Requests for a Tool for a configured cooldown once the Consecutive_Failure_Count of that Tool reaches a configured threshold.
- **Consecutive_Failure_Count**: For one Tool in one Run, the number of Tool_Calls of that Tool, including retry attempts and Trial_Calls, that returned error code `timeout` since the most recent Tool_Call of that Tool that returned a null error code, taken in the order the Tool_Calls return.
- **Trial_Call**: The single Tool_Call that an open Circuit_Breaker admits for its Tool after the cooldown elapses, to test whether the Tool has recovered.
- **Blackboard**: The shared investigation state of one Run: the Evidence ledger, Hypotheses, Tasks, Evidence_Requests, Data_Gaps, Incident_Report claim statuses and Budget usage.
- **Run_Ledger**: The Evidence ledger of one Run: the Blackboard Evidence ledger for an Investigator Run, or the Baseline_Agent ledger for a Baseline_Agent Run.
- **Evidence_Item**: One Evidence ledger entry: Evidence_ID, Tool, query, observation (the Observation_Summary of the Tool_Result), time range, reliability (complete, partial or stale) and source record IDs. The full records of the Tool_Result stay retrievable by Evidence_ID and by source record ID.
- **Observation_Summary**: The compact, deterministic summary of one Tool_Result that the Tool_Layer stores as the observation of an Evidence_Item: the record count, the time span of the records, the top distinct messages or values (at most the configured Observation_Summary size), and key attributes (service, level, metric name, change type, cohort attribute, as applicable); it never contains full records.
- **Evidence_ID**: The unique identifier assigned to an Evidence_Item when the Evidence_Item is appended to the Run_Ledger.
- **Historical_Evidence**: An Evidence_Item returned by search_incidents; Historical_Evidence describes a past incident, not the current incident.
- **Hypothesis**: A root-cause candidate: Hypothesis ID, Component, Cause_Type, statement, supporting Evidence_IDs, contradicting Evidence_IDs, Confidence, rank, revision number and optional contributing factors.
- **Component**: A service or infrastructure resource named in the dependency map of the Incident_Snapshot.
- **Cause_Type**: A value from the closed cause-type vocabulary defined in the shared models.
- **Root_Cause_Label**: The pair (Component, Cause_Type).
- **Confidence**: A number from 0.0 to 1.0 inclusive.
- **Claim**: A single statement in an Agent output or in a Baseline_Agent output.
- **Fact**: A Claim that cites at least one Evidence_ID present in the Run_Ledger.
- **Assumption**: A Claim that cites no Evidence_ID present in the Run_Ledger.
- **Citation_Validator**: The deterministic component that checks cited Evidence_IDs and Hypothesis IDs and labels each Claim as a Fact or an Assumption.
- **Data_Gap**: A record of evidence that the System_Under_Test could not obtain: source (Tool or Agent), query or Task, reason and affected time range.
- **Confidence_Policy**: The deterministic component that adjusts Hypothesis Confidence for recorded Data_Gaps.
- **Evidence_Request**: A structured request from the Critic_Agent: target Specialist_Agent, Hypothesis ID, concern category, question, suggested Tool, suggested Tool parameters and rationale.
- **Task**: A unit of work assigned to one Agent, with a list of Tasks the Task depends on and a state (pending, running, completed, failed or cancelled).
- **Ready_Task**: A Task in the pending state whose every dependency is in the completed, failed or cancelled state.
- **Agent_Failure**: The outcome of one Agent Task attempt (or one Baseline_Agent step) in which the Agent raises an unhandled exception, the attempt exceeds the configured per-Task timeout measured through the Injectable_Clock from the start of that attempt, or the Structured_Output_Validator rejects the repaired LLM output. An error Tool_Result returned to a Specialist_Agent is not an Agent_Failure, and neither is the Orchestrator cancelling a Task.
- **Task_Graph**: The directed acyclic graph of Tasks for one Run.
- **Default_Task_Graph**: The fixed Task_Graph the Orchestrator uses when planning output is unusable.
- **Critic_Round**: One Critic_Agent review of the current Hypotheses. Critic_Rounds are numbered from 1 in dispatch order within a Run, and a retried Critic_Agent Task stays in the same Critic_Round.
- **Budget**: The per-Run limits: maximum Critic_Rounds (at most 3), maximum Tool_Calls, maximum LLM tokens and a wall-clock timeout.
- **Token_Usage**: The running sum of the input and output token counts, taken from provider usage data, of every LLM call in one Run, including repair prompts.
- **Verdict**: The outcome of a Run, either `conclusive` or `inconclusive`.
- **Termination_Reason**: The recorded cause of loop termination. Investigator values: critic_accepted, max_critic_rounds, duplicate_requests_only, tool_call_budget_exhausted, token_budget_exhausted, wall_clock_timeout, critic_failed, no_hypotheses. Baseline_Agent values: final_answer, baseline_failed, tool_call_budget_exhausted, token_budget_exhausted, wall_clock_timeout.
- **Investigation_Report**: The final output of a Run, serialized as JSON and rendered as Markdown.
- **JSON_Report**: The UTF-8 JSON file serialized from one Investigation_Report model.
- **Markdown_Report**: The UTF-8 Markdown file rendered by the Markdown_Renderer from one Investigation_Report model.
- **Markdown_Renderer**: The deterministic component that renders a Markdown_Report from an Investigation_Report model without LLM calls.
- **Recommended_Action**: A remediation recommendation tied to one Hypothesis ID.
- **Deterministic_Report_Generator**: The component that builds an Investigation_Report without LLM calls from the Blackboard of an Investigator Run or the Run_Ledger of a Baseline_Agent Run.
- **LLM_Client**: The provider-agnostic interface through which every Agent and the Baseline_Agent call a language model.
- **Prompt_Template**: The fixed instruction text of an Agent or Baseline_Agent prompt, excluding content inserted at Run time from the Incident_Report, the Blackboard or Tool_Results.
- **Provider_Adapter**: An LLM_Client implementation for one provider (Anthropic or OpenAI).
- **Fake_LLM**: The deterministic, scripted LLM_Client implementation used by automated tests and offline runs.
- **Test_Suite**: The project's automated pytest tests, run offline with the Fake_LLM.
- **Structured_Output_Validator**: The component that validates LLM output against a Pydantic model and performs one repair retry.
- **Scenario_Spec**: The parametric description of one incident scenario that the Data_Generator consumes.
- **Scenario_Name**: The unique identifier of one Scenario_Spec within the Scenario_Catalogue.
- **Fault_Template**: A reusable, parameterized Data_Generator template that produces the fault records of one fault kind from Scenario_Spec values and a Seed; a Scenario_Spec selects one Fault_Template by name.
- **Trap**: The optional Scenario_Spec section that declares red herrings: coincidental, non-adversarial misleading records such as an unrelated deploy near onset, disagreeing monitors or a look-alike past incident, plus the decoy Cause_Type they suggest.
- **Symptom_Onset**: The time of the first symptom record that the scenario fault generates in an Incident_Snapshot, set by the Scenario_Spec as an offset from the telemetry window start.
- **Scenario_Catalogue**: The set of Scenario_Specs shipped with the project.
- **Data_Generator**: The component that turns a Scenario_Spec and a Seed into an Incident_Snapshot, an Incident_Report and an Answer_Key.
- **Answer_Key**: The expected outcome of one incident: a Root_Cause_Label or an insufficient-evidence marker, contributing factors, Key_Evidence record IDs, expected Data_Gaps and a canary token.
- **Answer_Key_Root**: The configured directory that contains Answer_Keys, located outside the Snapshot_Root.
- **Key_Evidence**: The Incident_Snapshot record IDs that the Answer_Key marks as essential for identifying the root cause.
- **Support_Check**: The deterministic rule set, defined in the design, that decides whether a cited Evidence_Item supports the Claim citing the Evidence_Item.
- **Evaluator**: The only component outside the Test_Suite that reads Answer_Keys; the Evaluator runs both Systems_Under_Test and scores Runs.
- **Run**: One execution of one System_Under_Test on one Incident_Snapshot with one Seed.
- **Run_Trace**: The per-Run JSON event log of Task transitions, Tool_Calls, LLM calls, Critic decisions, Budget usage and Termination_Reason.
- **Seed**: An integer that determines all generated data, injected failures and retry jitter.
- **Injectable_Clock**: The replaceable time source through which components read the current time and sleep; tests substitute a controllable clock that advances without real waiting.
- **Spread**: The sample standard deviation of a metric across the Runs of one System_Under_Test on one scenario; 0.0 for a single Run.
- **Results_Table**: The Evaluator output table of accuracy, grounding, Tool_Call count, latency and token metrics per System_Under_Test.
- **CLI**: The command-line interface with the `generate`, `investigate` and `eval` subcommands.
- **Repository**: The submitted Git repository that contains the project source, the tests and the deliverables.
- **README**: The `README.md` file at the repository root.
- **Packaging_File**: The single Python project file at the repository root (`pyproject.toml`) that declares the project's runtime and test dependencies.
- **Coding_Assistant_Transcript**: The transcript of the coding assistant session included in the submission.

## Requirements

### Requirement 1: Incident Report Input

**User Story:** As an SRE, I want to submit a short incident report, so that the Investigator knows which incident to analyse and which claims to verify.

#### Acceptance Criteria

1. WHEN the CLI loads the Incident_Report for a Run, THE CLI SHALL parse the Incident_Report into a validated Incident_Report model that requires a non-blank title of at most 200 characters, between 1 and 10 symptoms that are each non-blank and at most 500 characters, a non-blank affected service name of at most 100 characters, a start time given as an ISO 8601 timestamp with an explicit UTC offset, and a severity from the closed severity vocabulary defined in the shared models.
2. IF the Incident_Report is missing a required field or contains a value outside the constraints of the validated Incident_Report model, THEN THE CLI SHALL exit with a non-zero status and an error message naming each missing or invalid field, before any Tool_Call or LLM call is made and without writing an Investigation_Report.
3. IF the Incident_Report cannot be read or is not syntactically well-formed, THEN THE CLI SHALL exit with a non-zero status and an error message indicating that the Incident_Report could not be read or parsed, before any Tool_Call or LLM call is made and without writing an Investigation_Report.
4. WHEN a Run starts, THE Orchestrator SHALL record in the Blackboard, before dispatching the first Task, one Incident_Report claim per symptom, one Incident_Report claim for the affected service and one Incident_Report claim for the start time, each with a unique claim ID, the claim text, status `unverified`, no cited Evidence_IDs and the Assumption label.
5. WHEN the Reasoning_Agent completes a Task, THE Reasoning_Agent SHALL output, for every Incident_Report claim in the Blackboard, one status from `unverified`, `verified` or `contradicted`, the corroborating Evidence_IDs and the contradicting Evidence_IDs.
6. IF a Reasoning_Agent output sets an Incident_Report claim to `verified` and cites no corroborating Evidence_ID that is present in the Blackboard and is not Historical_Evidence, THEN THE Citation_Validator SHALL keep the claim status held before that output and record a citation violation in the Run_Trace.
7. IF a Reasoning_Agent output sets an Incident_Report claim to `contradicted` and cites no contradicting Evidence_ID that is present in the Blackboard and is not Historical_Evidence, THEN THE Citation_Validator SHALL keep the claim status held before that output and record a citation violation in the Run_Trace.
8. THE Investigation_Report SHALL list every Incident_Report claim, including claims with status `unverified`, with the claim ID, the claim text, the claim status held in the Blackboard when the Investigation_Report is built, the corroborating Evidence_IDs and the contradicting Evidence_IDs.

### Requirement 2: Read-Only Analysis of a Fixed Snapshot

**User Story:** As an SRE, I want the investigation to be read-only and reproducible, so that running the Investigator carries no operational risk.

#### Acceptance Criteria

1. THE Tool_Layer SHALL expose to Agents and to the Baseline_Agent exactly the five Tools query_logs, query_metrics, get_deployments, get_dependencies and search_incidents, and no other operation.
2. WHEN a Run starts, THE Tool_Layer SHALL load exactly one Incident_Snapshot, the Incident_Snapshot of the Run, before the first Tool_Call or LLM call of the Run.
3. WHEN the Tool_Layer receives a Tool_Request during a Run, THE Tool_Layer SHALL return only records taken from the Incident_Snapshot loaded at the start of that Run, without network access and unaffected by changes to files under the Snapshot_Root after Run start.
4. IF the Incident_Snapshot of a Run does not exist, cannot be read, or fails Incident_Snapshot model validation, THEN THE Tool_Layer SHALL raise an error naming the Incident_Snapshot path and the failure reason before any Tool_Call or LLM call of the Run is made.
5. WHILE the `investigate` or `eval` subcommand executes, THE CLI SHALL keep the Snapshot_Root unchanged, with the same file paths and byte-identical file contents at subcommand exit as at subcommand start (invariant property).
6. THE Investigator SHALL deliver remediation only as Recommended_Actions in the Investigation_Report, without executing any Recommended_Action.

### Requirement 3: Shared Investigation State (Blackboard)

**User Story:** As an Agent developer, I want one shared evidence board, so that Agents collaborate on the same evidence instead of passing free text along a chain.

#### Acceptance Criteria

1. THE Blackboard SHALL store the Evidence ledger, Hypotheses, Tasks, Evidence_Requests, Data_Gaps, Incident_Report claim statuses and Budget usage of exactly one Run, with no entry carried over from any other Run.
2. WHEN a Tool_Call returns a Tool_Result with a null error, including a Tool_Result with zero records, THE Tool_Layer SHALL append exactly one Evidence_Item to the Blackboard before the Tool_Result reaches the calling Agent, containing an Evidence_ID that the Blackboard assigns and that is unique within the Run, the Tool, the Tool input as the query, the Observation_Summary as the observation (which records that no records matched when the Tool_Result contains zero records), the time range of the Tool input with its end set to the as-of time when the stale flag is set (no time range for Tools whose input has no time range), the reliability, and the IDs of every record in the Tool_Result as the source record IDs.
3. WHEN the Tool_Layer delivers a Tool_Result for which an Evidence_Item was appended, THE Tool_Layer SHALL deliver the Evidence_ID of that Evidence_Item to the calling Agent together with the Tool_Result.
4. IF the Tool_Result partial flag is set, THEN THE Tool_Layer SHALL set the Evidence_Item reliability to `partial`, including when the stale flag is also set.
5. IF the Tool_Result stale flag is set and the partial flag is unset, THEN THE Tool_Layer SHALL set the Evidence_Item reliability to `stale`.
6. WHEN the Tool_Layer appends an Evidence_Item for a Tool_Result with both the partial flag and the stale flag unset, THE Tool_Layer SHALL set the Evidence_Item reliability to `complete`, whether or not the truncated flag is set.
7. IF any component attempts to modify or delete an Evidence_Item after insertion, THEN THE Blackboard SHALL reject the attempt with an error and keep every field of the Evidence_Item identical to its inserted value until the Run ends.
8. WHILE two or more writes to the Blackboard run concurrently, THE Blackboard SHALL apply every write with none lost or overwritten, so that N concurrent Evidence_Item appends add exactly N Evidence_Items with N distinct Evidence_IDs and N concurrent Budget usage increments raise Budget usage by the sum of the N increments.
9. WHEN a Reasoning_Agent Task outputs a Hypothesis whose Hypothesis ID already exists in the Blackboard, THE Blackboard SHALL store the output as a new revision under the same Hypothesis ID with a revision number exactly 1 greater than the highest revision number stored for that Hypothesis ID (the first stored version has revision number 1), and SHALL retain every prior revision with all fields unchanged.
10. FOR ALL Blackboard states, serializing the Blackboard to JSON and parsing the JSON SHALL produce a Blackboard equal to the original in every field, including Evidence_Item insertion order, the full records retained for every Evidence_Item and every Hypothesis revision (round-trip property).
11. THE Tool_Layer SHALL compute the Observation_Summary of every Evidence_Item without an LLM call, deterministically from only the Tool_Result and the configured Observation_Summary size.
12. THE Blackboard SHALL retain the full records of the Tool_Result of every Evidence_Item unchanged until the Run ends, and SHALL return, without a Tool_Call and without counting against the Tool_Call Budget, every retained record of an Evidence_Item on lookup by that Evidence_ID and the retained record with a given source record ID on lookup by that source record ID.

### Requirement 4: Citation Validation and Fact/Assumption Labelling

**User Story:** As a reviewer, I want every claim labelled as fact or assumption, so that I can tell what a tool returned from what an agent inferred.

#### Acceptance Criteria

1. WHEN an Agent output or a Baseline_Agent output passes the Structured_Output_Validator, THE Citation_Validator SHALL validate every Claim, Hypothesis and Recommended_Action in the output against the Run_Ledger contents at the time of validation, before the output is written to the Blackboard or included in an Investigation_Report.
2. WHEN the Citation_Validator validates a Claim that cites at least one Evidence_ID present in the Run_Ledger, THE Citation_Validator SHALL label the Claim a Fact, regardless of the reliability of the cited Evidence_Items and of whether the cited Evidence_Items are Historical_Evidence.
3. WHEN the Citation_Validator validates a Claim that cites zero Evidence_IDs or cites only Evidence_IDs absent from the Run_Ledger, THE Citation_Validator SHALL label the Claim an Assumption.
4. IF a Claim, the supporting or contradicting Evidence_IDs of a Hypothesis, or a Recommended_Action cites an Evidence_ID absent from the Run_Ledger, THEN THE Citation_Validator SHALL remove that Evidence_ID and retain the Claim, Hypothesis or Recommended_Action with every remaining Evidence_ID in the original order.
5. IF a Recommended_Action references a Hypothesis ID that matches no Hypothesis of the Run (the Blackboard Hypotheses for an Investigator Run, or the Hypotheses in the same Baseline_Agent output for a Baseline_Agent Run), THEN THE Citation_Validator SHALL remove the Recommended_Action from the output so that the Investigation_Report omits the Recommended_Action.
6. IF the Citation_Validator removes an Evidence_ID or a Recommended_Action, THEN THE Citation_Validator SHALL record one citation violation per removal in the Run_Trace, identifying the Agent or Baseline_Agent that produced the output, the affected Claim, Hypothesis or Recommended_Action, and the absent Evidence_ID or Hypothesis ID.
7. THE Citation_Validator SHALL produce identical labels, identical removals and citation violations for the same absent Evidence_IDs and Hypothesis IDs when given an Investigator output and a Baseline_Agent output that contain identical Claims, Hypotheses and Recommended_Actions and are validated against identical sets of Evidence_IDs and Hypothesis IDs.
8. FOR ALL Agent outputs and Baseline_Agent outputs, applying the Citation_Validator a second time to the validated output against the same Run_Ledger and Run Hypotheses SHALL produce an output identical to the first validated output and SHALL record zero additional citation violations (idempotence property).

### Requirement 5: Provider-Agnostic LLM Client and Structured Outputs

**User Story:** As a developer, I want one LLM interface with validated structured outputs, so that Agents are provider-independent and malformed model output is caught.

#### Acceptance Criteria

1. THE LLM_Client SHALL expose one interface, with identical request parameters and identical response types for the Anthropic Provider_Adapter, the OpenAI Provider_Adapter and the Fake_LLM, through which every Agent and the Baseline_Agent make every language-model call.
2. THE LLM_Client SHALL route every LLM call of a Run to the single configured model through the Anthropic Provider_Adapter, the OpenAI Provider_Adapter or the Fake_LLM, as named by the configured provider.
3. WHEN an LLM call returns a provider response, including the response to a repair prompt, THE LLM_Client SHALL record the input token count and the output token count from the provider usage data, attributed to the calling Agent or the Baseline_Agent, in the Run_Trace and in Budget usage, with Token_Usage equal to the sum of the input and output token counts of every LLM call in the Run.
4. WHEN the LLM_Client returns output for a call made by an Agent or by the Baseline_Agent, THE Structured_Output_Validator SHALL validate the output against the Pydantic output model declared for that call, treating empty or unparseable output as a validation failure.
5. IF LLM output fails validation, THEN THE Structured_Output_Validator SHALL send exactly one repair prompt through the LLM_Client that contains every validation error with the location of the failing field, and validate the repaired output against the same Pydantic output model.
6. IF the repaired LLM output fails validation, THEN THE Structured_Output_Validator SHALL raise an Agent_Failure for the calling Agent or the Baseline_Agent, carrying the validation errors of the repaired output, in place of returning any output for the call.
7. IF a provider returns a rate-limit error or a server-side error, or returns no response within the configured LLM request timeout (default 60 seconds), THEN THE LLM_Client SHALL retry the call until the total number of attempts reaches the configured LLM maximum attempts (default 3, including the first attempt), waiting before each retry for the configured base delay doubled for each prior retry, capped at the configured maximum delay and measured through the Injectable_Clock.
8. IF the final permitted attempt of an LLM call fails, or a provider returns an error other than a rate-limit error, a server-side error or a request timeout, THEN THE LLM_Client SHALL end the call with an error, raised to the calling Agent or the Baseline_Agent, that names the provider error category.
9. THE LLM_Client SHALL read provider API keys only from environment variables.
10. THE LLM_Client SHALL exclude API key values from every Run_Trace, Investigation_Report, log output and error message.
11. WHERE the fake provider is configured, THE LLM_Client SHALL return the Fake_LLM scripted response and scripted synthetic usage data for each LLM call without making a network request or reading an API key.
12. WHERE the fake provider is configured, IF the Fake_LLM script has no response for an LLM call, THEN THE Fake_LLM SHALL raise an error naming the calling Agent or the Baseline_Agent in place of returning output.

### Requirement 6: Orchestrator Planning and Task Dependencies

**User Story:** As a reviewer, I want the Orchestrator to plan an explicit task graph, so that investigation order follows evidence dependencies.

#### Acceptance Criteria

1. WHEN a Run starts, THE Orchestrator SHALL create, before dispatching any Task, an initial Task_Graph in which every Task is in the pending state and which contains between 1 and the configured maximum number of initial Tasks for each Specialist_Agent, exactly one Reasoning_Agent Task that depends on every Specialist_Agent Task in the initial Task_Graph, exactly one Critic_Agent Task that depends on the Reasoning_Agent Task, and exactly one Reporting_Agent Task.
2. THE Orchestrator SHALL move a Task from pending to running only after every Task in the dependency list of that Task is in the completed, failed or cancelled state and, for the Reporting_Agent Task, only after the feedback loop has ended with a recorded Termination_Reason.
3. IF a planned Task_Graph contains a cycle (including a Task that depends on itself), a duplicate Task ID, a dependency on a Task ID absent from the planned Task_Graph, or a set of Tasks or dependencies that differs from the structure required by criterion 1, THEN THE Orchestrator SHALL discard the entire planned Task_Graph without dispatching any of its Tasks and use the Default_Task_Graph.
4. IF the Orchestrator planning output fails Structured_Output_Validator validation after the repair retry, or the planning LLM call fails after the configured maximum LLM_Client attempts, THEN THE Orchestrator SHALL use the Default_Task_Graph and continue the Run.
5. WHEN a Task changes state, THE Orchestrator SHALL record one Run_Trace event containing the Task ID, the assigned Agent, the previous state, the new state and a timestamp.
6. THE Orchestrator SHALL dispatch Tasks so that the sequence of pending-to-running transitions recorded in the Run_Trace is a topological order of the Task_Graph in use (the validated planned Task_Graph or the Default_Task_Graph), including Tasks added during the feedback loop (invariant property).
7. THE Orchestrator SHALL build the Default_Task_Graph without an LLM call and with the same structure in every Run: exactly one Observability_Agent Task and exactly one Dependency_Agent Task with empty dependency lists, one Reasoning_Agent Task that depends on both Specialist_Agent Tasks, one Critic_Agent Task that depends on the Reasoning_Agent Task, and one Reporting_Agent Task.
8. WHEN the Orchestrator uses the Default_Task_Graph, THE Orchestrator SHALL record one Run_Trace event naming the condition from criterion 3 or criterion 4 that caused the fallback.
9. THE Orchestrator SHALL change Task states only through the transitions pending to running, pending to cancelled, running to completed, running to failed and running to cancelled (invariant property).

### Requirement 7: Parallel Execution of Independent Tasks

**User Story:** As a reviewer, I want independent tasks to run in parallel, so that the investigation uses wall-clock time efficiently.

#### Acceptance Criteria

1. WHEN the Orchestrator creates the Task_Graph, adds a Task to the Task_Graph, or a Task reaches the completed, failed or cancelled state, THE Orchestrator SHALL set Ready_Tasks to running without waiting for any running Task to finish, until the number of Tasks in the running state equals the configured concurrency limit or no Ready_Task remains.
2. WHERE the configured concurrency limit is 2 or more, WHEN the Orchestrator starts dispatching a Task_Graph that contains an Observability_Agent Task with no dependencies and a Dependency_Agent Task with no dependencies, THE Orchestrator SHALL set one such Observability_Agent Task and one such Dependency_Agent Task to running before either of those two Tasks leaves the running state.
3. THE Orchestrator SHALL keep the number of Tasks in the running state, counted across all Agents, at or below the configured concurrency limit at every Task state transition recorded in the Run_Trace (invariant property).
4. WHERE the configured concurrency limit is 2 or more, WHEN one Critic_Round yields Evidence_Requests that the Orchestrator does not discard as duplicates and that target both the Observability_Agent and the Dependency_Agent, THE Orchestrator SHALL set one resulting Observability_Agent Task and one resulting Dependency_Agent Task to running before either of those two Tasks leaves the running state.
5. IF an attempt of a running Task fails while other Tasks are in the running state, THEN THE Orchestrator SHALL let each other running Task continue to the completed or failed state without cancelling it, unless the wall-clock timeout elapses.
6. IF the configured concurrency limit is not an integer from 1 to 16, THEN THE CLI SHALL exit with a non-zero status and an error message naming the concurrency limit field before any Tool_Call or LLM call is made.

### Requirement 8: Observability Agent

**User Story:** As an SRE, I want a specialist that investigates logs and metrics, so that symptoms, onset times and affected services are established from telemetry.

#### Acceptance Criteria

1. THE Observability_Agent SHALL make Tool_Requests only for query_logs and query_metrics.
2. WHEN the Observability_Agent receives its first Task of a Run, THE Observability_Agent SHALL make at least one query_logs Tool_Request and at least one query_metrics Tool_Request for the affected service named in the Incident_Report, each with a time range that starts at the Incident_Report start time minus the configured pre-incident margin (a non-negative duration) and ends later than the Incident_Report start time.
3. WHEN the Observability_Agent completes a Task, THE Observability_Agent SHALL output findings in which each Claim names one service, names one signal as either a metric name or a log level with a text pattern, states either an onset time or a no-onset marker when the Claim reports the signal as unchanged over the queried time range, and cites one or more Evidence_IDs.
4. THE Observability_Agent SHALL make at most the configured per-Task step limit (an integer of at least 2) of Tool_Requests in one Task, counting each Tool_Request as one step regardless of how many Tool_Calls the Retry_Policy makes for the Tool_Request and regardless of rejection with error code `invalid_input`, by the Circuit_Breaker or by the Tool_Call Budget.
5. WHEN the Observability_Agent has made the configured per-Task step limit of Tool_Requests in a Task, THE Observability_Agent SHALL output findings for that Task without raising an Agent_Failure.
6. IF the Observability_Agent receives a Tool_Result with a non-null error, including error codes `timeout`, `invalid_input`, `circuit_open` and `budget_exhausted`, THEN THE Observability_Agent SHALL continue the Task through to findings output without raising an Agent_Failure.
7. WHEN the Observability_Agent receives a Task created from an Evidence_Request whose suggested Tool is query_logs or query_metrics, THE Observability_Agent SHALL make its first Tool_Request of that Task to the suggested Tool with the suggested Tool parameters.

### Requirement 9: Dependency Agent

**User Story:** As an SRE, I want a specialist that examines changes, dependencies and incident history, so that recent changes and upstream causes are considered.

#### Acceptance Criteria

1. WHILE the Dependency_Agent executes a Task, THE Dependency_Agent SHALL make Tool_Requests only for get_deployments, get_dependencies and search_incidents.
2. WHEN the Dependency_Agent receives the initial Task, THE Dependency_Agent SHALL make get_deployments Tool_Requests for the affected service named in the Incident_Report that together cover deployment, configuration-change and feature-flag-change events over the interval starting the configured change lookback period (default 7 days) before the Incident_Report start time and ending at the Incident_Report start time.
3. WHEN the Dependency_Agent completes a Task, THE Dependency_Agent SHALL output findings in which each Claim names every Component the Claim concerns, states the finding type (deployment, configuration change, feature-flag change, dependency relation or past incident), states the event timestamp for every finding type other than dependency relation, and cites at least one Evidence_ID.
4. WHEN the Dependency_Agent outputs a Claim that cites an Evidence_Item returned by search_incidents, THE Dependency_Agent SHALL label the Claim as Historical_Evidence and cite only Evidence_Items returned by search_incidents in that Claim.
5. WHEN the Dependency_Agent has made the configured per-Task step limit of Tool_Requests in a Task, counting each Tool_Request as one step including a Tool_Request rejected with error code `invalid_input`, THE Dependency_Agent SHALL make no further Tool_Request in that Task and SHALL return the Task findings without raising an Agent_Failure.
6. WHEN the Dependency_Agent receives the initial Task, THE Dependency_Agent SHALL make at least one get_dependencies Tool_Request for the affected service named in the Incident_Report.
7. WHEN the Dependency_Agent receives a Task created from an Evidence_Request whose suggested Tool is get_deployments, get_dependencies or search_incidents, THE Dependency_Agent SHALL make the first Tool_Request of that Task to the suggested Tool with the suggested Tool parameters.
8. IF the Tool_Layer returns an error for a Dependency_Agent Tool_Request, including `timeout` after the final retry attempt, `circuit_open`, `budget_exhausted` or `invalid_input`, THEN THE Dependency_Agent SHALL continue the Task and return the Task findings without raising an Agent_Failure.

### Requirement 10: Reasoning Agent and Hypothesis Revision

**User Story:** As a reviewer, I want competing hypotheses that are compared and revised as evidence arrives, so that the conclusion is not anchored on the first explanation.

#### Acceptance Criteria

1. WHEN the Reasoning_Agent receives a Task, THE Reasoning_Agent SHALL output between 2 and 5 competing Hypotheses, where no two Hypotheses in the same output share a Root_Cause_Label.
2. THE Reasoning_Agent SHALL give every Hypothesis a Component named in the dependency map of the Incident_Snapshot, a Cause_Type from the closed Cause_Type vocabulary, a non-empty statement of at most 500 characters, a list of supporting Evidence_IDs, a list of contradicting Evidence_IDs and a Confidence, with no Evidence_ID appearing in both lists of the same Hypothesis.
3. THE Reasoning_Agent SHALL assign each rank from 1 to N, where N is the number of Hypotheses in the output, to exactly one Hypothesis such that Confidence is non-increasing as rank increases (invariant property).
4. WHILE the feedback loop has not ended, WHEN an Evidence_Item is added to the Blackboard after the most recent Reasoning_Agent Task reached a terminal state, THE Orchestrator SHALL keep exactly one pending Reasoning_Agent Task that depends on every Specialist_Agent Task created after that Reasoning_Agent Task reached a terminal state.
5. THE Reasoning_Agent SHALL output, for each Hypothesis, an evidence-sufficiency flag with value true or false and a missing-evidence list of 0 to 10 entries, each entry describing one item of evidence absent from the Blackboard.
6. THE Reasoning_Agent SHALL make zero Tool_Requests during every Reasoning_Agent Task.
7. WHEN the Reasoning_Agent receives a Task, THE Reasoning_Agent SHALL include in the LLM prompt of that Task, for every Evidence_Item held in the Blackboard at Task start, the Evidence_ID, the Tool, the query, the reliability and the Observation_Summary and no full records, together with every Data_Gap, every Incident_Report claim status and every Evidence_Request held in the Blackboard at Task start, and every Hypothesis output by the most recent completed Reasoning_Agent Task.
8. THE Reasoning_Agent SHALL give each output Hypothesis the Hypothesis ID of the Blackboard Hypothesis that has the same Root_Cause_Label, and otherwise a Hypothesis ID not yet used in the Run.
9. IF a Hypothesis cites no supporting Evidence_ID whose Evidence_Item has reliability `complete` and is not Historical_Evidence, THEN THE Reasoning_Agent SHALL set the evidence-sufficiency flag of that Hypothesis to false.

### Requirement 11: Critic Agent and Evidence Feedback Loop

**User Story:** As a reviewer, I want a critic that challenges conclusions and sends targeted evidence requests back to the specialists, so that weak conclusions are tested before reporting.

#### Acceptance Criteria

1. WHEN the Orchestrator dispatches a Critic_Agent Task, THE Critic_Agent SHALL output exactly one of an acceptance naming the Hypothesis ID of the top-ranked Hypothesis (the rank-1 Hypothesis among the latest Hypothesis revisions in the Blackboard) or a list of between 1 and the configured maximum Evidence_Requests per Critic_Round, where the configured maximum is an integer from 1 to 10.
2. THE Critic_Agent SHALL give each Evidence_Request a target Specialist_Agent, a Hypothesis ID, exactly one concern category from missing_evidence, contradicting_evidence, unexplored_alternative, temporal_coincidence, historical_only_support, low_reliability_support or unverified_report_claim, a non-empty question of at most 500 characters, a suggested Tool, suggested Tool parameters and a non-empty rationale of at most 1,000 characters.
3. IF the Critic_Agent output contains both an acceptance and Evidence_Requests, contains neither, contains more Evidence_Requests than the configured maximum, or contains an acceptance naming a Hypothesis ID other than the top-ranked Hypothesis ID, THEN THE Structured_Output_Validator SHALL treat the Critic_Agent output as failing validation.
4. IF an Evidence_Request names a Hypothesis ID absent from the latest Hypothesis revisions in the Blackboard, names a suggested Tool outside the Tools of the target Specialist_Agent (query_logs and query_metrics for the Observability_Agent; get_deployments, get_dependencies and search_incidents for the Dependency_Agent), or has suggested Tool parameters that fail validation against the Pydantic input model of the suggested Tool, THEN THE Structured_Output_Validator SHALL treat the Critic_Agent output as failing validation.
5. IF the top-ranked Hypothesis cites no supporting Evidence_ID other than Historical_Evidence Evidence_IDs, THEN THE Structured_Output_Validator SHALL treat a Critic_Agent output containing an acceptance as failing validation.
6. IF the top-ranked Hypothesis cites one or more Historical_Evidence Evidence_IDs as support and no other supporting Evidence_ID, THEN THE Critic_Agent SHALL output at least one Evidence_Request with concern category historical_only_support that names the top-ranked Hypothesis ID.
7. WHEN the Critic_Agent outputs Evidence_Requests that pass validation in a Critic_Round numbered below the Critic_Round Budget, THE Orchestrator SHALL create, for each Evidence_Request not discarded as a duplicate, one Task for the target Specialist_Agent that carries that Evidence_Request.
8. WHEN the Orchestrator creates Specialist_Agent Tasks for a Critic_Round, THE Orchestrator SHALL create exactly one new Reasoning_Agent Task that depends on every Specialist_Agent Task created for that Critic_Round.
9. WHEN the Orchestrator creates the new Reasoning_Agent Task for a Critic_Round, THE Orchestrator SHALL create exactly one new Critic_Agent Task that depends on that Reasoning_Agent Task.
10. IF an Evidence_Request has the same target Specialist_Agent and suggested Tool as a completed Specialist_Agent Task that carries an Evidence_Request, or as an earlier Evidence_Request in the same Critic_Round, and has suggested Tool parameters equal to those of that Task or Evidence_Request after parsing into the Pydantic input model of the Tool, THEN THE Orchestrator SHALL discard the Evidence_Request as a duplicate.
11. WHEN the Orchestrator discards an Evidence_Request as a duplicate, THE Orchestrator SHALL record in the Run_Trace the discarded Evidence_Request and the matching completed Task ID or the matching earlier Evidence_Request.
12. IF every Evidence_Request output in a Critic_Round numbered below the Critic_Round Budget is discarded as a duplicate, THEN THE Orchestrator SHALL end the feedback loop with Termination_Reason duplicate_requests_only.
13. WHEN the Critic_Agent outputs Evidence_Requests that pass validation in the Critic_Round numbered equal to the Critic_Round Budget, THE Orchestrator SHALL end the feedback loop with Termination_Reason max_critic_rounds without creating Tasks for those Evidence_Requests.
14. THE Orchestrator SHALL run at most the configured Critic_Round Budget of Critic_Rounds per Run, where the Critic_Round Budget is an integer from 1 to 3.
15. WHEN the Orchestrator dispatches a Critic_Agent Task, THE Critic_Agent SHALL include in the LLM prompt of that Task, for every Evidence_Item held in the Blackboard at Task start, the Evidence_ID, the Tool, the query, the reliability and the Observation_Summary and no full records.
16. IF the top-ranked Hypothesis cites at least one supporting Evidence_ID that is not Historical_Evidence and every supporting Evidence_Item of the top-ranked Hypothesis that is not Historical_Evidence has reliability `partial` or `stale`, THEN THE Structured_Output_Validator SHALL treat a Critic_Agent output containing an acceptance as failing validation.
17. IF the top-ranked Hypothesis cites at least one supporting Evidence_ID that is not Historical_Evidence and every supporting Evidence_Item of the top-ranked Hypothesis that is not Historical_Evidence has reliability `partial` or `stale`, THEN THE Critic_Agent SHALL output at least one Evidence_Request with concern category low_reliability_support that names the top-ranked Hypothesis ID.

### Requirement 12: Reporting Agent

**User Story:** As an SRE, I want a reporting agent that writes the final report and remediation, so that conclusions and actions are presented in one consistent document.

#### Acceptance Criteria

1. WHILE the remaining token Budget (the token Budget minus Token_Usage) is at or above the configured Reporting_Agent reserve, WHEN the feedback loop terminates with a Termination_Reason other than wall_clock_timeout, THE Reporting_Agent SHALL produce the Investigation_Report and at most the configured maximum number of Recommended_Actions using only Blackboard contents and zero Tool_Requests.
2. THE Investigator SHALL carry the Verdict, the Termination_Reason, every Hypothesis with the field values stored after the Confidence_Policy, every Data_Gap and every Incident_Report claim status from the Blackboard into the Investigation_Report unchanged, whether the Reporting_Agent or the Deterministic_Report_Generator builds the Investigation_Report.
3. THE Reporting_Agent SHALL tie each Recommended_Action to exactly one Hypothesis ID present in the Blackboard.
4. THE Reporting_Agent SHALL cite at least one Evidence_ID present in the Blackboard in each Recommended_Action whose category is mitigation, fix or prevention.
5. THE Reporting_Agent SHALL assign each Recommended_Action exactly one category from mitigation, fix, prevention or further_investigation.
6. IF the Reporting_Agent Task fails after the Orchestrator's single retry, THEN THE Deterministic_Report_Generator SHALL build the Investigation_Report from the Blackboard, including the Data_Gap recorded for the failed Reporting_Agent Task.
7. IF the remaining token Budget is below the configured Reporting_Agent reserve when the feedback loop terminates, THEN THE Orchestrator SHALL cancel the Reporting_Agent Task before any Reporting_Agent LLM call and have the Deterministic_Report_Generator build the Investigation_Report from the Blackboard.

### Requirement 13: Typed Tools

**User Story:** As an Agent developer, I want typed tools with explicit error and completeness flags, so that agents handle bad or missing data explicitly.

#### Acceptance Criteria

1. THE Tool_Layer SHALL provide exactly five Tools, query_logs, query_metrics, get_deployments, get_dependencies and search_incidents, each with a Pydantic input model that accepts an optional record limit from 1 to 500 defaulting to 100, and a Pydantic output model.
2. THE Tool_Layer SHALL return every Tool_Result with a list of records, an error that is null on success and otherwise holds a message and one error code from `timeout`, `invalid_input`, `circuit_open` or `budget_exhausted`, a partial flag, a stale flag, an as-of time that is set when the stale flag is set and null otherwise, and a truncated flag.
3. WHEN query_logs receives a valid input, THE query_logs Tool SHALL return the log records of the requested service within the requested time range that match every supplied optional filter: a set of log levels, a text pattern of 1 to 200 characters matched as a case-insensitive substring of the log message, and up to 10 record attribute key-value pairs matched by exact equality, including feature-flag cohort attributes.
4. WHEN query_metrics receives a valid input, THE query_metrics Tool SHALL return the series of the requested metric name for the requested service within the requested time range as one record per aggregation step, where the aggregation step is a whole number of seconds from 60 to 3600 defaulting to 300.
5. WHEN get_deployments receives a valid input, THE get_deployments Tool SHALL return the deployment, configuration-change and feature-flag-change events within the requested time range, restricted to the requested service and to the requested change types when those optional filters are supplied.
6. WHEN get_dependencies receives a valid input, THE get_dependencies Tool SHALL return every Component within the requested depth of 1 to 5 hops, defaulting to 1, from the requested service, each labelled upstream (a Component the service depends on) or downstream (a Component that depends on the service) with its hop distance.
7. WHEN search_incidents receives a valid input, THE search_incidents Tool SHALL return the past incidents whose text contains at least one whitespace-separated term of a text query of 1 to 200 characters, compared case-insensitively, restricted to past incidents that list the requested service as affected when the optional service is supplied.
8. IF a Tool_Request input omits a required field, contains a value of the wrong type or outside a bound stated in this requirement, has a time range whose start is not before its end, or names a service that is not a Component of the Incident_Snapshot, THEN THE Tool_Layer SHALL reject the Tool_Request without making a Tool_Call, return a Tool_Result with error code `invalid_input` and a message naming each invalid field, and record the rejection in the Run_Trace, without retry, without applying the Failure_Injector, without counting against the Tool_Call Budget, without recording a Data_Gap and without changing the Consecutive_Failure_Count.
9. IF the records matching a valid Tool input outnumber the record limit, THEN THE Tool_Layer SHALL return the first N matching records in ascending timestamp order, with ties broken by ascending record ID, where N is the record limit, and set the truncated flag.
10. FOR ALL valid Tool inputs and Incident_Snapshots, two Tool_Requests to the same Tool with the same input on the same Incident_Snapshot that receive no injected failure SHALL return identical records in identical order and identical truncated flags (idempotence property).
11. IF a Tool_Result carries a non-null error, THEN THE Tool_Layer SHALL return the Tool_Result with zero records and with the partial, stale and truncated flags unset.
12. WHEN a valid Tool input matches zero records and the Failure_Injector injects no failure, THE Tool_Layer SHALL return a Tool_Result with zero records, a null error and the partial, stale and truncated flags unset.
13. THE Tool_Layer SHALL treat every requested time range as including records at the start time and excluding records at the end time.

### Requirement 14: Deterministic Tool Failure Injection

**User Story:** As an evaluator, I want reproducible tool failures, so that both systems face the same degraded conditions and tests are deterministic.

#### Acceptance Criteria

1. WHERE random failure injection is enabled in the configuration, WHEN a Tool_Request input passes validation, THE Failure_Injector SHALL select exactly one outcome for each Tool_Call made for that Tool_Request from timeout, partial-result, stale-data or no failure, choosing each failure type with its configured per-type probability and no failure with the remaining probability, where each per-type probability is from 0.0 to 1.0 inclusive, the three per-type probabilities sum to at most 1.0, and the stale-data probability is treated as 0.0 for get_dependencies and search_incidents.
2. FOR ALL combinations of Seed, Incident_Snapshot, configured per-type probabilities, random-injection setting, Tool, Tool input (compared by validated field values) and attempt number (1 for the first Tool_Call of a Tool_Request, increased by 1 for each retry), THE Failure_Injector SHALL produce the same failure outcome, consisting of the failure type, the records returned and the as-of time, on every evaluation, independent of the System_Under_Test, the calling Agent, the order and concurrency of Tool_Calls and the wall-clock time (determinism property).
3. WHEN a timeout failure is injected into a Tool_Call, THE Tool_Layer SHALL return a Tool_Result with error code `timeout`, zero records and the partial, stale and truncated flags unset, without appending an Evidence_Item to the Run_Ledger.
4. WHEN a partial-result failure is injected into a Tool_Call, THE Tool_Layer SHALL return a Tool_Result with no error, the partial flag set, the stale flag unset and a strict subset of the records that the same Tool_Call returns with no injected failure (the uninjected result), in the same order, containing at least 1 record when the uninjected result contains 2 or more records and zero records when the uninjected result contains at most 1 record.
5. WHEN a stale-data failure is injected into a Tool_Call, THE Tool_Layer SHALL return a Tool_Result with no error, the stale flag set, the partial flag unset, an as-of time at or after the requested start time and before the requested end time, and exactly those records of the uninjected result whose timestamps are at or before the as-of time.
6. WHERE the Failure_Profile of an Incident_Snapshot declares a forced failure for a Tool, THE Failure_Injector SHALL inject the declared failure type into every Tool_Call of that Tool whose input passes validation, including every retry attempt, for every Seed and in place of any randomly selected outcome.
7. WHERE random failure injection is disabled in the configuration, THE Failure_Injector SHALL inject no failure into any Tool_Call of a Tool without a Failure_Profile forced failure, regardless of the configured per-type probabilities.
8. FOR ALL Seeds, across 10,000 distinct combinations of Tool input and attempt number for one Tool without a Failure_Profile forced failure and with random failure injection enabled, THE Failure_Injector SHALL select each failure type for a share of the Tool_Calls within 0.03 of the per-type probability applicable to that Tool, and for zero Tool_Calls when that probability is 0.0 (distribution property).
9. IF the Failure_Profile of the loaded Incident_Snapshot names a Tool other than query_logs, query_metrics, get_deployments, get_dependencies or search_incidents, declares a failure type other than timeout, partial-result or stale-data, declares more than one forced failure for one Tool, or declares a stale-data failure for get_dependencies or search_incidents, THEN THE Tool_Layer SHALL reject the Incident_Snapshot with an error naming the invalid Failure_Profile entry before any Tool_Call or LLM call is made.

### Requirement 15: Tool Retries, Backoff and Circuit Breaker

**User Story:** As a reviewer, I want bounded retries and a circuit breaker on tools, so that transient failures are recovered and persistent failures stop consuming budget.

#### Acceptance Criteria

1. IF a Tool_Call returns error code `timeout` and its Tool_Request has made fewer Tool_Calls than the configured maximum attempts (an integer from 1 to 10 that includes the first Tool_Call), THEN THE Retry_Policy SHALL make retry number k of that Tool_Request (k starting at 1), with the same Tool input and attempt number k + 1, after a backoff delay equal to the configured base delay (from 0.01 to 10 seconds) × 2^(k−1) plus a jitter from 0 to the configured base delay × 2^(k−1), with the sum capped at the configured maximum delay (from the base delay to 60 seconds).
2. THE Retry_Policy SHALL compute each jitter value only from the Run Seed, the Tool, the Tool input and the retry number, so that equal combinations yield equal backoff delays in every Run.
3. WHEN a Tool_Call returns a Tool_Result with a null error code, including a Tool_Result with the partial flag or the stale flag set, THE Tool_Layer SHALL return that Tool_Result as the outcome of its Tool_Request without a further Tool_Call.
4. WHEN the Consecutive_Failure_Count of a Tool reaches the configured failure threshold (an integer from 1 to 20), or a Trial_Call of that Tool returns error code `timeout`, THE Circuit_Breaker SHALL open, or remain open, for that Tool with a cooldown of the configured duration (from 0.1 to 600 seconds) starting on the Injectable_Clock when that Tool_Call returns.
5. WHILE the Circuit_Breaker for a Tool is open, THE Circuit_Breaker SHALL reject every Tool_Request and every retry attempt for that Tool, other than the Trial_Call, with a Tool_Result that has error code `circuit_open` and no records, without invoking the Tool.
6. WHILE the Circuit_Breaker for a Tool is open, its cooldown has elapsed and no Trial_Call of that Tool is awaiting its Tool_Result, WHEN a Tool_Request or retry attempt for that Tool is submitted, THE Circuit_Breaker SHALL admit that attempt as the Trial_Call.
7. WHEN the Trial_Call of a Tool returns a null error code, THE Circuit_Breaker SHALL close for that Tool.
8. THE Circuit_Breaker SHALL keep a separate open or closed state, cooldown and Consecutive_Failure_Count for each Tool within each Run, starting each Run with every Tool closed and at a Consecutive_Failure_Count of 0.
9. THE Tool_Layer SHALL count against the Tool_Call Budget every Tool_Call, including each retry attempt and each Trial_Call, and no Circuit_Breaker rejection or Tool_Request rejected with error code `invalid_input`.
10. WHEN the Circuit_Breaker rejects a Tool_Request or a retry attempt, THE Tool_Layer SHALL record the rejection in the Run_Trace with the Tool, the Tool input, error code `circuit_open` and the time of the rejection.
11. THE Retry_Policy and the Circuit_Breaker SHALL read every time value and perform every backoff wait through the Injectable_Clock, making no direct system-clock reads or sleeps.

### Requirement 16: Agent Failure Recovery

**User Story:** As a reviewer, I want agent failures retried once and then recorded, so that one failing agent degrades the investigation instead of aborting it.

#### Acceptance Criteria

1. IF the first attempt of an Agent Task ends in an Agent_Failure, THEN THE Orchestrator SHALL stop the failed attempt and start one retry attempt of the same Task, keeping the Task in the running state and, for a Critic_Agent Task, within the same Critic_Round.
2. IF the retry attempt of an Agent Task ends in an Agent_Failure, THEN THE Orchestrator SHALL mark the Task failed, record one Data_Gap whose source is the Agent, whose Task is the failed Task, whose reason is the failure kind of the retry attempt and whose affected time range is the time range named in the Task or none when the Task names no time range, and continue the Run by dispatching each dependent Task once every Task that dependent Task depends on is in the completed, failed or cancelled state.
3. IF the retry attempt of a Critic_Agent Task ends in an Agent_Failure, THEN THE Orchestrator SHALL end the feedback loop with Termination_Reason critic_failed.
4. IF no Hypothesis exists in the Blackboard when the feedback loop ends, THEN THE Orchestrator SHALL set the Verdict to inconclusive regardless of the Termination_Reason.
5. WHEN the retry attempt of an Agent Task completes without an Agent_Failure, THE Orchestrator SHALL mark the Task completed and record no Data_Gap for the Task.
6. IF an Agent Task attempt ends in an Agent_Failure, THEN THE Orchestrator SHALL record the Agent, the Task, the attempt number (1 or 2) and the failure kind (exception, per-Task timeout or structured-output failure) in the Run_Trace, and SHALL keep every Evidence_Item and all Budget usage that the attempt produced.
7. IF no Hypothesis exists in the Blackboard when every Task that a Critic_Agent Task depends on is in a terminal state, THEN THE Orchestrator SHALL mark the Critic_Agent Task cancelled without a Critic_Agent LLM call or a Critic_Round, and SHALL end the feedback loop with Termination_Reason no_hypotheses.

### Requirement 17: Data Gaps and Confidence Adjustment

**User Story:** As an SRE, I want missing data recorded and reflected in confidence, so that a report built on incomplete data says so.

#### Acceptance Criteria

1. IF a Tool_Request ends with error code `timeout` after the Retry_Policy stops retrying, or with error code `circuit_open` or `budget_exhausted`, THEN THE Tool_Layer SHALL record exactly one Data_Gap for that Tool_Request, with the Tool as source, the Tool input as query, the error code as reason, and the Tool input time range as affected time range (no time range when the Tool input has none), before the Tool_Result reaches the calling Agent.
2. IF every Tool_Request for one or more Tools in a Run ends with a Data_Gap, THEN THE Investigator SHALL continue the Run to exactly one Investigation_Report.
3. WHEN the feedback loop ends, THE Orchestrator SHALL record one Data_Gap for each Evidence_Request that was not discarded as a duplicate and has no Task in the completed or failed state, including every Evidence_Request filed in the final Critic_Round, with the target Specialist_Agent as source, the suggested Tool and suggested Tool parameters as query, a reason naming the unfulfilled Evidence_Request and the Termination_Reason, and the time range in the suggested Tool parameters as affected time range (no time range when the parameters contain none).
4. WHEN the feedback loop ends or the Baseline_Agent gives a final answer, THE Confidence_Policy SHALL, exactly once per Run, after the Data_Gaps of criterion 3 are recorded and before the Verdict is set, assign the latest revision of every Hypothesis an adjusted Confidence of max(0.0, C − p × N), where C is the Confidence of that revision, p is the configured per-Data_Gap penalty from 0.0 to 1.0 inclusive, and N is the number of Data_Gaps recorded for the Run when the Confidence_Policy is applied, counting Data_Gaps that share the same source and the same query or Task once.
5. FOR ALL sets of ranked Hypotheses, Confidence values, penalties from 0.0 to 1.0 inclusive and Data_Gap counts, applying the Confidence_Policy SHALL leave every Hypothesis rank unchanged and keep the adjusted Confidence non-increasing as rank increases (invariant property).
6. THE Investigation_Report SHALL list every Data_Gap recorded for the Run when the Investigation_Report is built, showing for each Data_Gap the source, the query or Task, the reason and the affected time range.
7. THE Tool_Layer SHALL record no Data_Gap for a Tool_Request outcome other than those listed in criterion 1, including error code `invalid_input`, a complete Tool_Result with zero records, a truncated Tool_Result, and a partial or stale Tool_Result.
8. IF a Specialist_Agent receives a Tool_Result with error code `timeout`, `circuit_open` or `budget_exhausted`, THEN THE Specialist_Agent SHALL continue the Task without raising an Agent_Failure because of that Tool_Result.
9. THE Investigation_Report SHALL show the adjusted Confidence from the Confidence_Policy as the Confidence of every Hypothesis.
10. IF a Data_Gap is recorded after the Verdict is set, including the Data_Gap for a failed Reporting_Agent Task, THEN THE Orchestrator SHALL leave the Verdict and the Confidence of every Hypothesis unchanged, applying no further Confidence_Policy adjustment for that Data_Gap.
11. IF a Data_Gap is recorded after the Verdict is set, including the Data_Gap for a failed Reporting_Agent Task, THEN THE Investigation_Report SHALL still list that Data_Gap.

### Requirement 18: Execution Limits and Termination

**User Story:** As a reviewer, I want hard limits and explicit termination conditions, so that every run ends predictably with a report.

#### Acceptance Criteria

1. THE Orchestrator SHALL enforce four per-Run Budgets read from the configuration: a Critic_Round Budget that is an integer from 1 to 3, a Tool_Call Budget that is a positive integer, a token Budget that is a positive integer number of tokens, and a wall-clock timeout that is a positive number of seconds measured from Run start through the Injectable_Clock, together with a Reporting_Agent reserve that is an integer number of tokens from 0 to one less than the token Budget.
2. WHILE the recorded Tool_Call count equals the Tool_Call Budget, IF a Tool_Request arrives or a Retry_Policy retry attempt is due, THEN THE Tool_Layer SHALL return a Tool_Result with error code `budget_exhausted` and no records without invoking the Tool or incrementing the Tool_Call count, and SHALL record one Data_Gap naming the Tool, the query, the reason `budget_exhausted` and the affected time range.
3. WHILE Token_Usage is at or above the token Budget minus the Reporting_Agent reserve, THE Orchestrator SHALL start no LLM call, including a repair prompt, for any Agent other than the Reporting_Agent.
4. WHILE Token_Usage is at or above the token Budget, THE Orchestrator SHALL start no LLM call, including Reporting_Agent LLM calls and repair prompts.
5. WHEN the wall-clock timeout elapses before the Investigation_Report is written, THE Orchestrator SHALL set every pending and running Task, including the Reporting_Agent Task, to cancelled and start no further Tool_Call or LLM call.
6. WHEN the wall-clock timeout elapses before the Investigation_Report is written, THE Orchestrator SHALL write the Investigation_Report built by the Deterministic_Report_Generator from the Blackboard within 5 seconds after the timeout elapses, making no LLM call.
7. WHEN, after Run start, the first of the following conditions occurs, THE Orchestrator SHALL end the feedback loop and record exactly one matching Termination_Reason in the Run_Trace and in the Investigation_Report, choosing the earliest-listed condition when two or more conditions hold at the same check: the Critic_Agent accepts the top-ranked Hypothesis (critic_accepted); the Critic_Agent files Evidence_Requests in the Critic_Round whose number equals the Critic_Round Budget, including when every one of them is a duplicate (max_critic_rounds); every Evidence_Request in an earlier Critic_Round is discarded as a duplicate (duplicate_requests_only); the Tool_Layer returns a Tool_Result with error code `budget_exhausted` (tool_call_budget_exhausted); Token_Usage reaches the token Budget minus the Reporting_Agent reserve (token_budget_exhausted); the wall-clock timeout elapses (wall_clock_timeout); the Critic_Agent Task fails after the retry (critic_failed); no Hypothesis exists when the Critic_Agent Task becomes ready (no_hypotheses).
8. WHEN the feedback loop ends, THE Orchestrator SHALL start no further Critic_Round and SHALL set every pending and running Task other than the Reporting_Agent Task to cancelled.
9. WHEN the feedback loop ends with any Investigator Termination_Reason, THE Investigator SHALL produce exactly one Investigation_Report for the Run.
10. FOR ALL Runs, the recorded Tool_Call count SHALL be at or below the Tool_Call Budget and the recorded Critic_Round count SHALL be at or below the Critic_Round Budget (invariant property).

### Requirement 19: Verdict and Inconclusive Outcome

**User Story:** As an SRE, I want an honest inconclusive result when evidence is insufficient, so that I am not misled by a confident wrong answer.

#### Acceptance Criteria

1. IF the feedback loop ends with Termination_Reason critic_accepted, the Critic_Agent acceptance names the rank-1 Hypothesis ID, the rank-1 Confidence after the Confidence_Policy is at or above the configured conclusive threshold (a value from 0.0 to 1.0 inclusive), and the rank-1 Hypothesis cites at least one supporting Evidence_ID that is present in the Run_Ledger and whose Evidence_Item has reliability `complete` and is not Historical_Evidence, THEN THE Orchestrator SHALL set the Verdict to conclusive.
2. IF any condition in criterion 1 is unmet, including when the feedback loop ends with a Termination_Reason other than critic_accepted or when the Blackboard holds no Hypothesis, THEN THE Orchestrator SHALL set the Verdict to inconclusive.
3. IF the Verdict is inconclusive, THEN THE Investigation_Report SHALL list the latest revision of every Hypothesis in ascending rank order with the Confidence after the Confidence_Policy, listing zero Hypotheses when the Blackboard holds none.
4. IF the feedback loop ends with Termination_Reason max_critic_rounds, tool_call_budget_exhausted, token_budget_exhausted or wall_clock_timeout, THEN THE Investigation_Report SHALL name that Budget limit and show its configured value next to the Budget usage recorded when the feedback loop ended.
5. IF the Verdict is inconclusive, THEN THE Investigation_Report SHALL contain a missing-evidence list that includes every Evidence_Request with no completed Task, every Data_Gap and every missing-evidence entry of the most recently completed Reasoning_Agent Task, with each entry labelled by source (Evidence_Request, Data_Gap or Reasoning_Agent) and, for Evidence_Request and Reasoning_Agent entries, the Hypothesis ID.
6. WHEN the feedback loop ends, THE Orchestrator SHALL set the Verdict exactly once, after the Confidence_Policy has applied every Data_Gap recorded in the Blackboard up to that point (including the Data_Gaps recorded for unfulfilled Evidence_Requests) and before the Reporting_Agent or the Deterministic_Report_Generator builds the Investigation_Report.
7. THE Reporting_Agent and the Deterministic_Report_Generator SHALL copy the Verdict set by the Orchestrator into the Investigation_Report unchanged.

### Requirement 20: Investigation Report Content

**User Story:** As an SRE, I want an evidence-based report, so that I can verify each conclusion and act on the recommendations.

#### Acceptance Criteria

1. THE Investigation_Report SHALL contain each of the following sections, keeping a section that has zero entries as an empty section rather than omitting it: incident reference, Verdict, Termination_Reason, ranked Hypotheses, Facts, Assumptions, Data_Gaps, Recommended_Actions, Incident_Report claim statuses, Budget usage and Evidence appendix.
2. THE Investigation_Report SHALL show each ranked Hypothesis at its highest revision number, in ascending rank order starting at rank 1, with the Hypothesis ID, the rank, the Root_Cause_Label (Component and Cause_Type), the statement, the supporting Evidence_IDs, the contradicting Evidence_IDs, the Confidence after the Confidence_Policy and the contributing factors, showing an empty contributing-factors list when the Hypothesis has none.
3. THE Investigation_Report SHALL present every Claim labelled by the Citation_Validator for the Investigation_Report in exactly one of two separately labelled sections: the Facts section, showing each Fact with its text and cited Evidence_IDs, or the Assumptions section, showing each Assumption with its text.
4. THE Investigation_Report SHALL list in the Evidence appendix every Evidence_Item in the Run_Ledger exactly once, showing the Evidence_ID, the Tool, the query, the observation and the reliability (complete, partial or stale).
5. THE Investigator and the Baseline_Agent SHALL produce Investigation_Reports that validate against the same Investigation_Report model, including Investigation_Reports built by the Deterministic_Report_Generator.
6. THE Investigation_Report SHALL show, as the incident reference, the incident ID of the Incident_Snapshot and the title, affected service, start time and severity of the Incident_Report.
7. THE Investigation_Report SHALL show, as the Budget usage, the amount consumed when the Investigation_Report is built and the configured limit for each of Tool_Calls, LLM tokens, Critic_Rounds and wall-clock time.
8. THE Investigation_Report SHALL show, for each Recommended_Action, the recommendation text, the category (mitigation, fix, prevention or further_investigation), the Hypothesis ID and the cited Evidence_IDs.

### Requirement 21: Report Serialization and Rendering

**User Story:** As a developer, I want JSON and Markdown report outputs that stay in sync, so that machines and humans read the same content.

#### Acceptance Criteria

1. WHEN a Run produces its Investigation_Report, THE CLI SHALL write one JSON_Report and one Markdown_Report from the same Investigation_Report model into the same output directory, with file names that differ only in file extension.
2. THE Markdown_Renderer SHALL render the Markdown_Report using only the Investigation_Report model as input and without LLM calls.
3. FOR ALL Investigation_Reports, the Markdown_Report SHALL contain the Verdict, the Termination_Reason, every Hypothesis ID in ascending rank order with its rank and its Confidence rounded to 2 decimal places, every Evidence_ID present in the JSON_Report, every Recommended_Action with its Hypothesis ID and category, every Data_Gap with its source and reason, and every Incident_Report claim with its claim status (completeness property).
4. FOR ALL Investigation_Reports, serializing the Investigation_Report to a JSON_Report and parsing the JSON_Report SHALL produce an Investigation_Report equal to the original under Pydantic model equality, with every field value and every list order preserved (round-trip property).
5. IF a JSON file loaded as an Investigation_Report fails Investigation_Report schema validation, THEN THE CLI SHALL reject the file without producing an Investigation_Report and return an error naming the file and the field path of each invalid field.
6. IF a JSON file loaded as an Investigation_Report is not syntactically valid JSON, THEN THE CLI SHALL reject the file without producing an Investigation_Report and return an error naming the file and the line and column of the first syntax error.
7. IF the Hypotheses, Facts, Assumptions, Data_Gaps, Recommended_Actions, Incident_Report claims or Evidence appendix section of an Investigation_Report is empty, THEN THE Markdown_Renderer SHALL render that section's heading followed by a statement that the section has no entries.
8. FOR ALL Runs, rendering the Investigation_Report parsed from the JSON_Report of the Run with the Markdown_Renderer SHALL produce Markdown byte-identical to the Markdown_Report of the Run (sync property).

### Requirement 22: Scenario Specs and Data Generator

**User Story:** As an evaluator, I want incidents generated from parametric scenario specs with noise and red herrings, so that the dataset is reproducible and contains no hardcoded answers.

#### Acceptance Criteria

1. WHEN a Scenario_Spec file is loaded, THE Data_Generator SHALL parse the Scenario_Spec file into a validated Scenario_Spec model.
2. IF a Scenario_Spec file is unparseable, omits a required field, declares a service count outside 8 to 15, a telemetry window duration outside 2 to 24 hours, a change lookback outside 7 to 30 days, a Cause_Type outside the closed vocabulary, an unknown Fault_Template, or a Failure_Profile that names an unknown Tool, names a failure type other than timeout, partial-result or stale-data, declares stale-data for get_dependencies or search_incidents, or declares more than one forced failure for one Tool, THEN THE Data_Generator SHALL reject the Scenario_Spec with an error naming each invalid field and the violated constraint, and write no Incident_Snapshot, Incident_Report or Answer_Key for that Scenario_Spec.
3. THE Data_Generator SHALL format every valid Scenario_Spec model into a Scenario_Spec file that passes Scenario_Spec validation.
4. FOR ALL valid Scenario_Spec files, parsing, formatting the parsed model and parsing the result SHALL produce a Scenario_Spec model equal in every field to the first parsed model (round-trip property).
5. WHEN the Data_Generator runs with a valid Scenario_Spec and a Seed, THE Data_Generator SHALL produce exactly one Incident_Snapshot containing the Failure_Profile declared by the Scenario_Spec (an empty Failure_Profile when none is declared), exactly one Incident_Report and exactly one Answer_Key containing either one Root_Cause_Label or the insufficient-evidence marker.
6. FOR ALL valid Scenario_Specs and Seeds, running the Data_Generator twice with the same Scenario_Spec and Seed, including at different wall-clock times, SHALL produce Incident_Snapshot, Incident_Report and Answer_Key files with identical names and byte-identical contents (determinism property).
7. THE Data_Generator SHALL generate each Incident_Snapshot with a dependency map naming between 8 and 15 services inclusive, equal to the service count declared by the Scenario_Spec.
8. THE Data_Generator SHALL generate change events with timestamps only in the half-open range [telemetry window start minus the Scenario_Spec change lookback, telemetry window end), including at least 1 change event before the telemetry window start, where the change lookback is between 7 and 30 days inclusive and defaults to 7 days when the Scenario_Spec omits it.
9. THE Data_Generator SHALL add background noise to every Incident_Snapshot consisting of at least 5 error-level log records, at least 2 change events and at least 1 noisy alert log record whose record IDs are absent from the Answer_Key Key_Evidence, plus metric jitter that gives every metric series at least 2 distinct values.
10. WHEN a Scenario_Spec declares a Trap, THE Data_Generator SHALL add every red herring declared by the Trap to the Incident_Snapshot as records whose record IDs are absent from the Answer_Key Key_Evidence.
11. THE Data_Generator SHALL derive service names, record IDs, incident IDs, timestamps and the canary token only from the Seed and the Scenario_Spec, with record IDs unique within each Incident_Snapshot and incident IDs distinct for distinct Scenario_Spec and Seed pairs.
12. THE Data_Generator SHALL generate the fault records of every Incident_Snapshot only through the Fault_Template named in the Scenario_Spec, parameterized by Scenario_Spec values and the Seed.
13. WHEN the Data_Generator has generated the outputs for a Scenario_Spec and a Seed, THE Data_Generator SHALL verify, before writing any output, that every Key_Evidence record ID exists in the Incident_Snapshot, that an Answer_Key with a Root_Cause_Label lists at least 1 Key_Evidence record ID, that the Root_Cause_Label Component and the Incident_Report affected service are named in the dependency map, and that the Incident_Report start time lies within the telemetry window.
14. FOR ALL generated Incident_Snapshots, writing the Incident_Snapshot to disk and loading the Incident_Snapshot SHALL produce an Incident_Snapshot equal in every record and field to the written Incident_Snapshot (round-trip property).
15. THE Data_Generator SHALL timestamp every log record and metric data point of an Incident_Snapshot within the half-open telemetry window [start, end), whose duration equals the telemetry window duration declared by the Scenario_Spec, between 2 and 24 hours inclusive.
16. THE Data_Generator SHALL name each Incident_Snapshot directory with an opaque incident ID that contains no scenario name, Fault_Template name or Cause_Type.
17. IF the verification in criterion 13 fails, THEN THE Data_Generator SHALL return an error naming each failed check and each missing Key_Evidence record ID, and write no Incident_Snapshot, Incident_Report or Answer_Key for that Scenario_Spec and Seed.

### Requirement 23: Scenario Catalogue

**User Story:** As an evaluator, I want at least 12 scenarios covering misleading evidence, conflicting observations and tool failures, so that the comparison exercises realistic traps.

#### Acceptance Criteria

1. THE Scenario_Catalogue SHALL include a bad configuration deploy scenario, serving as the baseline case, whose Scenario_Spec declares no trap and whose Key_Evidence includes a configuration-change event on the Root_Cause_Label Component timestamped inside the telemetry window and before Symptom_Onset.
2. THE Scenario_Catalogue SHALL include a database failover scenario whose Root_Cause_Label Component is a database, whose Incident_Snapshot contains a deployment event on a Component other than the Root_Cause_Label Component timestamped exactly 5 minutes before Symptom_Onset, whose Key_Evidence excludes that deployment event, and whose Scenario_Spec trap names deploy regression as the decoy Cause_Type.
3. THE Scenario_Catalogue SHALL include a third-party API outage scenario whose Root_Cause_Label Component is a third-party API, whose Incident_Snapshot contains zero deployment, configuration-change and feature-flag-change events on any Component inside the telemetry window, and whose Failure_Profile declares no forced failure for get_deployments.
4. THE Scenario_Catalogue SHALL include a slow memory leak scenario whose Key_Evidence includes a change event on the Root_Cause_Label Component timestamped at least 48 hours before Symptom_Onset and inside the change lookback period.
5. THE Scenario_Catalogue SHALL include an expired TLS certificate scenario whose Incident_Snapshot contains zero deployment events, from the change lookback start to the telemetry window end, on the Root_Cause_Label Component and on every Component along any dependency-map path from the Incident_Report affected service to the Root_Cause_Label Component, and whose Failure_Profile declares no forced failure for get_deployments.
6. THE Scenario_Catalogue SHALL include a cache stampede scenario whose request-rate metric for the Incident_Report affected service peaks within 5 minutes after Symptom_Onset at no less than 5 times its mean over the 30 minutes before Symptom_Onset, whose Key_Evidence includes a cache hit-ratio metric record of the Root_Cause_Label Component timestamped no later than that peak, and whose Scenario_Spec trap names traffic attack as the decoy Cause_Type.
7. THE Scenario_Catalogue SHALL include a load-balancer misconfiguration scenario whose Root_Cause_Label Component is a load balancer and whose Incident_Snapshot contains health-check records from two distinct monitors for the same service, in which, over a span of at least 10 minutes starting at or after Symptom_Onset, every record from one monitor reports the service healthy and every record from the other monitor reports the service unhealthy.
8. THE Scenario_Catalogue SHALL include a disk-full scenario whose Incident_Snapshot contains a past incident that names the Incident_Report affected service, lists every Incident_Report symptom, records a Cause_Type different from the Answer_Key Cause_Type and is absent from the Key_Evidence, and whose Scenario_Spec trap names that past incident Cause_Type as the decoy Cause_Type.
9. THE Scenario_Catalogue SHALL include a logs-timeout scenario whose Failure_Profile forces query_logs to time out on every Tool_Call and declares no forced failure for any other Tool, whose Answer_Key contains a Root_Cause_Label and lists an expected Data_Gap with source query_logs and reason timeout, and whose Key_Evidence contains only metric records, change events and dependency-map records.
10. THE Scenario_Catalogue SHALL include an insufficient-evidence scenario whose Answer_Key carries the insufficient-evidence marker and no Root_Cause_Label, and whose Scenario_Spec declares symptom records for the Incident_Report affected service and no causal record.
11. THE Scenario_Catalogue SHALL include a feature-flag rollout scenario whose Key_Evidence includes a feature-flag-change event on the Root_Cause_Label Component timestamped before Symptom_Onset, in which every error log record of that Component at or after Symptom_Onset carries the cohort attribute enabled by that feature-flag-change event, and in which that Component also has log records without that cohort attribute at or after Symptom_Onset.
12. THE Scenario_Catalogue SHALL include a cascading failure scenario in which the Component with the strictly highest count of error log records from Symptom_Onset to the telemetry window end is not the Root_Cause_Label Component and has the Root_Cause_Label Component as a direct or transitive upstream dependency in the dependency map.
13. THE closed Cause_Type vocabulary SHALL include every Cause_Type used in an Answer_Key Root_Cause_Label and every decoy Cause_Type named in a Scenario_Spec trap, including traffic attack and deploy regression.
14. THE Scenario_Catalogue SHALL contain at least 12 Scenario_Specs with unique scenario names, with each of criteria 1 to 12 satisfied by a different Scenario_Spec.
15. THE Scenario_Catalogue SHALL give every scenario other than the insufficient-evidence scenario an Answer_Key that contains a Root_Cause_Label and at least one Key_Evidence record ID that refers to a record other than a past incident.
16. IF an Incident_Snapshot or Answer_Key generated for a Scenario_Catalogue scenario violates a condition stated for that scenario in criteria 1 to 12, THEN THE Data_Generator SHALL return a descriptive error naming the scenario, the Seed and the violated condition instead of writing the Incident_Snapshot, Incident_Report and Answer_Key for that scenario.

### Requirement 24: Answer Key Isolation and No Hardcoded Answers

**User Story:** As a reviewer, I want guarantees that agents and tools never see the answer key, so that evaluation results are trustworthy.

#### Acceptance Criteria

1. THE Data_Generator SHALL write each Answer_Key only under the Answer_Key_Root and each Incident_Snapshot and Incident_Report only under the Snapshot_Root.
2. THE Scenario_Catalogue SHALL store every Scenario_Spec file at a location that resolves, after symbolic-link resolution, outside the Snapshot_Root.
3. IF a file that the Tool_Layer opens, including the Incident_Snapshot path supplied for a Run, resolves after `..` normalization and symbolic-link resolution to a location outside the Snapshot_Root, THEN THE Tool_Layer SHALL read no bytes from that file and SHALL fail the Incident_Snapshot load with an error naming the rejected path.
4. THE Evaluator SHALL be the only component outside the test suite that reads or deserializes files under the Answer_Key_Root.
5. WHEN the Data_Generator produces an Answer_Key, THE Data_Generator SHALL write into that Answer_Key a canary token of at least 32 characters that differs from the canary token of every other Answer_Key under the Answer_Key_Root and that occurs in no file under the Snapshot_Root and in no other Data_Generator output.
6. FOR ALL Runs of either System_Under_Test, no Answer_Key canary token and no Answer_Key_Root path, as configured or as resolved, SHALL occur as a case-insensitive substring of any LLM prompt including repair prompts, any Tool_Result, any serialized Blackboard state, the Run_Trace, or the JSON or Markdown Investigation_Report (isolation property).
7. WHEN a Run starts, THE Investigator and the Baseline_Agent SHALL receive Run inputs and a configuration that contain no Answer_Key, no canary token, no Answer_Key_Root path and no Scenario_Name.
8. THE Investigator and the Baseline_Agent SHALL contain no code that reads Scenario_Catalogue files or branches on a Scenario_Name or an incident ID, and no Prompt_Template that contains a Scenario_Name or the Component of any Answer_Key Root_Cause_Label.
9. THE CLI SHALL be the only component outside the test suite that imports Evaluator code.
10. IF the Snapshot_Root and the Answer_Key_Root resolve, after `..` normalization and symbolic-link resolution, to the same directory or to two directories of which one contains the other, THEN THE CLI SHALL exit with a non-zero status and an error message naming both roots before writing any file, making any Tool_Call or making any LLM call.
11. THE Data_Generator SHALL write no Scenario_Name, no Scenario_Spec trap declaration and no marker that labels a record, event, Component or Cause_Type as the root cause, a contributing factor, Key_Evidence or a red herring into any file under the Snapshot_Root.

### Requirement 25: Single-Agent ReAct Baseline

**User Story:** As an evaluator, I want a single-agent baseline with identical resources, so that differences in results come from the multi-agent architecture.

#### Acceptance Criteria

1. THE Baseline_Agent SHALL run one sequential ReAct loop that invokes no other Agent, starts a step only while the Tool_Call Budget, the token Budget and the wall-clock timeout are all unreached, makes one LLM_Client call per step plus at most one Structured_Output_Validator repair call, yields per step a reasoning note plus exactly one of either one Tool_Request to any of the five Tools or a final answer, and passes each Tool_Request observation, including an observation carrying an error Tool_Result, to the next step.
2. THE Baseline_Agent SHALL take the provider, model, Seed, Tool_Call Budget, token Budget, wall-clock timeout, Retry_Policy, Circuit_Breaker, Failure_Injector, Confidence_Policy penalty and conclusive threshold settings from the same configuration fields the Investigator reads with no Baseline_Agent-specific override, use the same Tool_Layer, Citation_Validator, Confidence_Policy and Investigation_Report model as the Investigator, read the Incident_Snapshot and Incident_Report selected for the Run, run zero Critic_Rounds, and record none of the Termination_Reasons critic_accepted, max_critic_rounds, duplicate_requests_only, critic_failed or no_hypotheses.
3. WHEN a Baseline_Agent Tool_Request returns a Tool_Result with a null error, THE Tool_Layer SHALL append one Evidence_Item to the Run_Ledger of the Baseline_Agent Run before the Tool_Result reaches the Baseline_Agent and return to the Baseline_Agent the observation in the format of criterion 10.
4. WHEN a step yields a final answer that passes the Structured_Output_Validator, THE Baseline_Agent SHALL apply the Citation_Validator with the Run_Ledger as the Evidence source and with the rules the Citation_Validator applies to Investigator outputs and to Reasoning_Agent claim statuses, then apply the Confidence_Policy for every Data_Gap recorded in the Run, then build the Investigation_Report in the shared Investigation_Report schema from the validated final answer and the Run_Ledger, and record Termination_Reason `final_answer`.
5. THE Baseline_Agent SHALL set the Verdict to conclusive only when the final answer declares the outcome conclusive, the rank-1 Hypothesis Confidence after the Confidence_Policy is at or above the configured conclusive threshold, and the rank-1 Hypothesis cites, after the Citation_Validator has run, at least one supporting Evidence_ID that is present in the Run_Ledger and whose Evidence_Item has reliability `complete` and is not Historical_Evidence, and SHALL set the Verdict to inconclusive in every other case, including a final answer with zero Hypotheses.
6. IF the recorded Tool_Call count reaches the Tool_Call Budget, Token_Usage reaches the token Budget, or the wall-clock timeout elapses before a step yields a final answer that passes the Structured_Output_Validator, THEN THE Deterministic_Report_Generator SHALL build, within 5 seconds of the limit being reached, an inconclusive Investigation_Report from the Run_Ledger, the Data_Gaps and the Incident_Report claim statuses of the Run, with Termination_Reason `tool_call_budget_exhausted`, `token_budget_exhausted` or `wall_clock_timeout` naming the limit reached first.
7. THE Structured_Output_Validator SHALL accept a Baseline_Agent final answer only when the final answer contains a declared outcome of conclusive or inconclusive, at most 5 Hypotheses that meet criteria 2 and 3 of Requirement 10, a status of `verified`, `contradicted` or `unverified` for every Incident_Report claim with at least one cited Evidence_ID for each `verified` or `contradicted` status, and Recommended_Actions that meet criteria 3 to 5 of Requirement 12.
8. WHEN a Baseline_Agent Run starts, THE Baseline_Agent SHALL record each Incident_Report claim for the Run as an Assumption with status `unverified` before the first step.
9. IF a step raises an exception, exceeds the configured per-Task timeout or produces an Agent_Failure from the Structured_Output_Validator, and the one retry of that step also fails, THEN THE Baseline_Agent SHALL record a Data_Gap naming the Baseline_Agent and the step number, end the loop with Termination_Reason `baseline_failed`, and pass the Run to the Deterministic_Report_Generator, which builds an inconclusive Investigation_Report from the Run_Ledger.
10. THE Tool_Layer SHALL deliver to the Baseline_Agent and to each Specialist_Agent the same observation format for every Tool_Request, consisting of the Evidence_ID when an Evidence_Item was appended, the Observation_Summary, the records of the Tool_Result up to the record limit of the Tool input, the error, the partial flag, the stale flag with the as-of time, and the truncated flag.

### Requirement 26: Evaluation Runner

**User Story:** As an evaluator, I want one command that runs both systems over every scenario several times, so that the comparison is complete and repeatable.

#### Acceptance Criteria

1. WHEN the `eval` subcommand is invoked, THE Evaluator SHALL execute exactly N Runs, with run indices 1 to N, of each selected System_Under_Test on each selected scenario, where by default the selected scenarios are every Scenario_Catalogue scenario, the selected Systems_Under_Test are the Investigator and the Baseline_Agent, and N is 3.
2. THE Evaluator SHALL compute each Run's Seed as a deterministic function of only the configured Seed, the scenario and the run index, and SHALL give the Investigator Run and the Baseline_Agent Run that share a scenario and run index the same Seed, the same Incident_Snapshot and the same Incident_Report.
3. THE Evaluator SHALL have at most one Run in progress at any time, ordering Runs by scenario name, then ascending run index, then the Investigator before the Baseline_Agent.
4. WHEN a Run completes with an Investigation_Report, THE Evaluator SHALL write the JSON Investigation_Report, the Markdown Investigation_Report and the Run_Trace, with the Run Seed recorded in the Run_Trace, to a per-Run location identified by scenario, System_Under_Test and run index, with no Run overwriting another Run's files.
5. WHEN every selected Run has completed or been recorded as failed, THE Evaluator SHALL write the Results_Table as Markdown and as JSON with identical values, including the provider, the model, the configured Seed, the Run count, the evaluation date and the number of failed Runs per System_Under_Test per scenario.
6. WHEN the `eval` subcommand is invoked with a scenario option, a System_Under_Test option or a Run-count option, THE Evaluator SHALL replace the corresponding default with only the named scenarios, only the named Systems_Under_Test, or the given Run count.
7. IF a Run raises an unhandled error, or the Incident_Snapshot, Incident_Report or Answer_Key of the Run cannot be loaded, THEN THE Evaluator SHALL record the Run in the Results_Table JSON as failed with the scenario, System_Under_Test, run index and error message, score the Run as top-1 and top-3 incorrect, and continue with the next Run.
8. IF the `eval` subcommand receives a scenario option naming a scenario absent from the Scenario_Catalogue, a System_Under_Test option naming neither the Investigator nor the Baseline_Agent, or a Run-count option that is not an integer from 1 to 10, THEN THE CLI SHALL exit with a non-zero status and an error message naming the invalid option before any Run starts.
9. THE Results_Table SHALL include failed Runs in the top-1 accuracy, top-3 accuracy and ranking hit@3 means and Spreads, SHALL exclude failed Runs from the over-cautious rate, over-confident rate, citation precision, Key_Evidence recall, Tool_Call count, latency and token means and Spreads, and SHALL report each excluded metric as unavailable for a System_Under_Test and scenario in which every Run failed.
10. THE Evaluator SHALL pass no Answer_Key content, no Answer_Key_Root path and no scenario name to the Investigator or the Baseline_Agent.

### Requirement 27: Evaluation Metrics

**User Story:** As an evaluator, I want objective metrics computed without an LLM judge, so that accuracy and grounding comparisons are reproducible.

#### Acceptance Criteria

1. WHEN the Evaluator scores a Run, THE Evaluator SHALL assign a top-1 score of 1 in exactly two cases, (a) the Answer_Key carries the insufficient-evidence marker and the Verdict is inconclusive, or (b) the Answer_Key contains a Root_Cause_Label, the Verdict is conclusive and the rank-1 Hypothesis in the Investigation_Report matches the Root_Cause_Label, and a top-1 score of 0 in every other case, including an inconclusive Verdict against a Root_Cause_Label, a conclusive Verdict against the insufficient-evidence marker, an Investigation_Report with no Hypotheses and a Run recorded as failed.
2. WHEN the Evaluator scores a Run, THE Evaluator SHALL assign a top-3 score of 1 in exactly two cases, (a) the Answer_Key carries the insufficient-evidence marker and the Verdict is inconclusive, or (b) the Answer_Key contains a Root_Cause_Label, the Verdict is conclusive and at least one Hypothesis ranked 1, 2 or 3 in the Investigation_Report matches the Root_Cause_Label, considering only the ranks present when fewer than 3 Hypotheses exist, and a top-3 score of 0 in every other case, including an inconclusive Verdict against a Root_Cause_Label, a conclusive Verdict against the insufficient-evidence marker, an Investigation_Report with no Hypotheses and a Run recorded as failed.
3. WHEN the Evaluator compares a Hypothesis with a Root_Cause_Label, THE Evaluator SHALL report a match if and only if the Hypothesis Component equals the Answer_Key Component after both are trimmed of leading and trailing whitespace and converted to lowercase, and the Hypothesis Cause_Type exactly equals the Answer_Key Cause_Type, ignoring the Hypothesis statement, Confidence and contributing factors and using no LLM call.
4. WHEN the Evaluator scores a Run that is not recorded as failed and whose Investigation_Report contains at least one Evidence_ID citation, THE Evaluator SHALL compute citation precision as V ÷ C in the range 0.0 to 1.0, where C is the number of Evidence_ID citations in the Investigation_Report outside the Evidence appendix, counting each distinct pair of citing Claim and cited Evidence_ID once, and V is the number of those citations whose Evidence_ID exists in the Run_Ledger and whose Evidence_Item passes the Support_Check for the citing Claim.
5. WHEN the Evaluator scores a Run that is not recorded as failed and whose Answer_Key lists at least one Key_Evidence record ID, THE Evaluator SHALL compute Key_Evidence recall as F ÷ K in the range 0.0 to 1.0, where K is the number of distinct Key_Evidence record IDs in the Answer_Key and F is the number of those record IDs that appear among the source record IDs of at least one Evidence_Item in the Run_Ledger, whether or not the Investigation_Report cites that Evidence_Item.
6. WHEN a Run completes, THE Evaluator SHALL record for the Run the number of Tool_Calls in the Run_Trace with each retry attempt counted separately, the end-to-end wall-clock latency in seconds from the moment the Evaluator starts the Run until the JSON Investigation_Report write completes, and the input token count, the output token count and the Token_Usage, each summed from provider usage data over every LLM call of the Run including Structured_Output_Validator repair retries.
7. WHEN an Answer_Key lists one or more expected Data_Gaps, THE Evaluator SHALL record per Run, for each expected Data_Gap, whether the Investigation_Report lists at least one Data_Gap whose source equals the source of the expected Data_Gap and whose reason equals the reason of the expected Data_Gap.
8. THE Results_Table SHALL report, for each System_Under_Test on each scenario, the mean ± Spread over that scenario's Runs, and for each System_Under_Test across all scenarios, the mean ± sample standard deviation over the pooled per-Run values of all scenarios (0.0 for a single value), of each of these metrics: top-1 score (reported as top-1 accuracy), top-3 score (reported as top-3 accuracy), citation precision, Key_Evidence recall, Tool_Call count, latency, input token count, output token count and Token_Usage, and, labelled as diagnostic metrics in a group separate from the accuracy metrics, ranking hit@3, over-cautious indicator (reported as over-cautious rate) and over-confident indicator (reported as over-confident rate).
9. FOR ALL Investigation_Reports, Run_Ledgers, Run_Traces and Answer_Keys, THE Evaluator SHALL compute identical top-1 scores, top-3 scores, citation precision, Key_Evidence recall, Tool_Call counts and expected Data_Gap results on repeated scoring of the same inputs, using no LLM call (determinism property).
10. IF a Run is not recorded as failed and its Investigation_Report contains no Evidence_ID citation outside the Evidence appendix, THEN THE Evaluator SHALL set the Run citation precision to 0.0.
11. IF the Answer_Key lists no Key_Evidence record IDs, THEN THE Evaluator SHALL record the Run Key_Evidence recall as not applicable, exclude the Run from every Key_Evidence recall aggregate in the Results_Table, and show not applicable in each Results_Table cell for which no Run has a Key_Evidence recall value.
12. IF a Run is recorded as failed, THEN THE Evaluator SHALL set the Run citation precision to 0.0, set the Run Key_Evidence recall to 0.0 when the Answer_Key lists at least one Key_Evidence record ID, record every expected Data_Gap as absent, and record the Tool_Call count, latency and token counts accumulated from Run start until the failure is recorded.
13. WHEN the Evaluator scores a Run whose Answer_Key contains a Root_Cause_Label, THE Evaluator SHALL assign a ranking hit@3 score of 1 if at least one Hypothesis ranked 1, 2 or 3 in the Investigation_Report matches the Root_Cause_Label regardless of the Verdict, considering only the ranks present when fewer than 3 Hypotheses exist, and a ranking hit@3 score of 0 in every other case, including an Investigation_Report with no Hypotheses and a Run recorded as failed.
14. IF the Answer_Key of a Run carries the insufficient-evidence marker, THEN THE Evaluator SHALL record the Run ranking hit@3 as not applicable, exclude the Run from every ranking hit@3 aggregate in the Results_Table, and show not applicable in each Results_Table cell for which no Run has a ranking hit@3 value.
15. WHEN the Evaluator scores a Run that is not recorded as failed, THE Evaluator SHALL assign an over-cautious indicator of 1 when the Answer_Key contains a Root_Cause_Label, the Verdict is inconclusive and the rank-1 Hypothesis in the Investigation_Report matches the Root_Cause_Label, and an over-cautious indicator of 0 in every other case.
16. WHEN the Evaluator scores a Run that is not recorded as failed, THE Evaluator SHALL assign an over-confident indicator of 1 when the Verdict is conclusive and either the Answer_Key carries the insufficient-evidence marker or the rank-1 Hypothesis in the Investigation_Report does not match the Root_Cause_Label, and an over-confident indicator of 0 in every other case.
17. THE Evaluator SHALL compute the over-cautious rate as the mean of the over-cautious indicator and the over-confident rate as the mean of the over-confident indicator, each over the Runs not recorded as failed.

### Requirement 28: Command-Line Interface

**User Story:** As a user, I want a CLI to generate data, investigate one incident and run the evaluation, so that the system is usable without a frontend.

#### Acceptance Criteria

1. WHEN the `generate` subcommand is invoked with an integer Seed, THE CLI SHALL write, for each Scenario_Catalogue scenario, one Incident_Snapshot and one Incident_Report under the Snapshot_Root and one Answer_Key under the Answer_Key_Root.
2. WHEN the `investigate` subcommand is invoked with an Incident_Snapshot path, `--system multi` or `--system baseline`, a configuration file and an output directory, THE CLI SHALL execute exactly one Run of the Investigator for `multi` or of the Baseline_Agent for `baseline` with the Seed from the configuration file, and SHALL write exactly one JSON_Report, one Markdown_Report and one Run_Trace for that Run to the output directory, creating the output directory if absent.
3. WHEN `generate` has written the outputs of every Scenario_Catalogue scenario, `investigate` has written the JSON_Report, the Markdown_Report and the Run_Trace, or `eval` has written the Results_Table, THE CLI SHALL exit with status 0, independent of the Verdict, the Termination_Reason and the number of Evaluator Runs recorded as failed.
4. IF the configured provider is Anthropic or OpenAI and the environment variable holding that provider's API key is unset or empty when `investigate` or `eval` starts, THEN THE CLI SHALL exit with a non-zero status and an error message naming the environment variable before any Tool_Call or LLM call is made and before any output file is written.
5. IF a required argument is missing, an unknown subcommand or option is supplied, or an argument value is invalid (a non-integer Seed, a `--system` value other than `multi` or `baseline`, a run count that is not an integer from 1 to 10, a scenario name absent from the Scenario_Catalogue, a configuration file path that is not a readable file, an Incident_Snapshot path that is not an Incident_Snapshot directory under the Snapshot_Root, or an output directory or Answer_Key_Root that resolves inside the Snapshot_Root), THEN THE CLI SHALL exit with a non-zero status and a usage message naming the offending argument before any Tool_Call or LLM call is made and before any output file is written.
6. WHEN the `eval` subcommand is invoked with a configuration file and an output directory, and optionally with scenario names, `--system multi` or `--system baseline`, and a run count, THE CLI SHALL run the Evaluator with those selections and write to the output directory one JSON_Report, one Markdown_Report and one Run_Trace per Run, each identifiable by scenario, System_Under_Test and run index, plus the Results_Table as Markdown and as JSON.
7. IF `generate`, `investigate` or `eval` cannot write a required output file, or an `investigate` Run raises an unhandled error before the JSON_Report is written, THEN THE CLI SHALL exit with a non-zero status and an error message naming the unwritable path or the error.
8. IF the Data_Generator returns an error for a scenario during `generate`, including a Scenario_Spec validation error or a Key_Evidence record ID absent from the Incident_Snapshot, THEN THE CLI SHALL write no Incident_Snapshot, Incident_Report or Answer_Key for that scenario, generate no further scenarios, and exit with a non-zero status and an error message naming the scenario and the failed check.

### Requirement 29: Configuration and Run Trace

**User Story:** As a developer, I want one configuration file and a per-run trace, so that runs are tunable and the coordination is inspectable.

#### Acceptance Criteria

1. WHEN the CLI is invoked with a configuration file, THE CLI SHALL load every configuration field from that one file and apply the default value documented in the README to each absent field that has a documented default, where the configuration fields and their inclusive permitted ranges are: provider (anthropic, openai or fake); model (non-empty string); LLM request timeout (greater than 0 seconds, default 60 seconds); LLM maximum attempts (integer of at least 1, default 3); LLM base delay (greater than 0 seconds); LLM maximum delay (at or above the LLM base delay); Retry_Policy maximum attempts (integer 1 to 10); Retry_Policy base delay (0.01 to 10 seconds); Retry_Policy maximum delay (from the Retry_Policy base delay to 60 seconds); Circuit_Breaker failure threshold (integer 1 to 20); Circuit_Breaker cooldown (0.1 to 600 seconds); random failure injection enabled flag (boolean); Failure_Injector timeout, partial-result and stale-data probabilities (each 0.0 to 1.0, with a sum of at most 1.0); concurrency limit (integer 1 to 16); per-Task step limit (integer of at least 2); per-Task timeout (greater than 0 seconds); pre-incident margin (duration of at least 0 seconds); change lookback (duration greater than 0 seconds, default 7 days); maximum initial Tasks per Specialist_Agent (integer of at least 1); maximum Evidence_Requests per Critic_Round (integer 1 to 10); Critic_Round Budget (integer 1 to 3); Tool_Call Budget (integer of at least 1); token Budget (integer of at least 1); wall-clock timeout (greater than 0 seconds); Reporting_Agent reserve (integer from 0 to the token Budget minus 1); per-Data_Gap Confidence_Policy penalty (0.0 to 1.0); conclusive threshold (0.0 to 1.0); maximum Recommended_Actions (integer of at least 1); Observation_Summary size (integer 1 to 20, default 5); Seed (integer); Snapshot_Root path; and Answer_Key_Root path (neither equal to nor inside the Snapshot_Root).
2. IF the configuration file cannot be read or parsed, contains a field value of the wrong type or outside the permitted range defined in this requirement, contains a field name that is not a configuration field, or omits a field that has no documented default, THEN THE CLI SHALL exit with a non-zero status and an error message naming every invalid, unknown or missing field, or naming the configuration file when the file cannot be read or parsed, before any Tool_Call or LLM call is made and without starting a Run.
3. THE Run_Trace SHALL record, as separate timestamped events: every Task state transition with the Task ID, the Agent, the previous state and the new state; every Tool_Call with the Tool, the Tool input, the outcome (error code or none, plus the partial, stale and truncated flags), the attempt number starting at 1 and the duration; every call rejected by the Circuit_Breaker with the Tool and error code `circuit_open`; every Tool_Request rejected with error code `invalid_input` with the Tool, the Tool input and each invalid field; and every LLM call with the calling Agent or the Baseline_Agent, the input token count, the output token count and the duration.
4. THE Run_Trace SHALL record, as separate timestamped events: every citation violation with the removed Evidence_ID or Hypothesis ID; every Evidence_Request discarded as a duplicate with the matching completed Task ID or the matching earlier Evidence_Request; every Default_Task_Graph fallback with its cause, the condition from Requirement 6 criterion 3 or criterion 4; and every Critic_Agent decision with the Critic_Round number and either the accepted Hypothesis ID or every filed Evidence_Request with all Evidence_Request fields.
5. WHEN a Run starts, THE Run_Trace SHALL record the effective configuration received by the System_Under_Test, listing every configuration field value after defaults are applied, with no API key value and no Answer_Key_Root path.
6. WHEN a Run ends, THE Run_Trace SHALL record the Termination_Reason and the final Budget usage as the Critic_Round count, the Tool_Call count, the input, output and total token counts and the elapsed wall-clock time, each alongside its configured Budget limit.

### Requirement 30: Automated Test Suite

**User Story:** As a developer, I want an automated test suite that runs offline, so that reliability behaviour is verified on every change.

#### Acceptance Criteria

1. WHEN pytest runs the full Test_Suite with every provider API key environment variable unset, THE Test_Suite SHALL route every Agent and Baseline_Agent LLM call through the Fake_LLM, open zero network connections, skip zero tests for lack of an API key or network access, and complete within 120 seconds of wall-clock time as reported by pytest in a single pytest process on a developer laptop.
2. THE Test_Suite SHALL contain at least one test asserting each acceptance criterion of Requirements 4 (Citation_Validator), 6 (Task_Graph), 7 (parallel dispatch), 13 (Tools), 14 (Failure_Injector) and 15 (Retry_Policy and Circuit_Breaker) and of Requirement 27 criteria 1 to 5 and 13 to 17 (Evaluator scoring rules and diagnostic metrics), with the top-1 and top-3 scoring tests covering all four combinations of Answer_Key outcome (Root_Cause_Label or insufficient-evidence marker) and Verdict (conclusive or inconclusive).
3. THE Test_Suite SHALL test every acceptance criterion in this document labelled as a round-trip, idempotence, invariant, determinism, completeness, isolation, distribution or sync property with a property-based test written with the `hypothesis` Python library that evaluates at least 10 generated examples per property and generates identical examples on repeated invocations.
4. THE Fake_LLM SHALL record every prompt the Fake_LLM receives, including repair prompts, in the order received, so that tests can assert on prompt contents.
5. THE Test_Suite SHALL drive every backoff, cooldown, per-Task timeout and wall-clock timeout through the Injectable_Clock and SHALL perform no sleep of non-zero duration on the real clock.
6. THE Test_Suite SHALL contain, for each Investigator Termination_Reason at least one Investigator Run test, and for final_answer and baseline_failed at least one Baseline_Agent Run test, that drives the Run to that Termination_Reason and asserts that the Run writes exactly one Investigation_Report and that the Investigation_Report and the Run_Trace record that Termination_Reason.
7. THE Test_Suite SHALL contain at least one test per conclusive-Verdict condition that applies the Verdict rule directly to a constructed state in which only that condition is unmet and asserts an inconclusive Verdict (direct tests, because the Structured_Output_Validator rejects a Critic_Agent acceptance in those states under Requirement 11 criteria 5 and 16, so an end-to-end Run cannot reach them), covering the Orchestrator Verdict rule of Requirement 19 criteria 1 and 2 over constructed Blackboard states for the conditions Termination_Reason critic_accepted, a Critic_Agent acceptance naming the rank-1 Hypothesis ID, a rank-1 Confidence after the Confidence_Policy at or above the conclusive threshold, and the supporting-Evidence condition, and the Baseline_Agent Verdict rule of Requirement 25 criterion 5 over constructed Run_Ledger states and final answers for the conditions a final answer declaring the outcome conclusive, a rank-1 Confidence after the Confidence_Policy at or above the conclusive threshold, and the supporting-Evidence condition, with the supporting-Evidence condition tested once each with the only rank-1 supporting Evidence_ID absent from the Run_Ledger, with reliability `partial`, with reliability `stale` and as Historical_Evidence, plus at least one Investigator Run test in which no Hypothesis exists in the Blackboard when the feedback loop ends, asserting an inconclusive Verdict and an Investigation_Report containing the ranked Hypotheses of Requirement 19 criterion 3 and the missing-evidence list of Requirement 19 criterion 5.
8. THE Test_Suite SHALL contain, for every Scenario_Spec in the Scenario_Catalogue, at least one Investigator Run test and at least one Baseline_Agent Run test that assert the Answer_Key canary token is absent from every prompt recorded by the Fake_LLM, every Tool_Result, the Blackboard state or Baseline_Agent ledger at Run end, the Run_Trace and the Investigation_Report.

### Requirement 31: Documentation and Deliverables

**User Story:** As an assignment reviewer, I want complete documentation and evaluation results, so that I can set up, run and assess the submission.

#### Acceptance Criteria

1. THE README SHALL contain a section headed Setup that states Python 3.11 or later as the required Python version, the installation command, the name of the API key environment variable for each of the Anthropic and OpenAI Provider_Adapters, every configuration field loaded under Requirement 29 with its default value, and one complete example command for each of `generate` with a Seed, `investigate` with an Incident_Snapshot path, a System_Under_Test, a configuration file and an output directory, `eval`, and the pytest test suite.
2. THE README SHALL contain a section headed Architecture with a fenced `mermaid` code block that parses without syntax errors and shows the six Agents, the Blackboard, the Tool_Layer and the LLM_Client, followed by an explanation of Agent coordination that covers the Blackboard, the Task_Graph, parallel dispatch, the Critic feedback loop, Budgets and termination, naming every Termination_Reason.
3. THE Repository SHALL contain `TECHNICAL_NOTE.md` at the repository root with sections headed Key Decisions, Trade-offs, Limitations and Potential Improvements, each containing at least one entry, with the Limitations section stating that the Confidence_Policy applies one uniform per-Data_Gap penalty to every Hypothesis regardless of the relevance of each Data_Gap to that Hypothesis, and at most 500 words counted as whitespace-separated tokens across the whole file including headings.
4. THE README SHALL contain a section headed Incomplete Functionality that lists, by requirement and criterion number, each acceptance criterion in this document that the implementation does not satisfy or states that every acceptance criterion is satisfied, and a section headed External Dependencies that states that real Runs need one Anthropic or OpenAI API key, states that the test suite and Runs with the Fake_LLM need no API key, and lists every dependency declared in the Packaging_File with the version pinned in the Packaging_File.
5. THE Repository SHALL contain the Scenario_Catalogue with every Scenario_Spec required by Requirement 23, the automated test suite of Requirement 30, and either the generated Snapshot_Root and Answer_Key_Root contents or a README statement of the exact `generate` command and Seed that reproduce byte-identical Incident_Snapshots, Incident_Reports and Answer_Keys for the scenarios in the committed Results_Table.
6. THE Repository SHALL contain the Markdown Results_Table and the JSON Results_Table written by one `eval` run that covers both the Investigator and the Baseline_Agent, each recording the provider, the model, the Seed, the run date in YYYY-MM-DD format, the number of scenarios, the number of Runs per scenario per System_Under_Test and the exact `eval` command of that run.
7. THE README SHALL contain a section headed Deliverables that states the repository-relative path of the Coding_Assistant_Transcript, the Markdown Results_Table, the JSON Results_Table, the Scenario_Catalogue, the test suite, `TECHNICAL_NOTE.md` and `ASSUMPTIONS.md`, with each stated path resolving to an existing file or directory in the Repository.
8. THE Packaging_File SHALL pin every direct runtime and test dependency to one exact version and SHALL contain no dependency entry with a version range, a wildcard or no version.
9. THE Repository SHALL contain `ASSUMPTIONS.md` at the repository root listing the 19 numbered assumptions, each of which holds for the implemented behaviour and none of which is contradicted by the README or `TECHNICAL_NOTE.md`.
10. IF the committed Results_Table covers fewer scenarios than the Scenario_Catalogue, fewer than 3 Runs per scenario per System_Under_Test, or Runs that used the Fake_LLM, THEN THE README SHALL list each such shortfall in the Incomplete Functionality section.
11. THE Repository SHALL contain no API key value in any committed file, including the Coding_Assistant_Transcript, Run_Traces, Investigation_Reports and Results_Tables.
12. WHEN the installation command, the `generate` command and the test-suite command from the README Setup section are run in that order in a fresh clone of the Repository with Python 3.11 and no API key environment variable set, THE installation, `generate` and test-suite commands SHALL each exit with status 0.
