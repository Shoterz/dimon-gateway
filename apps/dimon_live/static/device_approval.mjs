// Device-bound approval: the browser half of the zero-trust gateway.
//
// The server only moves money for a signature over a one-time challenge that
// is bound to one receipt digest. Two signers produce that signature in the
// same WebAuthn assertion format:
//   - WEBAUTHN_PLATFORM: the OS authenticator (Windows Hello, Touch ID,
//     Face ID) with mandatory user verification — a real biometric or PIN.
//   - MOCK_DEVICE_KEY: a labelled stand-in permitted by the brief. A
//     non-extractable P-256 key generated and kept in this browser; the
//     "biometric" is a deliberate press-and-hold on the confirmation overlay.

const encoder = new TextEncoder();
const FLAGS_PRESENT_AND_VERIFIED = 0x05;
const KEY_DB = "dimon-device-keys";
const KEY_STORE = "keys";

export function b64urlEncode(bytes) {
  const view = bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let binary = "";
  for (const byte of view) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlDecode(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function concat(...parts) {
  const total = parts.reduce((sum, part) => sum + part.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(new Uint8Array(part), offset);
    offset += part.byteLength;
  }
  return out;
}

async function sha256(bytes, subtle) {
  return new Uint8Array(await subtle.digest("SHA-256", bytes));
}

function isIpAddress(host) {
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.includes(":");
}

// Per-browser key storage. IndexedDB keeps the CryptoKey object itself, so the
// private key is never exported; tests and non-browser runtimes use memory.
export function memoryKeyStore() {
  const keys = new Map();
  return {
    async get(id) { return keys.get(id) || null; },
    async put(id, value) { keys.set(id, value); },
  };
}

export function indexedDbKeyStore(indexedDB = globalThis.indexedDB) {
  if (!indexedDB) return memoryKeyStore();
  const open = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(KEY_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(KEY_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const run = async (mode, action) => {
    const db = await open();
    try {
      return await new Promise((resolve, reject) => {
        const request = action(db.transaction(KEY_STORE, mode).objectStore(KEY_STORE));
        request.onsuccess = () => resolve(request.result ?? null);
        request.onerror = () => reject(request.error);
      });
    } finally {
      db.close();
    }
  };
  return {
    get: id => run("readonly", store => store.get(id)),
    put: (id, value) => run("readwrite", store => store.put(value, id)),
  };
}

export class DeviceApprover {
  constructor({
    // Wrapped: browsers reject fetch called as a method of another object.
    fetchImpl = (...args) => globalThis.fetch(...args),
    subtle = globalThis.crypto?.subtle,
    credentials = globalThis.navigator?.credentials,
    PublicKeyCredential = globalThis.PublicKeyCredential,
    location = globalThis.location,
    keyStore = indexedDbKeyStore(),
    getRandomValues = bytes => globalThis.crypto.getRandomValues(bytes),
  } = {}) {
    this.fetch = fetchImpl;
    this.subtle = subtle;
    this.credentials = credentials;
    this.PublicKeyCredential = PublicKeyCredential;
    this.origin = location.origin;
    this.host = location.hostname;
    this.keyStore = keyStore;
    this.getRandomValues = getRandomValues;
  }

  async post(url, body) {
    const response = await this.fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.detail || `HTTP ${response.status}`);
      error.reasonCode = typeof payload.detail === "string" ? payload.detail : "HTTP_ERROR";
      throw error;
    }
    return payload;
  }

  // WebAuthn forbids IP-address relying parties, so http://127.0.0.1 falls
  // back to the mock device key; http://localhost can use Windows Hello.
  async platformAvailable() {
    if (!this.PublicKeyCredential || !this.credentials || isIpAddress(this.host)) return false;
    try {
      return await this.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
    } catch (_error) {
      return false;
    }
  }

  async usableCredential(serverCredentials) {
    for (const credential of serverCredentials) {
      if (credential.kind === "WEBAUTHN_PLATFORM" && await this.platformAvailable()) return credential;
      if (credential.kind === "MOCK_DEVICE_KEY" && await this.keyStore.get(credential.credential_id)) return credential;
    }
    return null;
  }

  async enroll({preferPlatform = true} = {}) {
    const challenge = await this.post("/api/signing/registration-challenge");
    if (preferPlatform && await this.platformAvailable()) {
      return this.enrollPlatform(challenge);
    }
    return this.enrollMock(challenge);
  }

  async enrollPlatform(challenge) {
    const credential = await this.credentials.create({
      publicKey: {
        challenge: b64urlDecode(challenge.challenge),
        rp: {id: challenge.rp_id, name: "Dimon Gateway (synthetic ledger)"},
        user: {
          id: encoder.encode(challenge.user.id),
          name: challenge.user.name,
          displayName: challenge.user.name,
        },
        pubKeyCredParams: [{type: "public-key", alg: -7}, {type: "public-key", alg: -257}],
        authenticatorSelection: {
          authenticatorAttachment: "platform",
          userVerification: "required",
          residentKey: "discouraged",
        },
        attestation: "none",
        timeout: 60000,
      },
    });
    const response = credential.response;
    return this.post("/api/signing/credentials", {
      challenge_id: challenge.challenge_id,
      credential_kind: "WEBAUTHN_PLATFORM",
      credential_id: b64urlEncode(credential.rawId),
      public_key_spki: b64urlEncode(response.getPublicKey()),
      algorithm: response.getPublicKeyAlgorithm(),
      client_data_json: b64urlEncode(response.clientDataJSON),
      authenticator_data: b64urlEncode(response.getAuthenticatorData()),
    });
  }

  async mockAuthenticatorData(rpId, counter) {
    const counterBytes = new Uint8Array(4);
    new DataView(counterBytes.buffer).setUint32(0, counter);
    return concat(await sha256(encoder.encode(rpId), this.subtle), Uint8Array.of(FLAGS_PRESENT_AND_VERIFIED), counterBytes);
  }

  async mockSign(privateKey, authenticatorData, clientData) {
    const signed = concat(authenticatorData, await sha256(clientData, this.subtle));
    return new Uint8Array(await this.subtle.sign({name: "ECDSA", hash: "SHA-256"}, privateKey, signed));
  }

  async enrollMock(challenge) {
    const keyPair = await this.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"}, false, ["sign"]);
    const credentialId = b64urlEncode(this.getRandomValues(new Uint8Array(16)));
    const clientData = encoder.encode(JSON.stringify({
      type: "webauthn.create",
      challenge: challenge.challenge,
      origin: this.origin,
    }));
    const authenticatorData = await this.mockAuthenticatorData(challenge.rp_id, 0);
    const result = await this.post("/api/signing/credentials", {
      challenge_id: challenge.challenge_id,
      credential_kind: "MOCK_DEVICE_KEY",
      credential_id: credentialId,
      public_key_spki: b64urlEncode(await this.subtle.exportKey("spki", keyPair.publicKey)),
      algorithm: -7,
      client_data_json: b64urlEncode(clientData),
      authenticator_data: b64urlEncode(authenticatorData),
      signature: b64urlEncode(await this.mockSign(keyPair.privateKey, authenticatorData, clientData)),
    });
    await this.keyStore.put(credentialId, {privateKey: keyPair.privateKey, counter: 0});
    return result;
  }

  // Fetch the one-time challenge and the server-stored review facts. Returns
  // {kind: "BLOCKED", result} when the gateway refuses to issue one.
  async requestChallenge(collection, receipt) {
    const response = await this.post(`/api/${collection}/${receipt.receipt_id}/challenge`, {
      receipt_digest: receipt.receipt_digest,
    });
    if (response.outcome === "BLOCKED") return {kind: "BLOCKED", result: response};
    return {kind: "CHALLENGE", challenge: response};
  }

  async signChallenge(challenge) {
    const credential = await this.usableCredential(challenge.allow_credentials);
    if (!credential) {
      const error = new Error("No usable signing credential on this device");
      error.reasonCode = "SIGNING_CREDENTIAL_UNAVAILABLE";
      throw error;
    }
    if (credential.kind === "WEBAUTHN_PLATFORM") {
      const assertion = await this.credentials.get({
        publicKey: {
          challenge: b64urlDecode(challenge.challenge),
          rpId: this.host,
          allowCredentials: [{type: "public-key", id: b64urlDecode(credential.credential_id)}],
          userVerification: "required",
          timeout: 60000,
        },
      });
      return {
        challenge_id: challenge.challenge_id,
        credential_id: b64urlEncode(assertion.rawId),
        client_data_json: b64urlEncode(assertion.response.clientDataJSON),
        authenticator_data: b64urlEncode(assertion.response.authenticatorData),
        signature: b64urlEncode(assertion.response.signature),
        method: "WEBAUTHN_PLATFORM",
      };
    }
    const stored = await this.keyStore.get(credential.credential_id);
    const counter = stored.counter + 1;
    await this.keyStore.put(credential.credential_id, {...stored, counter});
    const clientData = encoder.encode(JSON.stringify({
      type: "webauthn.get",
      challenge: challenge.challenge,
      origin: this.origin,
    }));
    const authenticatorData = await this.mockAuthenticatorData(this.host, counter);
    return {
      challenge_id: challenge.challenge_id,
      credential_id: credential.credential_id,
      client_data_json: b64urlEncode(clientData),
      authenticator_data: b64urlEncode(authenticatorData),
      signature: b64urlEncode(await this.mockSign(stored.privateKey, authenticatorData, clientData)),
      method: "MOCK_DEVICE_KEY",
    };
  }
}

// Strip client-only metadata before the assertion goes on the wire.
export function wireAssertion(assertion) {
  const {method: _method, ...wire} = assertion;
  return wire;
}
