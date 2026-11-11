"""Authenticate detached OMS skill bundles against locally chosen publishers."""
from __future__ import annotations

import base64
import json
from pathlib import Path

SIGNATURE_FILE = "skill.oms.sig"


def _covers_whole_tree(bundle: dict, root: Path) -> bool:
    payload = base64.b64decode(bundle["dsseEnvelope"]["payload"], validate=True)
    statement = json.loads(payload)
    predicate = statement["predicate"]
    serialization = predicate["serialization"]
    if serialization["method"] != "files" or serialization["hash_type"] != "sha256":
        return False
    resources = predicate["resources"]
    signed_files = {resource["name"] for resource in resources}
    actual_files = {p.relative_to(root).as_posix() for p in root.rglob("*")
                    if p.is_file() and p != root / SIGNATURE_FILE}
    return len(signed_files) == len(resources) and signed_files == actual_files


def _certificate_identity(bundle: dict) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    certificates = bundle["verificationMaterial"]["x509CertificateChain"]["certificates"]
    raw = base64.b64decode(certificates[0]["rawBytes"], validate=True)
    return x509.load_der_x509_certificate(raw).fingerprint(hashes.SHA256()).hex()


def _publisher_verifier(signer: dict, bundle: dict):
    from model_signing import verifying

    kind = signer.get("type")
    identity = signer.get("identity")
    if not isinstance(identity, str) or not identity:
        return None, "signer_required"
    if kind == "sigstore" and isinstance(signer.get("issuer"), str) and signer["issuer"]:
        return verifying.Config().use_sigstore_verifier(
            identity=identity, oidc_issuer=signer["issuer"],
        ), ""
    if kind == "certificate" and isinstance(signer.get("certificate_chain"), str):
        if _certificate_identity(bundle) != identity:
            return None, "signer_changed"
        chain = Path(signer["certificate_chain"]).expanduser()
        if not chain.is_absolute() or not chain.is_file():
            return None, "trust_anchor_missing"
        return verifying.Config().use_certificate_verifier(certificate_chain=[chain]), ""
    return None, "signer_required"


def verify_publisher(root: Path, record: dict) -> str:
    """Return a bounded public result; verifier exceptions may contain secrets."""
    policy = record.get("verification_policy")
    if not isinstance(policy, str) or policy not in {"require_signed", "allow_unsigned"}:
        return "policy_required"
    signer = record.get("expected_signer")
    signature = root / SIGNATURE_FILE
    if not signature.is_file():
        if policy == "allow_unsigned" and signer is None:
            return "unsigned_allowed"
        return "signature_missing"
    if not isinstance(signer, dict) or not signer:
        return "signer_required"
    try:
        from model_signing import hashing

        bundle = json.loads(signature.read_bytes())
        verifier, error = _publisher_verifier(signer, bundle)
        if error:
            return error
        if not _covers_whole_tree(bundle, root):
            return "digest_mismatch"
        hashing_config = hashing.Config().set_ignored_paths(
            paths=[Path(SIGNATURE_FILE)], ignore_git_paths=False,
        ).set_allow_symlinks(False)
        verifier.set_hashing_config(hashing_config).set_ignore_unsigned_files(False).verify(
            root, signature,
        )
    except ImportError:
        return "verifier_unavailable"
    except Exception:
        return "signature_invalid"
    return "verified"
