# Caliper for Curator

Caliper measures whether a skill helps Codex complete a task. Curator's
independent verifier runs the configured cases without the skill and with
the current skill. It can also test an exact proposed replacement. It
keeps these checks separate from `skill_validate`'s format and mirror checks.

## Enable it

Build the isolated runtime from this checkout and sign Caliper in to your
ChatGPT subscription in a separate private directory.

```sh
docker build -f docker/caliper.Dockerfile -t threadkeeper-caliper .
caliper_dir="$HOME/.threadkeeper/caliper"
install -d -m 700 "$caliper_dir" "$caliper_dir/auth" "$caliper_dir/suites"
XDG_CONFIG_HOME="$caliper_dir/auth" CALIPER_TELEMETRY_DISABLED=1 \
  uvx --from caliper-eval caliper login chatgpt
chmod 700 "$caliper_dir/auth/caliper"
```

Set `THREADKEEPER_CURATOR_CALIPER_DIR` to that absolute directory in
ThreadKeeper's configuration. Set `THREADKEEPER_CURATOR_CALIPER_MODEL`
to the exact Codex model you want to use from that subscription. The model
must work with Caliper's signed-in Codex runner. The regular Curator pass
picks up the suites. Advisory passes continue to produce reports only.

The runner mounts the dedicated Caliper session, staged skill and cases, and
a temporary results directory. Codex runs as an unprivileged user inside
the container. It has its attempt workspace and a short-lived subscription
token. The live ThreadKeeper database, installed skills, checkout, and personal
Codex settings are outside the container. Keep suite prompts, fixtures and
skill content suitable for the model provider. The cases below use synthetic data.

## Add a suite

Put `<skill-name>.eval.yaml` in the `suites` directory. Supply two to four
independent tasks that exercise the skill's actual job. Each needs a name,
a prompt and inline Python assertions of the observable result. An optional
`setup` shell command can prepare a fixture in the attempt directory.
Only put suites you trust in this directory.

```yaml
tasks:
  - name: Preserve open items
    setup: printf 'open\nclosed\n' > input.txt
    prompt: Read input.txt and write only the open items to result.txt.
    assert: |
      from pathlib import Path
      assert Path("result.txt").read_text().splitlines() == ["open"]
  - name: Handle no open items
    setup: printf 'closed\n' > input.txt
    prompt: Read input.txt and write only the open items to result.txt.
    assert: |
      from pathlib import Path
      assert Path("result.txt").read_text().splitlines() == []
```

Curator proposes a check for each enrolled managed skill and runs a bounded
number per pass, prioritising skills that have waited longest. Each arm runs
the same cases three times. A repair requires a
passing replacement on every attempt and evidence that Codex loaded it.
Standalone removal requires the baseline to pass every case, the loaded
current skill to fail every case, and any attempted repair to fail every case.
The verifier also checks for unique value and a viable repair before removing
anything. Missing suites, mixed results and runtime failures do not authorize
a change. Pinned entries and existing recovery rules still apply.

The verifier records the outcome in its report. The pass snapshot retains
case counts, activation counts and fingerprints so an edit or removal cannot
use evidence from a different skill revision or changed suite.

## Usefulness scores and rule-only retirement

Curator scores usefulness for the user on a scale from 1 to 10. A score of 1
means the skill adds no standalone value. A score of 2 means a single useful
rule belongs in a lesson. Scores of 3–4 reflect limited benefit, 5–6 reflect
a narrow recurring use, 7–8 reflect reliable improvement, and 9–10 reflect
substantial, repeated benefit on important work. The report includes a short
reason for the score and leaves the score open if required checks are missing.

For scores of 1 or 2, the independent verifier can retire a skill that has
only a rule and no workflow or support files. It first checks that a retained
lesson contains the complete rule. If the skill has a configured Caliper
suite, the verifier also needs a result that supports removal or shows that
every case passed with and without the loaded skill. The usual protection,
snapshot, and recovery checks still apply.

Call `skill_deletion_log()` to see the date, score, reason, retained lesson,
and recovery reference for each recorded rule-only retirement. Filter with
`name` when you want the history of one skill. The log uses ThreadKeeper's
normal event retention. Recovery copies follow Curator's trash retention.
