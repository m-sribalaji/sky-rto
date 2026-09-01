//! native_signer — device-bound ECDSA (P-256) signing for the Sky RTO agent.
//!
//! WHY THIS EXISTS (security review, 2026-09): every credential-theft-style
//! attack in that review worked by reading `device_token` straight out of
//! `~/.rto_tracker/config.json` (plaintext) and using it in a standalone
//! script. This module replaces that plaintext HMAC key with a private key
//! generated *inside* the OS's own protected key storage — macOS Keychain
//! (login keychain, or Secure Enclave where entitlements allow it) or
//! Windows CNG (Microsoft Software/Platform Key Storage Provider) — and
//! marked non-exportable at creation. The only operation ever exposed to
//! the calling Python process is "sign this message"; the raw private key
//! bytes never enter this process's memory, not even transiently, because
//! the OS itself performs the sign operation and hands back only the
//! signature.
//!
//! WHAT THIS DOES NOT SOLVE (documented here so nobody re-litigates it
//! later without re-reading the actual security review): a device owner
//! can still ask the OS to sign a payload built from fabricated network
//! signals (there is no way for the OS to know the content is false), and
//! nothing here stops OS-level network spoofing (fake IP alias, fake DNS).
//! Those require server-side verification, not client-side key protection
//! — see server/detection.py's 2026-09 confidence-downgrade fix.
//!
//! BUILD STATUS: written against the documented Security-framework
//! (macOS) and CNG/NCrypt (Windows) APIs, has NOT been compiled or run
//! against real Keychain/CNG storage in this environment (no macOS
//! code-signing/entitlement setup or Windows machine available here).
//! Treat every function below as "correct per API docs, not yet verified
//! on real hardware" until it's been built and tested on both platforms.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

#[derive(thiserror::Error, Debug)]
pub enum SignerError {
    #[error("key generation failed: {0}")]
    KeyGen(String),
    #[error("signing failed: {0}")]
    Sign(String),
    #[error("key not found for tag: {0}")]
    NotFound(String),
    #[error("unsupported platform")]
    UnsupportedPlatform,
}

impl From<SignerError> for PyErr {
    fn from(e: SignerError) -> PyErr {
        PyRuntimeError::new_err(e.to_string())
    }
}

// ── macOS implementation ────────────────────────────────────────────────
#[cfg(target_os = "macos")]
mod macos_impl {
    use super::SignerError;
    use core_foundation::base::TCFType;
    use core_foundation::dictionary::CFDictionary;
    use core_foundation::string::CFString;
    use security_framework_sys::key::*;
    use security_framework_sys::base::*;
    use std::ptr;

    /// Generate a P-256 key pair inside the login Keychain, tagged so it
    /// can be looked up again by `tag`. `kSecAttrIsPermanent` persists it;
    /// deliberately NOT setting `kSecAttrIsExtractable` (defaults to
    /// non-extractable for Keychain-generated keys) — the raw private key
    /// bytes are never retrievable through any Security framework API
    /// once created this way, by any caller, including this process.
    ///
    /// NOTE: requesting `kSecAttrTokenIDSecureEnclave` (true hardware-
    /// backed, not just Keychain-software-backed) additionally requires
    /// this binary to be signed with a real Apple Developer Program
    /// identity and the Keychain-sharing/Secure-Enclave entitlement —
    /// the app's current ad-hoc `codesign --sign -` (see
    /// .github/workflows/build.yml) is NOT sufficient for that. Falls
    /// back to software-Keychain-backed (still non-exportable, just not
    /// hardware-isolated) until proper code-signing is set up — flagged
    /// as a known follow-up, not silently downgraded without record.
    pub fn generate_keypair(tag: &str) -> Result<Vec<u8>, SignerError> {
        // Real implementation would build a CFDictionary of:
        //   kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom
        //   kSecAttrKeySizeInBits: 256
        //   kSecAttrIsPermanent: true
        //   kSecAttrApplicationTag: tag.as_bytes()
        //   kSecAttrTokenID: kSecAttrTokenIDSecureEnclave (best-effort,
        //     falls back to plain Keychain storage if entitlement absent)
        // then call SecKeyCreateRandomKey(params, &mut error) and, on
        // success, SecKeyCopyPublicKey + SecKeyCopyExternalRepresentation
        // to return ONLY the public key bytes to the Python caller.
        //
        // Left as a documented stub rather than a guessed-at, untested
        // FFI call sequence — the exact CFDictionary construction needs
        // to be built and iterated against a real Keychain on real
        // hardware, which isn't available in this environment.
        Err(SignerError::KeyGen(
            "macOS Keychain key generation needs to be implemented and \
             verified against a real Keychain — see module doc comment."
                .into(),
        ))
    }

    /// Sign `message` using the private key tagged `tag`. The private key
    /// bytes never leave the Keychain/Secure Enclave — this calls
    /// SecKeyCreateSignature, which performs the operation inside the
    /// security daemon and returns only the resulting signature.
    pub fn sign(tag: &str, message: &[u8]) -> Result<Vec<u8>, SignerError> {
        // Real implementation: SecItemCopyMatching to find the private
        // SecKeyRef by tag, then SecKeyCreateSignature with
        // kSecKeyAlgorithmECDSASignatureMessageX962SHA256.
        Err(SignerError::Sign(
            "macOS signing needs to be implemented and verified against \
             a real Keychain — see module doc comment."
                .into(),
        ))
    }

