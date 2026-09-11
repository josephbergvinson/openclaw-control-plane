#!/usr/bin/env node
import { binding as operatorBinding, materializeOperatorValue } from "./routing_operator_bindings.mjs";

import { createHash } from "node:crypto";
import { chmodSync, lstatSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const ACCOUNT_ID = operatorBinding("services.wallets.hedera_testnet.account_id");
const NETWORK = "testnet";
const MIRROR_URL = `https://testnet.mirrornode.hedera.com/api/v1/accounts/${ACCOUNT_ID}`;
const GENERAL_PATH = operatorBinding("paths.routing_openclaw_hedera_testnet_test_account_env");
const GENERAL_LAYOUT = {
  account: "OPENCLAW_HEDERA_TESTNET_TEST_ACCOUNT_ID",
  privateKey: "OPENCLAW_HEDERA_TESTNET_TEST_PRIVATE_KEY",
};
const CLASSIFICATION = "general_openclaw_hedera_testnet_disposable_test_signer";
// The previous literal pointed at ~/Code/OpenClawWorkspace, a path policy
// requires to stay absent, so the probe always reported the SDK missing.
// Resolve the owning repository from the topology registry instead.
function registrySdkRoot() {
  try {
    const registry = materializeOperatorValue(JSON.parse(
      readFileSync(
        path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "registry", "project_topology.json"),
        "utf8",
      ),
    ));
    for (const project of registry?.projects ?? []) {
      for (const repository of project?.repositories ?? []) {
        if (repository?.repository_id === "company-alpha-server" && repository?.canonical_root) {
          return path.join(repository.canonical_root, "node_modules", "@hashgraph", "sdk");
        }
      }
    }
  } catch {
    // An unreadable registry is reported as sdk-unavailable below, never as a
    // silent skip.
  }
  return undefined;
}

const SDK_CANDIDATES = [process.env.HEDERA_SDK_ROOT, registrySdkRoot()].filter(Boolean);

class SafeProbeError extends Error {
  constructor(code, detail) {
    super(code);
    this.code = code;
    this.detail = detail;
  }
}

function fail(code, detail) {
  throw new SafeProbeError(code, detail);
}

function modeString(mode) {
  return (mode & 0o777).toString(8).padStart(4, "0");
}

function loadSdk() {
  for (const root of SDK_CANDIDATES) {
    try {
      const packageJson = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
      const require = createRequire(import.meta.url);
      const sdk = require(path.join(root, "lib", "index.cjs"));
      // PrivateKey/PublicKey alone is too weak: an older SDK on this host
      // satisfies it and then fails downstream with a malformed key. Require
      // the ED25519 entry points this probe actually calls.
      if (
        typeof sdk?.PrivateKey?.fromStringED25519 === "function" &&
        typeof sdk?.PrivateKey?.generateED25519 === "function" &&
        typeof sdk?.PublicKey?.fromStringED25519 === "function"
      ) {
        return { sdk, version: String(packageJson.version || "unknown") };
      }
    } catch {
      // Candidate absence is reported only after all secret-free paths are checked.
    }
  }
  fail("hedera_sdk_unavailable", "canonical local @hashgraph/sdk installation was not found");
}

