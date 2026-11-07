# Source-tracked skill trust

People who track a published skill choose which publisher they trust before an
update reaches their installed skill folders. A downloaded signature never
establishes that choice. Signing authenticates the publisher and files; it does
not establish whether the instructions are safe or useful.

## Source lock format

The primary skill directory contains `.threadkeeper-skill-source.json`. A
successful update writes lock version 2 and mirrors the same record with the
skill. For example, a Sigstore source uses:

```json
{
  "lock_version": 2,
  "type": "github",
  "repo": "example/skills",
  "ref": "main",
  "path": "skills/example-skill",
  "resolved_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "tree_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "verification_policy": "require_signed",
  "expected_signer": {
    "type": "sigstore",
    "identity": "https://github.com/example/skills/.github/workflows/publish.yml@refs/heads/main",
    "issuer": "https://token.actions.githubusercontent.com"
  },
  "last_verification": {"result": "verified", "checked_at": 1793923200},
  "source_url": "https://github.com/example/skills/tree/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/skills/example-skill",
  "inferred": false,
  "updated_at": 1793923200
}
```

`ref` remains the configured branch, tag, or commit. Each pass resolves it once
per repository/ref through GitHub's commit API, downloads the archive by the
full commit, and checks its top-level commit prefix. A full commit configured
as `ref` must match the API result. Different skill directories in that archive
retain the same resolved commit. This binds the recorded origin to the fetched
archive; publisher authentication comes from OMS verification, not GitHub's
archive name alone.

`tree_sha256` covers every regular file, including hidden files and the detached
signature, except the local source lock itself. In sorted relative POSIX path
order, hash the UTF-8 path, a NUL byte, and the 32-byte SHA-256 digest of the file
contents into one SHA-256 stream. Read failures stop the pass. Downloaded source
locks and symbolic links are refused. A different digest for the same recorded
commit is also refused. The older digest algorithm remains available only to
check the local-edit baseline during migration.

`last_verification` describes the latest accepted tree. A refusal leaves the
lock and every installed copy unchanged; its outcome appears in the latest
`skill_update_pass` event and daemon status. These contain fixed result codes
and counts, without signer identities, certificate paths, verifier output, or
credentials.

## Trust policy

- `require_signed`: require `skill.oms.sig` at the skill directory's top level
  and an explicitly configured expected signer.
- `allow_unsigned`: accept a missing signature only when no signer is pinned.
  The accepted lock and status say `unsigned_allowed`. A present signature
  still requires a pinned signer and full verification. A pinned signed source
  cannot silently downgrade to unsigned.
- Missing, unknown, or malformed policy: refuse with `policy_required`.
  Older records and inferred sources receive no automatic unsigned exemption.
  Existing overwrite flags do not bypass trust checks.

Install the verifier in the same Python environment as thread-keeper:

```sh
python -m pip install 'threadkeeper[skill-signing]'
```

OpenSSF `model-signing` verifies OMS v1 file manifests using SHA-256. The updater
requires every downloaded file except `skill.oms.sig` to appear in the signed
manifest. Extra files, partial manifests, changed content, and invalid
signatures fail closed. Other serialization formats require a publisher to
provide a supported full-directory bundle; they are not silently accepted.

Sigstore sources pin both `identity` and `issuer`. Certificate sources use this
`expected_signer` shape instead:

```json
{
  "type": "certificate",
  "identity": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "certificate_chain": "/trusted/publisher-root.pem"
}
```

Here `identity` is the exact signing leaf certificate's SHA-256 fingerprint
(lowercase hex, no separators). `certificate_chain` is an absolute local path
to independently obtained PEM trust anchors. Obtain the fingerprint and trust
anchors through a trusted publisher channel; do not take them from the same
unverified archive. The verifier checks the chain and signature after checking
the leaf pin. Certificate rotation requires an explicit local trust change.

The source updater verifies before replacing any tracked root, even when the
content hash already matches. Tracked groups skip ordinary local synchronization.
Edits in the primary or another mirror stop a successful upstream
update from overwriting local work. Accepted replacements keep the existing
backup under `skill-update-backups` beside the database, then mirror to every
configured root.

## Migration and recovery

1. Pause scheduled updates with `THREADKEEPER_SKILL_UPDATE_INTERVAL_S=0` while
   reviewing a source. Preserve the existing lock and any local edits.
2. For an older lock, keep its existing source fields and `tree_sha256`. Add
   `verification_policy` and, for signed sources, `expected_signer` to the
   primary lock. Leave `lock_version` absent until the updater migrates the
   old digest after a successful verification. For a new source, create the
   GitHub source fields and trust policy locally after reviewing the install.
3. Check the latest status reason. Install the extra for
   `verifier_unavailable`; restore the independently trusted PEM file for
   `trust_anchor_missing`. For signature, signer, commit, or digest failures,
   investigate the publisher and use a reviewed immutable ref. Do not remove a
   pin or enable unsigned updates just to clear a failed signature.
4. If a publisher rotates its signer, confirm the new identity independently
   before changing the primary lock. For a local-edit refusal, save the edits
   elsewhere and restore the same reviewed baseline across installed roots.
   A saved backup can restore that baseline. Do not replace the stored digest
   with an unreviewed local digest to bypass the guard.
5. Re-enable the interval and run a pass. A successful result writes the full
   v2 lock and copies it with the verified skill to every configured root.
   An explicit manual pass uses `run_skill_update_pass(force=True)`; forcing a
   pass bypasses cadence only, and keeps all trust and local-edit guards.

The detached layout follows [NVIDIA's skill verification documentation](https://docs.nvidia.com/skills/signing-agent-skills).
Verification uses the [OpenSSF Model Signing tooling](https://github.com/sigstore/model-transparency).
