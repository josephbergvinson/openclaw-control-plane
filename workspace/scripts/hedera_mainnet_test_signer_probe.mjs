#!/usr/bin/env node
import { binding as operatorBinding, materializeOperatorValue } from "./routing_operator_bindings.mjs";

import { createHash, randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import {
  closeSync,
  constants as fsConstants,
  chmodSync,
  chownSync,
  existsSync,
  fsyncSync,
  fstatSync,
  ftruncateSync,
  lstatSync,
  mkdtempSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

const ACCOUNT_ID = operatorBinding("services.wallets.hedera_mainnet.account_id");
const NETWORK = "mainnet";
const MIRROR_URL = `https://mainnet-public.mirrornode.hedera.com/api/v1/accounts/${ACCOUNT_ID}`;
const SECRET_ROOT = path.join(operatorBinding("paths.state_root"), "secrets");
const LEGACY_PATH = path.join(SECRET_ROOT, "refuel402-mainnet-test-signer.env");
const GENERAL_PATH = operatorBinding("paths.routing_openclaw_hedera_mainnet_test_account_env");
const LEGACY_LAYOUT = {
  account: "REFUEL402_MAINNET_TEST_ACCOUNT_ID",
  privateKey: "REFUEL402_MAINNET_TEST_PRIVATE_KEY",
};
const GENERAL_LAYOUT = {
  account: "OPENCLAW_HEDERA_MAINNET_TEST_ACCOUNT_ID",
  privateKey: "OPENCLAW_HEDERA_MAINNET_TEST_PRIVATE_KEY",
  keyType: "OPENCLAW_HEDERA_MAINNET_TEST_KEY_TYPE",
};
const CLASSIFICATION = "general_openclaw_hedera_mainnet_disposable_test_signer";
// The previous literal pointed at ~/Code/OpenClawWorkspace, a path policy
// requires to stay absent, so the probe always reported the SDK missing.
// Resolve the owning repository from the topology registry instead of pinning
// another literal.
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
    // A missing or unreadable registry is reported as sdk-unavailable below,
    // never as a silent skip.
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
      // satisfies it and then fails downstream with
      // authoritative_public_key_malformed. Require the ED25519 entry points
      // this probe actually calls.
      if (
        typeof sdk?.PrivateKey?.fromStringED25519 === "function" &&
        typeof sdk?.PrivateKey?.generateED25519 === "function" &&
        typeof sdk?.PublicKey?.fromStringED25519 === "function"
      ) {
        return { sdk, version: String(packageJson.version || "unknown") };
      }
    } catch {
      // Candidate absence is reported only after all known secret-free paths are checked.
    }
  }
  fail("hedera_sdk_unavailable", "canonical local @hashgraph/sdk installation was not found");
}

