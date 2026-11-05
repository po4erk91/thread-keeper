# Caliper for Curator

Caliper measures whether a skill helps Codex complete a task. Curator's
independent verifier builds cases from the skill's recorded purpose and runs them without the skill and with
the current skill. It can also test an exact proposed replacement. It
keeps these checks separate from `skill_validate`'s format and mirror checks.

## Enable it

Build the isolated runtime from this checkout and sign Caliper in to your
ChatGPT subscription in a separate private directory.

```sh
docker build -f docker/caliper.Dockerfile -t threadkeeper-caliper .
caliper_dir="$HOME/.threadkeeper/caliper"
install -d -m 700 "$caliper_dir" "$caliper_dir/auth"
XDG_CONFIG_HOME="$caliper_dir/auth" CALIPER_TELEMETRY_DISABLED=1 \
  uvx --from caliper-eval caliper login chatgpt
chmod 700 "$caliper_dir/auth/caliper"
```

Set `THREADKEEPER_CURATOR_CALIPER_DIR` to that absolute directory in
ThreadKeeper's configuration. Set `THREADKEEPER_CURATOR_CALIPER_MODEL`
to the exact Codex model you want to use from that subscription. The model
must work with Caliper's signed-in Codex runner. The regular Curator pass
selects managed skills with recorded context. Advisory passes continue to
produce reports only.

The runner mounts the dedicated Caliper session, staged skill and cases, and
a temporary results directory. Codex runs as an unprivileged user inside
the container. It has its attempt workspace and a short-lived subscription
token. The live ThreadKeeper database, installed skills, checkout, and personal
Codex settings are outside the container. Keep suite prompts, fixtures and
skill content suitable for the model provider. The cases below use synthetic data.

## Where scenarios come from

Curator uses the reason the skill was created or added. A skill created via
ThreadKeeper can link directly to a source note, user quote or user message.
For an imported skill, record why you added it in the skill's existing
frontmatter metadata. Use a short description that is safe to share with the
model provider.

```yaml
metadata:
  threadkeeper-eval-context: >-
    I added this skill to route incoming requests. Open requests go to the
    active queue, closed requests stay in the archive, and drafts stay private.
    Requests with no status need a separate review.
```

Curator creates five or six cases and ties each to a passage in the recorded
context. At least three cases must represent mutually exclusive values of one
decision condition. For the example above, a request can be open, closed or
draft. The remaining cases can cover missing status and a batch of requests.
Curator writes synthetic fixtures and exact expected outcomes. ThreadKeeper
builds deterministic file checks from those values. The original note or
message is kept out of the Caliper scenario inputs. A skill with no recorded
context, or with too little context for distinct cases, stays unscored for
that behavioural check.

Curator proposes a check for eligible managed skills and runs a bounded
number per pass, prioritising skills that have waited longest. Each arm runs
the same cases three times. A repair requires a
passing replacement on every attempt and evidence that Codex loaded it.
Standalone removal requires the baseline to pass every case, the loaded
current skill to fail every case, and any attempted repair to fail every case.
The verifier also checks for unique value and a viable repair before removing
anything. Missing context, mixed results and runtime failures do not authorize
a change. Pinned entries and existing recovery rules still apply.

The verifier records the outcome in its report. The pass snapshot retains
the synthetic cases, case counts, activation counts and fingerprints so an
edit or removal uses the same context, scenarios and skill revision.

## Usefulness scores and rule-only retirement

Curator scores usefulness for the user on a scale from 1 to 10. A score of 1
means the skill adds no standalone value. A score of 2 means a single useful
rule belongs in a lesson. Scores of 3–4 reflect limited benefit, 5–6 reflect
a narrow recurring use, 7–8 reflect reliable improvement, and 9–10 reflect
substantial, repeated benefit on important work. The report includes a short
reason for the score and leaves the score open if required checks are missing.

For scores of 1 or 2, the independent verifier can retire a skill that has
only a rule and no workflow or support files. It first checks that a retained
lesson contains the complete rule. When Caliper is enabled, the verifier
also needs a result that supports removal or shows that
every case passed with and without the loaded skill. The usual protection,
snapshot, and recovery checks still apply.

Call `skill_deletion_log()` to see the date, score, reason, retained lesson,
and recovery reference for each recorded rule-only retirement. Filter with
`name` when you want the history of one skill. The log uses ThreadKeeper's
normal event retention. Recovery copies follow Curator's trash retention.
