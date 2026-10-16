# Identity and Transaction Authorization

This note describes the identity controls in the current transaction flow.
The gateway derives required factors from the stored receipt and validates
them independently of the browser.

## Short answer for Pop!_OS

The application can run on Pop!_OS, including device-bound signing and face
verification. It cannot use Microsoft Windows Hello because Windows Hello is
a Windows service.

On Linux, the browser checks whether it exposes a user-verifying **platform**
WebAuthn authenticator. The current client requests
`authenticatorAttachment: "platform"`, so it does not ask for a roaming USB
security key. If Chromium reports no platform authenticator, the application
uses its `MOCK_DEVICE_KEY` fallback:

- the browser generates an ECDSA P-256 key pair with Web Crypto;
- the private key is non-exportable and remains in that browser's IndexedDB;
- the gateway stores the public key and binds it to the KYC profile;
- each approval signs a one-time challenge tied to the exact receipt digest;
- clearing that browser profile can make the credential unavailable.

This fallback proves possession of the enrolled browser key, but it is not a
hardware-backed passkey and does not invoke a Linux biometric or PIN prompt.
High-value transfers and investment transactions also require face
verification before the signature.

Use `http://localhost:<port>`. The client deliberately rejects IP-address
relying parties such as `http://127.0.0.1:<port>` for platform WebAuthn and
falls back to the mock key.

## Control flow

```text
Mock Myinfo retrieval and consent
             |
             v
Register face template -------> activate it when KYC completes
             |
             v
Register one device credential -> bind it to the customer profile
             |
             v
Draft transaction -> policy checks -> immutable receipt + digest
                                          |
                          +---------------+----------------+
                          |                                |
                    under S$200                     S$200 or more
              “yes, confirm” mock phrase       mock Singpass + live face
                          |                                |
                          +------- device signature -------+
                                          |
                         re-check requirements and settle
```

For an investment order, every amount uses mock Singpass, face, and device
signature. Singpass is also required for transfer receipts totaling S$200 or
more. A friend-transfer batch uses its combined receipt total.

## 1. Mock Singpass Myinfo KYC onboarding

**What it does.** The UI retrieves a fixed synthetic Myinfo person, displays
the attributes requested, obtains consent, registers a face, and registers a
device credential. The gateway then marks KYC as verified for 365 days and
binds the credential to the profile.

**Implementation.** `apps/dimon_live/kyc.py` creates a random consent with a
10-minute expiry, hashes the returned person data, rejects missing, expired,
or reused consent, and completes KYC in the same flow as credential
registration. `apps/dimon_live/app.py` exposes `/api/kyc/myinfo`,
`/api/kyc/complete`, and `/api/kyc`. `apps/dimon_live/static/app.js` drives
the onboarding UI.

**Evidence.** `apps/dimon_live/tests/test_api.py` covers mandatory onboarding,
single-use consent, and origin checks. This is a mock service with a synthetic
person; it does not integrate with production Singpass or Myinfo.

## 2. Passkey and device-bound signing

**What it does.** The model can create a draft but cannot settle it. The
gateway accepts settlement only after the KYC-bound credential signs a
short-lived, one-time challenge derived from the purpose, challenge ID,
receipt digest, and random nonce.

**Browser implementation.** `apps/dimon_live/static/device_approval.mjs`
provides two credential types:

- `WEBAUTHN_PLATFORM`: calls `navigator.credentials.create()` and `.get()`
  with user verification required. On Windows this can invoke Windows Hello.
- `MOCK_DEVICE_KEY`: creates a non-exportable P-256 key in Web Crypto and
  stores the `CryptoKey` in IndexedDB. It produces the same assertion envelope
  so the server verifies both paths consistently.

**Gateway implementation.** `apps/dimon_live/approval_gateway.py` validates
the origin, relying-party hash, challenge, user-presence and user-verification
flags, algorithm, and signature. `apps/dimon_live/signing_store.py` persists
credentials and challenges, rejects expiry and replay, requires the one
credential bound during KYC, and records the verified signature with the
receipt.

**Evidence.** `test_signed_approval.py` covers genuine signatures, unknown
keys, wrong origins and relying parties, altered client data, receipt swapping,
and challenge replay. `test_device_approval_client.py` exercises the browser
fallback and proves its signature cannot authorize another challenge.

## 3. Face registration during onboarding

**What it does.** KYC cannot finish without a successful face enrollment.
The service converts accepted frames into a normalized 128-number face
template and stores it in a separate SQLite face database. It stores hashes
and scan metadata, not the submitted camera images.

**Implementation.** `apps/dimon_live/face_id.py` uses OpenCV YuNet for face
detection and OpenCV SFace for embeddings and comparison. The browser capture
code lives in `apps/dimon_live/static/face_capture.mjs`. The enrollment is
tied to the KYC consent and activated during KYC completion.

