# native_signer

Device-bound ECDSA signing for the Sky RTO agent — replaces the plaintext
`device_token` HMAC scheme with a private key that's never written to disk
as plaintext and can't be read by an unrelated script the way
`config.json` could be.

## Status: macOS — built, live-tested, working (with one open UX question)

### What was tried, and why the final design looks like this
Two Apple-provided storage tiers were tried first and both hit real,
confirmed platform walls (not coding mistakes) on real hardware:
- `kSecClassKey` + `Location::DefaultFileKeychain`: key generation
  succeeds, but lands on the legacy CDSA-backed implementation, which
  doesn't support any modern `SecKeyCreateSignature` algorithm variant
  (`OSStatus -50`, "algorithm not supported by the key").
- `kSecClassKey` + `Location::DataProtectionKeychain` (the modern,
  fully-capable, hardware-eligible path): fails at key generation with
  `OSStatus -34018` (`errSecMissingEntitlement`) — this tier requires a
  real Apple Developer Program code-signing identity + Keychain-access-
  group entitlement, which this app's ad-hoc `codesign --sign -` (see
  `.github/workflows/build.yml`) cannot provide, and there's no
  client-side workaround. **If Sky ever gets a Developer ID Application
  certificate, switch to this tier — it's strictly stronger (true
  hardware/Secure-Enclave-eligible, private key never enters process
  memory at all).**

### What's actually implemented and verified instead
Since neither `kSecClassKey` tier was usable, the key is generated in
Rust (`p256` crate, OS CSPRNG) and stored as a `kSecClassGenericPassword`
item — the plain "keychain item" type every macOS app can use without any
entitlement — protected by a **self-only Access Control List** built from
the classic (deprecated but functional) `SecAccessCreate` /
`SecTrustedApplicationCreateFromPath` API, since the modern
`security-framework` crate doesn't expose ACL construction at all.

**Live-tested end to end on real macOS hardware (2026-09), all confirmed working:**
1. `generate_device_keypair()` — generates and persists a P-256 key pair, ~0.05s, silent, no prompt.
2. `sign_message()` — signs, ~0.00s, silent, no prompt, called repeatedly.
3. The resulting signature verifies correctly server-side (`cryptography`'s `EllipticCurvePublicKey.from_encoded_point` + `ec.ECDSA(hashes.SHA256())` — see `server/deps.py::_verify_ecdsa_signature`).
4. A tampered message is correctly rejected by verification.
5. `get_public_key()` correctly re-fetches the same key across separate process invocations (persistence confirmed).
6. **The ACL genuinely blocks a different caller**: running `security find-generic-password -w` against the stored item (standing in for "a different unsigned script," exactly the attack class every credential-based finding in the 2026-09 review used) triggers a real macOS "Allow/Deny" authorization prompt. Clicking **Deny** returns exit code 128 with empty output — the key is not leaked. Clicking **Allow** (once) or **Allow Always** (permanently) grants that specific other caller access, same as any macOS Keychain prompt — this is standard OS behavior, not a bug in this code.

### Open item: silent auto-deny instead of a prompt
A prompt that the legitimate app never sees but the user *could*
accidentally click "Allow"/"Allow Always" on is real security, but not
as strong as it could be. A silent, no-prompt-at-all denial for
non-trusted callers is a documented, real macOS ACL capability
(`SecACLSetSimpleContents` with an empty `SecKeychainPromptSelector`),
and was attempted — it produced `OSStatus -67702`, decoded via
`SecCopyErrorMessageString` as **"An invalid ACL was encountered."**
Some combination of arguments passed to `SecACLSetSimpleContents` isn't
structurally valid on this macOS version, and resolving it needs real
Apple documentation for this deprecated CDSA-era API that wasn't
available when this was built. **This is a real, open, unsolved
improvement for whoever picks this back up** — not abandoned, just
blocked on documentation access. The reverted, working prompt-based
version is what's actually in `src/lib.rs` today.

### Trade-off, stated plainly
The private key bytes DO pass through this Rust process's memory during
generate/sign (the math happens here, not inside Apple's Security
daemon/hardware) — unlike a true Secure-Enclave key, this is not
provably non-extractable against a sufficiently resourced attacker with
a debugger attached at the right moment. What it does provide, verified
live: at-rest encryption (Keychain's own database encryption) and a real
access gate against a *different, unsigned, casually-run script* — which
is the actual threat model every finding in the 2026-09 security review
demonstrated.

## Status: Windows — scaffold, NOT built or verified

`windows_impl` in `src/lib.rs` is a documented stub (no real
`NCryptCreatePersistedKey`/`NCryptSignHash` calls) — no Windows machine
was available to write and test this against real CNG/TPM key storage.
One genuine reason for optimism, unverified: Windows' TPM-backed CNG key
APIs have no Apple-Developer-Program-style paid entitlement gate for
third-party apps, so the true hardware-backed tier that's blocked on
macOS may simply work on Windows once someone builds and tests it there.

## Build

```bash
pip install maturin
cd client/native_signer
maturin develop --release   # builds and installs into the active venv
```

Produces a Python-importable `native_signer` module exposing:

- `generate_device_keypair() -> str` — base64 public key (X9.63
  uncompressed point form — call once, at first agent enrollment on a
  new device)
- `sign_message(message_b64: str) -> str` — base64 DER-encoded ECDSA
  signature over the given base64 message bytes
- `get_public_key() -> str` — re-fetch the public key on subsequent runs

## Not yet wired into the shipped agent

`build.yml`'s PyInstaller step does not bundle this crate at all today —
`api.py`'s `import native_signer` is wrapped in `try/except ImportError`
and silently falls back to the existing HMAC `device_token` scheme, which
is what every currently-shipped binary actually uses. To actually ship
this:
1. Finish and verify the Windows side (or ship macOS-only initially, with
   the existing HMAC fallback covering Windows).
2. Add a `native_signer` build step to `build.yml` and bundle the
   resulting extension module into the PyInstaller output.
3. Wire `generate_device_keypair()` into the first-run registration flow
   (`checkin.py`/`auth.py`) so a new device's public key gets sent to
   `/api/register` automatically.
4. Decide on and resolve the silent-auto-deny open item above, or
   consciously accept the prompt-based version's "Allow Always" risk.

## What this does and doesn't fix

Closes the "read plaintext token, forge requests with a standalone
script" attack class — every credential-based finding in the 2026-09
security review used exactly that. Does **not** and cannot stop a device
owner from signing a payload built from fabricated network signals, or
from spoofing OS-level network config (fake IP alias, fake DNS) — those
need server-side verification, handled separately in
`server/detection.py`'s 2026-09 confidence-downgrade fix.