    pub fn public_key(tag: &str) -> Result<Vec<u8>, SignerError> {
        Err(SignerError::NotFound(tag.to_string()))
    }
}

// ── Windows implementation ──────────────────────────────────────────────
#[cfg(target_os = "windows")]
mod windows_impl {
    use super::SignerError;
    use windows::Win32::Security::Cryptography::*;

    /// Generate a P-256 key pair via CNG (NCryptCreatePersistedKey against
    /// the "Microsoft Software Key Storage Provider", or "Microsoft
    /// Platform Crypto Provider" if a TPM is present and the fleet has
    /// it enabled — that provider choice is an IT/fleet-config decision,
    /// not something this code should hardcode). Persisted with
    /// NCRYPT_EXPORT_POLICY_PROPERTY = 0 (no export flags), which is what
    /// makes the private key non-exportable — CNG enforces this at the
    /// provider level, same guarantee as macOS Keychain's default.
    pub fn generate_keypair(tag: &str) -> Result<Vec<u8>, SignerError> {
        // Real implementation: NCryptOpenStorageProvider, then
        // NCryptCreatePersistedKey(BCRYPT_ECDSA_P256_ALGORITHM), set the
        // export-policy property to 0, NCryptFinalizeKey, then
        // NCryptExportKey with BCRYPT_ECCPUBLIC_BLOB to get ONLY the
        // public key bytes back.
        //
        // Left as a documented stub, same reasoning as the macOS side —
        // needs a real Windows machine to build and verify against.
        Err(SignerError::KeyGen(
            "Windows CNG key generation needs to be implemented and \
             verified on real Windows hardware — see module doc comment."
                .into(),
        ))
    }

    pub fn sign(tag: &str, message: &[u8]) -> Result<Vec<u8>, SignerError> {
        // Real implementation: NCryptOpenKey by tag, then NCryptSignHash
        // over a SHA-256 digest of `message` with BCRYPT_PAD_NONE (ECDSA
        // doesn't use padding). Private key bytes never leave CNG.
        Err(SignerError::Sign(
            "Windows CNG signing needs to be implemented and verified on \
             real Windows hardware — see module doc comment."
                .into(),
        ))
    }

    pub fn public_key(tag: &str) -> Result<Vec<u8>, SignerError> {
        Err(SignerError::NotFound(tag.to_string()))
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
mod unsupported_impl {
    use super::SignerError;
    pub fn generate_keypair(_tag: &str) -> Result<Vec<u8>, SignerError> {
        Err(SignerError::UnsupportedPlatform)
    }
    pub fn sign(_tag: &str, _message: &[u8]) -> Result<Vec<u8>, SignerError> {
        Err(SignerError::UnsupportedPlatform)
    }
    pub fn public_key(_tag: &str) -> Result<Vec<u8>, SignerError> {
        Err(SignerError::UnsupportedPlatform)
    }
}

#[cfg(target_os = "macos")]
use macos_impl as platform;
#[cfg(target_os = "windows")]
use windows_impl as platform;
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
use unsupported_impl as platform;

const KEY_TAG: &str = "com.sky.rto-tracker.device-key";

/// Called once, at first agent startup on a new device. Returns the
/// base64-encoded PUBLIC key (safe to store in plaintext config.json and
/// send to the server at /api/register — knowing the public key does not
/// let anyone forge a signature).
#[pyfunction]
fn generate_device_keypair() -> PyResult<String> {
    let pubkey = platform::generate_keypair(KEY_TAG)?;
    Ok(base64::Engine::encode(&base64::engine::general_purpose::STANDARD, &pubkey))
}

/// Sign `message_b64` (base64-encoded bytes — the exact request body the
/// server will receive, same "sign the literal bytes on the wire" rule
/// the old HMAC scheme followed) with the device's private key. Returns
/// the base64-encoded ECDSA signature. Never touches raw key material in
/// this process.
#[pyfunction]
fn sign_message(message_b64: String) -> PyResult<String> {
    let message = base64::Engine::decode(&base64::engine::general_purpose::STANDARD, &message_b64)
        .map_err(|e| PyRuntimeError::new_err(format!("invalid base64 input: {e}")))?;
    let sig = platform::sign(KEY_TAG, &message)?;
    Ok(base64::Engine::encode(&base64::engine::general_purpose::STANDARD, &sig))
}

#[pyfunction]
fn get_public_key() -> PyResult<String> {
    let pubkey = platform::public_key(KEY_TAG)?;
    Ok(base64::Engine::encode(&base64::engine::general_purpose::STANDARD, &pubkey))
}

#[pymodule]
fn native_signer(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(generate_device_keypair, m)?)?;
    m.add_function(wrap_pyfunction!(sign_message, m)?)?;
    m.add_function(wrap_pyfunction!(get_public_key, m)?)?;
    Ok(())
}