**Evidence.** `test_face_id.py` proves KYC rejects missing face enrollment and
tests template creation, matching, and engine-unavailable behavior.

## 4. Face verification for high-value transfers and investments

**What it does.** The confirmation dialog issues a face scan for the exact
approval challenge and receipt digest for friend-transfer receipts totaling
S$200 or more, and for every investment transaction. The gateway consumes a
successful match once, in the same settlement transaction as the device
signature.

**Implementation.** `FaceStore.verify()` compares current embeddings with the
active account template. The database records a match against its challenge
and digest. Settlement refuses a missing, mismatched, reused, or
already-consumed face confirmation when the transaction tier requires one.

**Evidence.** Face verification remains one-use and bound to the receipt
challenge. A missing face blocks transfers at or above S$200 and all
investment transactions. Friend transfers below S$200 use the mock affirmation
without a face match.

## 5. Head-movement liveness and replay checks

**What it does.** Enrollment and verification require a short sequence of
frames containing exactly one consistent face. The implementation estimates
head turn from face landmarks and requires enough variation. It also rejects
duplicate frames and hashes previously accepted frames to prevent reuse.

**Implementation.** `apps/dimon_live/face_id.py` enforces frame-count and size
bounds, one face per frame, embedding continuity, yaw spread, and unique frame
hashes. It rejects still images, multiple faces, absent faces, identity swaps,
and replayed frames with stable reason codes.

**Evidence.** `test_face_id.py` covers `FACE_NOT_LIVE`,
`FACE_MULTIPLE_FACES`, `FACE_NOT_FOUND`, `FACE_INCONSISTENT`,
`FACE_FRAME_COUNT`, and `FACE_REPLAYED`.

**Limit.** This is challenge-demo liveness based on landmark motion. It is not
a certified presentation-attack detector and should not be described as bank-
grade anti-spoofing.

## 6. One face, one bound device, and lockout

**What it does.** An account has one active face template and one active
credential. Re-onboarding must match the existing face. Successful new-device
onboarding revokes older credentials. Five failed verification scans within
10 minutes lock face verification for the remainder of that window.

**Implementation.** `kyc.complete_onboarding()` sets
`bound_credential_id` and revokes the other active credentials.
`signing_store.list_credentials()` returns only that bound credential.
`FaceStore` retains one template per user and counts recent failed scans.

**Evidence.** `test_face_id.py` covers account-face mismatch and lockout;
`test_api.py` and `test_signed_approval.py` cover credential binding and
replacement behavior.

## 7. Mock Singpass authorization for investments and high-value transfers

**What it does.** Buy, sell, and recurring-plan creation require Singpass at
every amount. Friend-transfer receipts totaling S$200 or more require it too.
The mock app shows the transaction title, amount, and a two-digit match code.

**Implementation.** `apps/dimon_live/singpass.py` creates an authorization
bound to the order or receipt ID and digest, signs the decision with
HMAC-SHA256, expires it after five minutes, and permits one use.
`apps/dimon_live/database.py` re-verifies and consumes it inside the matching
transaction settlement.
`apps/dimon_live/static/mock_singpass.html` supplies the demo UI.

**Evidence.** `test_investing.py` covers missing, rejected, expired,
cross-order, tampered, replayed, and non-Myinfo authorization cases.

**Scope.** Transfer batches use the sum stored in the batch receipt. Utility
bill proposals are read-only and display the tier their own amount would
require. When one user intent contains both a grounded bill amount and a friend
transfer, the bill amount is included in the transfer's authentication
threshold and bound into its receipt for approval-time rechecking. The bill
remains read-only; no bill payment can execute today.

## 8. Mock low-value voice affirmation

Friend transfers under S$200 require the visible phrase “yes, confirm” and a
device signature. The UI asks the user to say the phrase and check a box; the
server validates the exact phrase value alongside the receipt-bound device
signature. This is a mock user affirmation only: the app does not capture
audio for this step and does not match the speaker's identity. A teammate can
replace this boundary with voice recognition later without adding speaker
identity to the current implementation.

## Models and data required

Identity needs two small ONNX model files in addition to the Python OpenCV
package:

| Purpose | Model | Default filename | Pinned size and SHA-256 |
| --- | --- | --- | --- |
| Face detection | OpenCV Zoo YuNet | `face_detection_yunet_2023mar.onnx` | 232,589 bytes; `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| Face embedding/matching | OpenCV Zoo SFace | `face_recognition_sface_2021dec.onnx` | 38,696,353 bytes; `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |

`DIMON_FACE_DETECTOR_PATH` and `DIMON_FACE_RECOGNIZER_PATH` can override their
locations. The Windows installer downloads pinned copies with SHA-256 checks.
On Linux, place verified copies at the configured paths before testing real
camera enrollment. Unit tests use a fake face engine and do not need them.

Passkey signing does not use an AI model. WebAuthn or Web Crypto supplies the
key operations.
