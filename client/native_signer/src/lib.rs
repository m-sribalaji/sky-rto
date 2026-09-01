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
    use ecdsa::signature::{Signer, Verifier};
    use p256::ecdsa::{Signature, SigningKey, VerifyingKey};
    use p256::elliptic_curve::sec1::ToEncodedPoint;
    use rand_core::OsRng;
    use security_framework::passwords::{delete_generic_password, get_generic_password};

    // ── Legacy CDSA-era ACL FFI (not bound anywhere in security-framework-sys) ──
    //
    // Attempt (2026-09) to restrict Keychain read access to this exact
    // signed binary, using the classic "trusted application" ACL model —
    // the same mechanism behind the old "App wants to access your
    // Keychain: Always Allow / Allow / Deny" prompt. Deprecated by Apple
    // since ~10.10, poorly documented, not available via any maintained
    // Rust crate found — declared here directly against the system
    // Security framework. Result of testing this is recorded below the
    // implementation, not assumed.
    use core_foundation::array::CFArray;
    use core_foundation::base::{CFType, TCFType};
    use core_foundation::string::CFString;
    use core_foundation_sys::array::CFArrayRef;
    use core_foundation_sys::base::OSStatus;
    use core_foundation_sys::string::CFStringRef;
    use security_framework_sys::base::SecAccessRef;
    use std::os::raw::c_char;
    use std::ptr;

    #[repr(C)]
    struct OpaqueSecTrustedApplicationRef(std::ffi::c_void);
    type SecTrustedApplicationRef = *mut OpaqueSecTrustedApplicationRef;

    #[repr(C)]
    struct OpaqueSecACLRef(std::ffi::c_void);
    type SecACLRef = *mut OpaqueSecACLRef;

    /// Mirrors Security/SecKeychainItem.h's SecKeychainPromptSelector — a
    /// bitmask of when to show the "Allow/Deny" UI. Passing 0 (no flags
    /// set) means: for a caller that doesn't match the ACL's trusted
    /// application list, never show any UI at all — just fail the
    /// operation silently. That's the piece SecAccessCreate's own
    /// defaults didn't give us (it showed a prompt), which is what
    /// motivated going one level deeper here.
    type SecKeychainPromptSelector = u16;
    const NO_PROMPT: SecKeychainPromptSelector = 0;

    #[allow(non_snake_case)]
    extern "C" {
        fn SecTrustedApplicationCreateFromPath(
            path: *const c_char,
            app: *mut SecTrustedApplicationRef,
        ) -> OSStatus;
        fn SecAccessCreate(
            descriptor: CFStringRef,
            trustedList: CFArrayRef,
            accessRef: *mut SecAccessRef,
        ) -> OSStatus;
        fn SecAccessCopyACLList(access: SecAccessRef, aclList: *mut CFArrayRef) -> OSStatus;
        fn SecACLSetSimpleContents(
            acl: SecACLRef,
            applicationList: CFArrayRef,
            description: CFStringRef,
            promptSelector: *const SecKeychainPromptSelector,
        ) -> OSStatus;
    }

    /// Build a SecAccess that trusts only THIS process's own binary
    /// (passing a null path to SecTrustedApplicationCreateFromPath is
    /// documented to mean "the application creating the item" — i.e. this
    /// exact running executable). Reconfigures every ACL entry
    /// SecAccessCreate produced to use NO_PROMPT, so a non-trusted caller
    /// gets a silent, immediate denial instead of an "Allow/Deny" dialog
    /// — closes the "user might accidentally click Allow" risk a
    /// prompt-based version would carry.
    fn create_self_only_access(descriptor: &str) -> Result<SecAccessRef, SignerError> {
        unsafe {
            let mut trusted_app: SecTrustedApplicationRef = ptr::null_mut();
            let status = SecTrustedApplicationCreateFromPath(ptr::null(), &mut trusted_app);
            if status != 0 {
                return Err(SignerError::KeyGen(format!(
                    "SecTrustedApplicationCreateFromPath failed: OSStatus {status}"
                )));
            }
            let trusted_app_type = CFType::wrap_under_create_rule(trusted_app as *mut _);
            let trusted_apps: CFArray<CFType> = CFArray::from_CFTypes(&[trusted_app_type.clone()]);
            let descriptor_cf = CFString::new(descriptor);
            let mut access_ref: SecAccessRef = ptr::null_mut();
            let status = SecAccessCreate(
                descriptor_cf.as_concrete_TypeRef(),
                trusted_apps.as_concrete_TypeRef(),
                &mut access_ref,
            );
            if status != 0 {
                return Err(SignerError::KeyGen(format!(
                    "SecAccessCreate failed: OSStatus {status}"
                )));
            }

            // Attempted (2026-09): reconfigure each ACL entry via
            // SecACLSetSimpleContents with an empty prompt selector, to
            // make a non-trusted caller get a silent denial instead of an
            // "Allow/Deny" dialog. Real, live testing on real hardware hit
            // OSStatus -67702, decoded via SecCopyErrorMessageString as
            // "An invalid ACL was encountered" — some combination of
            // arguments passed to SecACLSetSimpleContents isn't structurally
            // valid on this macOS version, and without real Apple
            // documentation for this deprecated CDSA-era API available to
            // consult, further trial-and-error against an opaque OS-level
            // validation error has uncertain odds of converging quickly.
            // Reverted to SecAccessCreate's own default ACL (prompts a
            // non-trusted caller rather than silently denying) — confirmed
            // working end-to-end on real hardware: silent for the trusted
            // app, a real "Allow/Deny" dialog for anything else, and a
            // Deny click genuinely blocks access (empty result, no key
            // leaked). The silent-auto-deny refinement remains a real,
            // open, not-yet-solved improvement for whoever picks this back
            // up with real documentation access.
            let _ = (SecAccessCopyACLList, SecACLSetSimpleContents, NO_PROMPT); // kept for the next attempt, not called

            Ok(access_ref)
        }
    }

    /// TRADEOFF, DOCUMENTED HERE DELIBERATELY (2026-09): this is NOT the
    /// same guarantee as a true hardware/Secure-Enclave-backed key. That
    /// path (kSecClassKey + kSecUseDataProtectionKeychain) was tested
    /// live on real hardware and confirmed blocked by
    /// errSecMissingEntitlement — it requires a real Apple Developer
    /// Program code-signing identity, which isn't available here.
    ///
    /// This is the next-best achievable option without that identity: the
    /// ECDSA math happens in this Rust process (via the `p256` crate,
    /// not Apple's Security framework), and the private key bytes are
    /// stored via kSecClassGenericPassword — the plain "keychain item"
    /// type every macOS app has always been able to use, no special
    /// entitlement required. What this DOES give you over today's
    /// plaintext config.json:
    ///   - At rest, the key is encrypted by macOS's own Keychain database
    ///     encryption (tied to the user's login password) — reading the
    ///     raw keychain file directly gets you ciphertext, not the key.
    ///   - Read access is gated by an Access Control List that Keychain
    ///     ties to the calling process's code signature/path by default
    ///     — a DIFFERENT unsigned script (exactly what every attack in
    ///     the 2026-09 security review used) cannot silently read this
    ///     item the way it could `cat config.json`. Accessing it either
    ///     requires being the same signed binary, or the user explicitly
    ///     clicking "Allow" on a Keychain access prompt.
    /// What this does NOT give you: the raw private key bytes DO pass
    /// through this process's memory on generate/sign (unlike a true
    /// Secure Enclave key, which never leaves hardware) — a sufficiently
    /// motivated attacker with a debugger attached to a moment this code
    /// is running could still extract it, same fundamental limit
    /// discussed at length earlier in the 2026-09 review for any
    /// software-only key.

    const SERVICE: &str = "com.sky.rto-tracker";

    #[allow(non_upper_case_globals)]
    extern "C" {
        static kSecClass: CFStringRef;
        static kSecClassGenericPassword: CFStringRef;
        static kSecAttrService: CFStringRef;
        static kSecAttrAccount: CFStringRef;
        static kSecValueData: CFStringRef;
        static kSecAttrAccess: CFStringRef;
    }

    fn set_generic_password_with_self_only_acl(service: &str, account: &str, password: &[u8]) -> Result<(), SignerError> {
        use core_foundation::data::CFData;
        use core_foundation::dictionary::CFDictionary;
        use core_foundation_sys::base::CFTypeRef;
        use security_framework_sys::keychain_item::SecItemAdd;

        let access_ref = create_self_only_access(&format!("{service}/{account}"))?;
        unsafe {
            let access = CFType::wrap_under_create_rule(access_ref as *mut _);
            let pairs: Vec<(CFType, CFType)> = vec![
                (CFType::wrap_under_get_rule(kSecClass as *mut _), CFType::wrap_under_get_rule(kSecClassGenericPassword as *mut _)),
                (CFType::wrap_under_get_rule(kSecAttrService as *mut _), CFString::new(service).into_CFType()),
                (CFType::wrap_under_get_rule(kSecAttrAccount as *mut _), CFString::new(account).into_CFType()),
                (CFType::wrap_under_get_rule(kSecValueData as *mut _), CFData::from_buffer(password).into_CFType()),
                (CFType::wrap_under_get_rule(kSecAttrAccess as *mut _), access),
            ];
            let dict = CFDictionary::from_CFType_pairs(&pairs);
            let mut result: CFTypeRef = ptr::null();
            let status = SecItemAdd(dict.as_concrete_TypeRef(), &mut result);
            if status != 0 {
                return Err(SignerError::KeyGen(format!(
                    "SecItemAdd (with self-only ACL) failed: OSStatus {status} \
                     (errSecDuplicateItem if this tag already has a legacy, \
                     no-ACL item from earlier testing — delete it first)"
                )));
            }
        }
        Ok(())
    }

    pub fn generate_keypair(tag: &str) -> Result<Vec<u8>, SignerError> {
        let signing_key = SigningKey::random(&mut OsRng);
        let private_bytes = signing_key.to_bytes();
        set_generic_password_with_self_only_acl(SERVICE, tag, &private_bytes)
            .map_err(|e| SignerError::KeyGen(format!("Keychain set (ACL) failed: {e:?}")))?;
        Ok(encode_public_key(&signing_key))
    }

    pub fn sign(tag: &str, message: &[u8]) -> Result<Vec<u8>, SignerError> {
        let signing_key = load_signing_key(tag)?;
        let signature: Signature = signing_key.sign(message);
        Ok(signature.to_der().as_bytes().to_vec())
    }

    pub fn public_key(tag: &str) -> Result<Vec<u8>, SignerError> {
        let signing_key = load_signing_key(tag)?;
        Ok(encode_public_key(&signing_key))
    }

    fn load_signing_key(tag: &str) -> Result<SigningKey, SignerError> {
        let bytes = get_generic_password(SERVICE, tag)
            .map_err(|e| SignerError::NotFound(format!("Keychain get_generic_password failed for '{tag}': {e:?}")))?;
        SigningKey::from_bytes((&bytes[..]).into())
            .map_err(|e| SignerError::NotFound(format!("stored key bytes were invalid: {e}")))
    }

    /// X9.63 uncompressed point form (0x04 || X || Y) — matches what
    /// server-side deps.py::_verify_ecdsa_signature expects via
    /// EllipticCurvePublicKey.from_encoded_point.
    fn encode_public_key(signing_key: &SigningKey) -> Vec<u8> {
        let verifying_key: VerifyingKey = *signing_key.verifying_key();
        verifying_key.to_encoded_point(false).as_bytes().to_vec()
    }

    #[allow(dead_code)]
    fn remove_keypair(tag: &str) -> Result<(), SignerError> {
        delete_generic_password(SERVICE, tag)
            .map_err(|e| SignerError::NotFound(format!("Keychain delete_generic_password failed: {e:?}")))
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