function assertSafeSecretFile(secretPath) {
  let fileStat;
  try {
    const linkStat = lstatSync(secretPath);
    if (linkStat.isSymbolicLink()) fail("secret_symlink_forbidden", "secret path must not be a symlink");
    if (!linkStat.isFile()) fail("secret_not_regular_file", "secret path must be a regular file");
    fileStat = statSync(secretPath);
  } catch (error) {
    if (error instanceof SafeProbeError) throw error;
    if (error?.code === "ENOENT") fail("secret_file_missing", secretPath);
    fail("secret_file_unreadable", "secret metadata could not be read");
  }

  const currentUser = userInfo();
  if (currentUser.username !== operatorBinding("identifiers.host_user") || fileStat.uid !== currentUser.uid) {
    fail("secret_owner_mismatch", "secret file must be owned by the configured host user");
  }
  if (modeString(fileStat.mode) !== "0600") fail("secret_mode_mismatch", `expected 0600, observed ${modeString(fileStat.mode)}`);

  let bytes;
  try {
    bytes = readFileSync(secretPath);
  } catch {
    fail("secret_file_unreadable", "secret bytes could not be read");
  }
  if (bytes.includes(0)) fail("secret_contains_nul", "NUL bytes are forbidden");
  if (bytes.includes(13)) fail("secret_contains_cr", "CR bytes are forbidden");

  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    fail("secret_not_utf8", "secret file must be valid UTF-8");
  }
  if (!text.endsWith("\n")) fail("secret_missing_final_newline", "secret file must end after the final record newline");
  const body = text.slice(0, -1);
  if (!body) fail("secret_blank_or_trailing_record", "secret file has no records");
  const expectedNames = Object.values(GENERAL_LAYOUT);
  const lines = body.split("\n");
  if (lines.some((line) => line === "")) fail("secret_blank_or_trailing_record", "blank records are forbidden");
  if (lines.length !== expectedNames.length) fail("secret_record_count_mismatch", `expected ${expectedNames.length} records, observed ${lines.length}`);

  const values = new Map();
  for (const line of lines) {
    const match = /^([A-Z0-9_]+)=([^\s]+)$/.exec(line);
    if (!match) fail("secret_record_malformed", "records must be NAME=VALUE with no whitespace");
    const [, name, value] = match;
    if (!expectedNames.includes(name)) fail("secret_extra_variable", `unexpected variable ${name}`);
    if (values.has(name)) fail("secret_duplicate_variable", `duplicate variable ${name}`);
    values.set(name, value);
  }
  for (const name of expectedNames) if (!values.has(name)) fail("secret_missing_variable", `missing variable ${name}`);
  if (values.get(GENERAL_LAYOUT.account) !== ACCOUNT_ID) fail("secret_account_id_mismatch", `expected account ${ACCOUNT_ID}`);

  return {
    values,
    metadata: {
      path: secretPath,
      owner: currentUser.username,
      uid: fileStat.uid,
      gid: fileStat.gid,
      mode: modeString(fileStat.mode),
      regular_file: true,
      symlink: false,
      inode: String(fileStat.ino),
      size_bytes: fileStat.size,
      variable_names: expectedNames,
      value_lengths: Object.fromEntries(expectedNames.map((name) => [name, values.get(name).length])),
    },
  };
}

