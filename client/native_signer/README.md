# native_signer

Device-bound ECDSA signing for the Sky RTO agent — replaces the plaintext
`device_token` HMAC scheme with a private key that lives only inside the
OS's protected key storage (macOS Keychain / Windows CNG), never as raw
bytes in `config.json` or in this process's own memory.

## Status: scaffold, NOT verified on real hardware

This crate is written against the documented macOS Security-framework and
Windows CNG/NCrypt APIs, but the actual FFI call sequences in
`src/lib.rs`'s `macos_impl`/`windows_impl` modules are left as documented
stubs (returning a clear error) rather than guessed-at, untested code —
building this out and confirming it against a real Keychain and real CNG
key storage needs actual macOS and Windows hardware, which wasn't
available in the environment this was written in. **Do not wire this into
the production agent, or trust it with real device credentials, until
someone has:**

1. Filled in the real `SecKeyCreateRandomKey`/`SecKeyCreateSignature`
   calls in `macos_impl` and confirmed key generation + signing actually
   round-trips against a real login Keychain on a real Mac.
2. Filled in the real `NCryptCreatePersistedKey`/`NCryptSignHash` calls in
   `windows_impl` and confirmed the same on a real Windows machine.
3. Confirmed the generated public key really can't be used to derive or
   extract the private key (this should be true by construction — ECDSA
   is a one-way relationship — but verify the actual key attributes end
   up non-exportable as intended, not just non-exportable by omission).
4. If pursuing true Secure-Enclave-backed keys (strongest tier, hardware-
   isolated) rather than software-Keychain-backed: obtained a real Apple
   Developer Program signing identity and the Keychain-sharing/Secure-
   Enclave entitlement for this app. The current build pipeline's
   ad-hoc `codesign --sign -` (see `.github/workflows/build.yml`) is not
   sufficient for Secure Enclave access — falls back to software-Keychain
   storage (still non-exportable, just not hardware-isolated) until that's
   set up.

## Build (once the stubs are filled in)

```bash
pip install maturin
cd client/native_signer
maturin develop --release   # builds and installs into the active venv
```

Produces a Python-importable `native_signer` module exposing:

- `generate_device_keypair() -> str` — base64 public key (call once, at
  first agent startup on a new device)
- `sign_message(message_b64: str) -> str` — base64 signature over the
  given base64 message bytes
- `get_public_key() -> str` — re-fetch the public key on subsequent runs

## What this does and doesn't fix

See the module doc comment at the top of `src/lib.rs` — short version:
closes the "read plaintext token, forge requests with a standalone
script" attack class (every credential-based finding in the 2026-09
security review). Does **not** and cannot stop a device owner from
signing a payload built from fabricated network signals, or from spoofing
OS-level network config (fake IP alias, fake DNS) — those need
server-side verification, which is handled separately in
`server/detection.py`.