function assertSafeSecretFile(secretPath, layout) {
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
  if (modeString(fileStat.mode) !== "0600") {
    fail("secret_mode_mismatch", `expected 0600, observed ${modeString(fileStat.mode)}`);
  }

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

  const expectedNames = Object.values(layout);
  const lines = body.split("\n");
  const blankLineNumbers = lines.flatMap((line, index) => (line === "" ? [index + 1] : []));
  if (blankLineNumbers.length > 0) {
    const lineShapes = lines.map((line, index) => {
      const match = /^([A-Z0-9_]+)=(.*)$/.exec(line);
      if (match) return `${index + 1}:${match[1]}:value_length=${match[2].length}`;
      return `${index + 1}:${line.trim() === "" ? "whitespace" : "malformed"}:length=${line.length}`;
    });
    fail(
      "secret_blank_or_trailing_record",
      `blank record line(s) ${blankLineNumbers.join(",")}; line shapes: ${lineShapes.join(";")}`,
    );
  }
  if (lines.length !== expectedNames.length) {
    fail("secret_record_count_mismatch", `expected ${expectedNames.length} records, observed ${lines.length}`);
  }

  const values = new Map();
  for (const line of lines) {
    const match = /^([A-Z0-9_]+)=([^\s]+)$/.exec(line);
    if (!match) fail("secret_record_malformed", "records must be NAME=VALUE with no whitespace");
    const [, name, value] = match;
    if (!expectedNames.includes(name)) fail("secret_extra_variable", `unexpected variable ${name}`);
    if (values.has(name)) fail("secret_duplicate_variable", `duplicate variable ${name}`);
    if (!value) fail("secret_blank_value", `blank value for ${name}`);
    values.set(name, value);
  }
  for (const name of expectedNames) {
    if (!values.has(name)) fail("secret_missing_variable", `missing variable ${name}`);
  }

  if (values.get(layout.account) !== ACCOUNT_ID) {
    fail("secret_account_id_mismatch", `expected account ${ACCOUNT_ID}`);
  }
  if (layout.keyType && values.get(layout.keyType) !== "ED25519") {
    fail("secret_key_type_mismatch", "expected ED25519");
  }

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
      headers: { accept: "application/json", "user-agent": "openclaw-hedera-mainnet-test-signer-probe/1" },
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    fail("mirror_node_unreachable", "authoritative mainnet account readback failed");
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

async function prove(secretPath, layout, mirrorAccount = null) {
  const { sdk, version } = loadSdk();
  const { values, metadata } = assertSafeSecretFile(secretPath, layout);

  let privateKey;
  try {
    privateKey = sdk.PrivateKey.fromStringED25519(values.get(layout.privateKey));
  } catch {
    fail("private_key_malformed", "private key did not parse as ED25519");
  }
  const derivedPublicKey = privateKey.publicKey;
  const derivedRaw = derivedPublicKey.toStringRaw().toLowerCase();
  const account = mirrorAccount || (await fetchMirrorAccount());
  if (account?.account !== ACCOUNT_ID || account?.deleted === true) {
    fail("authoritative_account_mismatch", "mainnet readback did not return the expected active account");
  }
  if (account?.key?._type !== "ED25519" || typeof account?.key?.key !== "string") {
    fail("authoritative_key_type_mismatch", "mainnet account key is not ED25519");
  }

  let authoritativePublicKey;
  try {
    authoritativePublicKey = sdk.PublicKey.fromStringED25519(account.key.key);
  } catch {
    fail("authoritative_public_key_malformed", "mainnet account public key could not be parsed");
  }
  const authoritativeRaw = authoritativePublicKey.toStringRaw().toLowerCase();
  const matches = derivedRaw === authoritativeRaw;
  if (!matches) fail("private_key_account_mismatch", `derived public key does not match account ${ACCOUNT_ID}`);

  const tinybar = BigInt(account?.balance?.balance ?? 0);
  const hbar = `${tinybar / 100000000n}.${(tinybar % 100000000n).toString().padStart(8, "0")}`;
  return {
    schema: "openclaw.hedera_mainnet_test_signer_probe.v1",
    ok: true,
    checked_at: new Date().toISOString(),
    network: NETWORK,
    ledger: "hedera-mainnet",
    account_id: ACCOUNT_ID,
    classification: CLASSIFICATION,
    secret_file: metadata,
    sdk: { package: "@hashgraph/sdk", version },
    key: {
      type: "ED25519",
      derived_public_key: derivedRaw,
      derived_public_key_fingerprint_sha256: publicFingerprint(derivedRaw),
      authoritative_public_key: authoritativeRaw,
      authoritative_public_key_fingerprint_sha256: publicFingerprint(authoritativeRaw),
      matches_authoritative_account: matches,
      derived_evm_alias: null,
      mirror_evm_address: account.evm_address || null,
    },
    balance: {
      tinybar: tinybar.toString(),
      hbar,
      mirror_timestamp: String(account?.balance?.timestamp || "unknown"),
    },
    boundaries: {
      transaction_executed: false,
      signing_executed: false,
      funds_moved: false,
      default_process_inheritance: false,
    },
  };
}

function fsyncDirectory(dirPath) {
  let fd;
  try {
    fd = openSync(dirPath, fsConstants.O_RDONLY);
    fsyncSync(fd);
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function targetMustNotExist() {
  try {
    lstatSync(GENERAL_PATH);
    fail("target_secret_already_exists", "general secret path already exists; no overwrite performed");
  } catch (error) {
    if (error instanceof SafeProbeError) throw error;
    if (error?.code !== "ENOENT") fail("target_secret_state_unknown", "general secret path could not be inspected");
  }
}

function normalizeLegacyBlankRecord() {
  const before = lstatSync(LEGACY_PATH);
  if (before.isSymbolicLink() || !before.isFile()) {
    fail("secret_not_regular_file", "legacy secret must be a regular non-symlink file");
  }
  const currentUser = userInfo();
  if (currentUser.username !== operatorBinding("identifiers.host_user") || before.uid !== currentUser.uid) {
    fail("secret_owner_mismatch", "legacy secret must be owned by the configured host user");
  }
  if (modeString(before.mode) !== "0600") {
    fail("secret_mode_mismatch", `expected 0600, observed ${modeString(before.mode)}`);
  }
  const bytes = readFileSync(LEGACY_PATH);
  if (bytes.includes(0) || bytes.includes(13)) {
    fail("secret_unsafe_bytes", "legacy secret contains forbidden NUL or CR bytes");
  }
  if (!bytes.subarray(-2).equals(Buffer.from("\n\n")) || bytes.subarray(-3).equals(Buffer.from("\n\n\n"))) {
    fail("legacy_blank_record_shape_changed", "expected exactly one trailing blank record");
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, -1));
  } catch {
    fail("secret_not_utf8", "legacy secret must be valid UTF-8");
  }
  const lines = text.slice(0, -1).split("\n");
  if (lines.length !== 2) fail("legacy_record_count_changed", `expected two nonblank records, observed ${lines.length}`);
  const observedNames = [];
  for (const line of lines) {
    const match = /^([A-Z0-9_]+)=([^\s]+)$/.exec(line);
    if (!match) fail("secret_record_malformed", "legacy records must be NAME=VALUE with no whitespace");
    observedNames.push(match[1]);
  }
  if (observedNames.join(",") !== Object.values(LEGACY_LAYOUT).join(",")) {
    fail("legacy_variable_order_changed", `observed variable names: ${observedNames.join(",")}`);
  }

  let fd;
  try {
    fd = openSync(LEGACY_PATH, fsConstants.O_RDWR | fsConstants.O_NOFOLLOW);
    const opened = fstatSync(fd);
    if (opened.ino !== before.ino || opened.uid !== before.uid || modeString(opened.mode) !== "0600") {
      fail("legacy_inode_changed_during_normalization", "legacy secret identity changed before normalization");
    }
    ftruncateSync(fd, before.size - 1);
    fsyncSync(fd);
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
  const after = assertSafeSecretFile(LEGACY_PATH, LEGACY_LAYOUT).metadata;
  if (after.inode !== String(before.ino)) {
    fail("legacy_inode_changed_during_normalization", "legacy secret inode changed unexpectedly");
  }
  return {
    schema: "openclaw.hedera_mainnet_test_signer_normalization.v1",
    ok: true,
    operation: "in_place_remove_exact_trailing_blank_record",
    path: LEGACY_PATH,
    inode_before: String(before.ino),
    inode_after: after.inode,
    owner: after.owner,
    mode: after.mode,
    variable_names: after.variable_names,
    secret_value_output: false,
    backup_created: false,
    duplicate_created: false,
  };
}

async function reclassify() {
  targetMustNotExist();
  const before = await prove(LEGACY_PATH, LEGACY_LAYOUT);
  const legacy = assertSafeSecretFile(LEGACY_PATH, LEGACY_LAYOUT);
  const content = [
    `${GENERAL_LAYOUT.account}=${legacy.values.get(LEGACY_LAYOUT.account)}`,
    `${GENERAL_LAYOUT.privateKey}=${legacy.values.get(LEGACY_LAYOUT.privateKey)}`,
    `${GENERAL_LAYOUT.keyType}=ED25519`,
    "",
  ].join("\n");
  const tempPath = path.join(SECRET_ROOT, `.openclaw-hedera-mainnet-test-account.env.tmp-${process.pid}-${randomBytes(6).toString("hex")}`);
  let fd;
  try {
    fd = openSync(tempPath, fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW, 0o600);
    writeFileSync(fd, content, { encoding: "utf8" });
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    chownSync(tempPath, statSync(LEGACY_PATH).uid, statSync(LEGACY_PATH).gid);
    chmodSync(tempPath, 0o600);
    renameSync(tempPath, LEGACY_PATH);
    fsyncDirectory(SECRET_ROOT);
    renameSync(LEGACY_PATH, GENERAL_PATH);
    fsyncDirectory(SECRET_ROOT);
  } catch (error) {
    if (fd !== undefined) closeSync(fd);
    if (existsSync(tempPath)) unlinkSync(tempPath);
    if (error instanceof SafeProbeError) throw error;
    fail("secret_reclassification_failed", "atomic rewrite/move did not complete");
  }

  const after = await prove(GENERAL_PATH, GENERAL_LAYOUT);
  if (existsSync(LEGACY_PATH)) fail("legacy_secret_still_exists", "legacy secret path remains after reclassification");
  return {
    ...after,
    reclassification: {
      executed: true,
      operation: "atomic_rewrite_then_same_directory_rename",
      source_path: LEGACY_PATH,
      target_path: GENERAL_PATH,
      source_inode_before: before.secret_file.inode,
      target_inode_after: after.secret_file.inode,
      legacy_path_absent: true,
      persistent_backup_created: false,
      persistent_duplicate_created: false,
    },
  };
}

function scanForPrivateMaterial() {
  const { values } = assertSafeSecretFile(GENERAL_PATH, GENERAL_LAYOUT);
  const privateMaterial = Buffer.from(values.get(GENERAL_LAYOUT.privateKey), "utf8");
  const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
  const roots = [
    repoRoot,
    operatorBinding("paths.legacy_signer_scan_root"),
  ];
  const matches = [];
  let filesScanned = 0;
  let filesSkippedLarge = 0;
  const visit = (target) => {
    let entry;
    try {
      entry = lstatSync(target);
    } catch {
      return;
    }
    if (entry.isSymbolicLink()) return;
    if (entry.isDirectory()) {
      if ([".git", "node_modules", "__pycache__"].includes(path.basename(target))) return;
      for (const child of readdirSync(target)) visit(path.join(target, child));
      return;
    }
    if (!entry.isFile()) return;
    if (entry.size > 20 * 1024 * 1024) {
      filesSkippedLarge += 1;
      return;
    }
    filesScanned += 1;
    const bytes = readFileSync(target);
    if (bytes.includes(privateMaterial)) matches.push(target);
  };
  for (const root of roots) visit(root);
  return {
    schema: "openclaw.hedera_mainnet_test_signer_secret_scan.v1",
    ok: matches.length === 0,
    roots,
    files_scanned: filesScanned,
    files_skipped_over_20_mib: filesSkippedLarge,
    private_key_match_count: matches.length,
    matching_paths: matches,
    raw_private_key_output: false,
  };
}

async function selfTest() {
  const { sdk } = loadSdk();
  const dir = mkdtempSync(path.join(tmpdir(), "openclaw-hedera-probe-"));
  const secretPath = path.join(dir, "test.env");
  try {
    const privateKey = sdk.PrivateKey.generateED25519();
    const publicKey = privateKey.publicKey.toStringRaw();
    writeFileSync(
      secretPath,
      [
        `${GENERAL_LAYOUT.account}=${ACCOUNT_ID}`,
        `${GENERAL_LAYOUT.privateKey}=${privateKey.toStringDer()}`,
        `${GENERAL_LAYOUT.keyType}=ED25519`,
        "",
      ].join("\n"),
      { encoding: "utf8", mode: 0o600 },
    );
    chmodSync(secretPath, 0o600);
    const proof = await prove(secretPath, GENERAL_LAYOUT, {
      account: ACCOUNT_ID,
      deleted: false,
      evm_address: "0x00000000000000000000000000000000001119f0",
      key: { _type: "ED25519", key: publicKey },
      balance: { balance: 1, timestamp: "self-test" },
    });
    return {
      schema: "openclaw.hedera_mainnet_test_signer_probe.self_test.v1",
      ok: proof.ok,
      key_type: proof.key.type,
      public_key_fingerprint_sha256: proof.key.derived_public_key_fingerprint_sha256,
      secret_parser_exercised: true,
      account_match_exercised: true,
      raw_private_key_output: false,
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function printSafeError(error) {
  const code = error instanceof SafeProbeError ? error.code : "unexpected_probe_failure";
  const detail = error instanceof SafeProbeError ? error.detail : "probe failed without secret output";
  process.stdout.write(`${JSON.stringify({ schema: "openclaw.hedera_mainnet_test_signer_probe.v1", ok: false, error: code, detail }, null, 2)}\n`);
}

async function main() {
  const args = new Set(process.argv.slice(2));
  const known = new Set(["--json", "--legacy", "--normalize-legacy-blank-record", "--reclassify", "--secret-scan", "--self-test", "--help"]);
  for (const arg of args) {
    if (!known.has(arg)) fail("unsupported_argument", arg);
  }
  if (args.has("--help")) {
    process.stdout.write("Usage: node scripts/hedera_mainnet_test_signer_probe.mjs [--json|--legacy|--normalize-legacy-blank-record|--reclassify|--secret-scan|--self-test]\n");
    return;
  }
  const selected = ["--legacy", "--normalize-legacy-blank-record", "--reclassify", "--secret-scan", "--self-test"].filter((arg) => args.has(arg));
  if (selected.length > 1) fail("conflicting_arguments", selected.join(","));
  const result = args.has("--self-test")
    ? await selfTest()
    : args.has("--secret-scan")
      ? scanForPrivateMaterial()
      : args.has("--normalize-legacy-blank-record")
        ? normalizeLegacyBlankRecord()
        : args.has("--reclassify")
          ? await reclassify()
          : args.has("--legacy")
            ? await prove(LEGACY_PATH, LEGACY_LAYOUT)
            : await prove(GENERAL_PATH, GENERAL_LAYOUT);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

main().catch((error) => {
  printSafeError(error);
  process.exitCode = 1;
});