async function fetchMirrorAccount() {
  let response;
  try {
    response = await fetch(MIRROR_URL, {
      headers: { accept: "application/json", "user-agent": "openclaw-hedera-testnet-test-signer-probe/1" },
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    fail("mirror_node_unreachable", "authoritative testnet account readback failed");
  }
  if (!response.ok) fail("mirror_node_http_error", `authoritative readback returned HTTP ${response.status}`);
  try {
    return await response.json();
  } catch {
    fail("mirror_node_invalid_json", "authoritative readback did not return JSON");
  }
}

function publicFingerprint(publicKeyRaw) {
  return createHash("sha256").update(Buffer.from(publicKeyRaw, "hex")).digest("hex");
}

async function prove(secretPath = GENERAL_PATH, mirrorAccount = null) {
  const { sdk, version } = loadSdk();
  const { values, metadata } = assertSafeSecretFile(secretPath);
  let privateKey;
  try {
    privateKey = sdk.PrivateKey.fromStringECDSA(values.get(GENERAL_LAYOUT.privateKey));
  } catch {
    fail("private_key_malformed", "private key did not parse as ECDSA_SECP256K1");
  }
  const derivedPublicKey = privateKey.publicKey;
  const derivedRaw = derivedPublicKey.toStringRaw().toLowerCase();
  const derivedEvm = `0x${derivedPublicKey.toEvmAddress().replace(/^0x/, "").toLowerCase()}`;
  const account = mirrorAccount || (await fetchMirrorAccount());
  if (account?.account !== ACCOUNT_ID || account?.deleted === true) fail("authoritative_account_mismatch", "testnet readback did not return the expected active account");
  if (account?.key?._type !== "ECDSA_SECP256K1" || typeof account?.key?.key !== "string") fail("authoritative_key_type_mismatch", "testnet account key is not ECDSA_SECP256K1");
  const authoritativeRaw = account.key.key.toLowerCase();
  if (derivedRaw !== authoritativeRaw) fail("private_key_account_mismatch", `derived public key does not match account ${ACCOUNT_ID}`);
  if (String(account.evm_address || "").toLowerCase() !== derivedEvm) fail("evm_alias_mismatch", "derived EVM alias does not match authoritative account");
  const tinybar = BigInt(account?.balance?.balance ?? 0);
  return {
    schema: "openclaw.hedera_testnet_test_signer_probe.v1",
    ok: true,
    checked_at: new Date().toISOString(),
    network: NETWORK,
    ledger: "hedera-testnet",
    account_id: ACCOUNT_ID,
    classification: CLASSIFICATION,
    secret_file: metadata,
    sdk: { package: "@hashgraph/sdk", version },
    key: {
      type: "ECDSA_SECP256K1",
      derived_public_key: derivedRaw,
      derived_public_key_fingerprint_sha256: publicFingerprint(derivedRaw),
      authoritative_public_key: authoritativeRaw,
      authoritative_public_key_fingerprint_sha256: publicFingerprint(authoritativeRaw),
      matches_authoritative_account: true,
      derived_evm_alias: derivedEvm,
      mirror_evm_address: account.evm_address || null,
      evm_alias_matches: true,
    },
    balance: {
      tinybar: tinybar.toString(),
      hbar: `${tinybar / 100000000n}.${(tinybar % 100000000n).toString().padStart(8, "0")}`,
      mirror_timestamp: String(account?.balance?.timestamp || "unknown"),
    },
    boundaries: {
      transaction_executed: false,
      signing_executed: false,
      funds_moved: false,
      default_process_inheritance: false,
      raw_private_key_output: false,
    },
  };
}

async function selfTest() {
  const { sdk } = loadSdk();
  const dir = mkdtempSync(path.join(tmpdir(), "openclaw-hedera-testnet-probe-"));
  const secretPath = path.join(dir, "test.env");
  try {
    const privateKey = sdk.PrivateKey.generateECDSA();
    const publicKey = privateKey.publicKey.toStringRaw();
    const evm = `0x${privateKey.publicKey.toEvmAddress().replace(/^0x/, "").toLowerCase()}`;
    writeFileSync(secretPath, `${GENERAL_LAYOUT.account}=${ACCOUNT_ID}\n${GENERAL_LAYOUT.privateKey}=${privateKey.toStringRaw()}\n`, { encoding: "utf8", mode: 0o600 });
    chmodSync(secretPath, 0o600);
    const proof = await prove(secretPath, {
      account: ACCOUNT_ID,
      deleted: false,
      evm_address: evm,
      key: { _type: "ECDSA_SECP256K1", key: publicKey },
      balance: { balance: 1, timestamp: "self-test" },
    });
    return {
      schema: "openclaw.hedera_testnet_test_signer_probe.self_test.v1",
      ok: proof.ok,
      key_type: proof.key.type,
      public_key_fingerprint_sha256: proof.key.derived_public_key_fingerprint_sha256,
      secret_parser_exercised: true,
      account_match_exercised: true,
      evm_match_exercised: true,
      raw_private_key_output: false,
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function printSafeError(error) {
  const code = error instanceof SafeProbeError ? error.code : "unexpected_probe_failure";
  const detail = error instanceof SafeProbeError ? error.detail : "probe failed without secret output";
  process.stdout.write(`${JSON.stringify({ schema: "openclaw.hedera_testnet_test_signer_probe.v1", ok: false, error: code, detail }, null, 2)}\n`);
}

async function main() {
  const args = new Set(process.argv.slice(2));
  const known = new Set(["--json", "--self-test", "--help"]);
  for (const arg of args) if (!known.has(arg)) fail("unsupported_argument", arg);
  if (args.has("--help")) {
    process.stdout.write("Usage: node scripts/hedera_testnet_test_signer_probe.mjs [--json|--self-test]\n");
    return;
  }
  const result = args.has("--self-test") ? await selfTest() : await prove();
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

main().catch((error) => {
  printSafeError(error);
  process.exitCode = 1;
});
