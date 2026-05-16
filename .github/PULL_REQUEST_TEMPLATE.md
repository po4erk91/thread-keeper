<!--
Thanks for the PR! A few quick checks before we look at the diff —
they keep review fast and ship rate high. Tick what applies.
-->

## What this changes

<!-- One paragraph. Why is the change worth making? -->

## How

<!-- Brief notes on the approach. Skip if obvious from the diff. -->

## Test plan

<!--
What did you run locally? Include the pytest output or the commands.
For adapter / daemon / prompt changes, include a manual smoke check too.
-->

```
python -m pytest                  # full suite
python -m threadkeeper._setup --dry-run
```

## Checklist

- [ ] `pytest` passes locally (`pip install -e '.[semantic,dev]'`)
- [ ] New / changed behavior has a test
- [ ] If a new MCP tool — added to `threadkeeper/tools/` AND imported in `server.py`
- [ ] If a new adapter — registered in `adapters/__init__.py` with a test in `tests/test_adapters.py`
- [ ] If user-visible behavior changed — README / CONTRIBUTING reflects it
- [ ] No new emoji in code or docs (per house style)
- [ ] No new locale strings outside `threadkeeper/i18n.py`

## Related issues

<!-- "closes #N" / "refs #N" / "split from #N" -->
