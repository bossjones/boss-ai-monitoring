I want to keep artifacts of boss-cmux commands in this folder so that we can find patterns and figure out easier ways to prompt claude to create them.

| Prompt | What it's for |
|---|---|
| [`boss-ai-monitoring-build-team.md`](boss-ai-monitoring-build-team.md) | **Current.** The 7-pane run that *builds* what the spec describes — [`specs/boss-ai-monitoring/boss-ai-monitoring.html`](../specs/boss-ai-monitoring/boss-ai-monitoring.html) Phases 1–9, TDD red-first, entirely in place in this repo (no new GitHub repo, no push). Lead runs opus for coordination; all 6 workers (store, otlp, jsonl+langsmith, web, jobs, validator) run sonnet. |

Format and binding lessons are carried over from the `macos-ci` build-team lineage
(`/Users/bossjones/dev/bossjones/macos-ci/prompts/macos-ci-build-team.md`): the prompt is not
privileged over the evidence, silence is never success, `cmux send` types while `cmux send-key
enter` submits, and `just check` is the only definition of done. The one new lesson this run adds:
when the spec you're building from was drafted assuming a different repo layout than the one it
actually lives in, fix the spec's location language *before* writing the team prompt — otherwise
the team faithfully builds the wrong thing in the wrong place.
