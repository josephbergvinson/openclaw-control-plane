import { binding as operatorBinding, materializeOperatorValue } from "../routing_operator_bindings.mjs";
import { createHash, randomUUID } from "node:crypto";
import {
  lstatSync,
  readFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { createRequire } from "node:module";
import { homedir, userInfo } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const MODULE_ROOT = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.dirname(path.dirname(MODULE_ROOT));
const RUNTIME_SUBDIRECTORY = "openclaw-walletconnect-testnet-runtime";
const DEFAULT_PROJECT_ID = operatorBinding("services.walletconnect.project_id");
const SUPPORTED_METHODS = Object.freeze([
  "hedera_signAndExecuteTransaction",
  "hedera_signMessage",
]);
const SUPPORTED_EVENTS = Object.freeze(["accountsChanged"]);
const SESSION_TIMEOUT_MS = 120_000;
const REQUEST_TIMEOUT_MS = 300_000;
const MIRROR_TIMEOUT_MS = 15_000;
const HAPI_RECEIPT_TIMEOUT_MS = 12_000;
const SETTLEMENT_ATTEMPTS = 15;
const SETTLEMENT_INTERVAL_MS = 2_000;
const NETWORK_RUNTIME = Object.freeze({
  "hedera:testnet": Object.freeze({
    network: "testnet",
    secretFile: "openclaw-hedera-testnet-test-account.env",
    secretLayout: Object.freeze({
      account: "OPENCLAW_HEDERA_TESTNET_TEST_ACCOUNT_ID",
      privateKey: "OPENCLAW_HEDERA_TESTNET_TEST_PRIVATE_KEY",
    }),
    keyType: "ECDSA_SECP256K1",
    mirrorBase: "https://testnet.mirrornode.hedera.com/api/v1",
    hashscanBase: "https://hashscan.io/testnet/transaction",
  }),
  "hedera:mainnet": Object.freeze({
    network: "mainnet",
    secretFile: "openclaw-hedera-mainnet-test-account.env",
    secretLayout: Object.freeze({
      account: "OPENCLAW_HEDERA_MAINNET_TEST_ACCOUNT_ID",
      privateKey: "OPENCLAW_HEDERA_MAINNET_TEST_PRIVATE_KEY",
      keyType: "OPENCLAW_HEDERA_MAINNET_TEST_KEY_TYPE",
    }),
    keyType: "ED25519",
    mirrorBase: "https://mainnet-public.mirrornode.hedera.com/api/v1",
    hashscanBase: "https://hashscan.io/mainnet/transaction",
  }),
});

export class WalletConnectAdapterError extends Error {
  constructor(code, executionEvidence = null) {
    super(code);
    this.code = code;
    this.executionEvidence = executionEvidence;
  }
}

export function isTrustedCompanyAlphaOrigin(value) {
  try {
    const parsed = new URL(value);
    const hostname = parsed.hostname.toLowerCase();
    const companyAlpha =
      parsed.protocol === "https:" &&
      (hostname === operatorBinding("services.company_alpha.domain") ||
        hostname.endsWith(operatorBinding("services.company_alpha.domain_suffix")));
    const loopback =
      parsed.protocol === "http:" &&
      (hostname === "127.0.0.1" || hostname === "localhost") &&
      parsed.port !== "";
    return (
      parsed.origin === value &&
      parsed.username === "" &&
      parsed.password === "" &&
      (companyAlpha || loopback)
    );
  } catch {
    return false;
  }
}

export function isTrustedCompanyAlphaOriginForChain(value, chainId) {
  if (!isTrustedCompanyAlphaOrigin(value)) return false;
  const parsed = new URL(value);
  const hostname = parsed.hostname.toLowerCase();
  if (chainId === "hedera:testnet") {
    return (
      hostname === operatorBinding("services.company_alpha.testnet_hostname") ||
      (parsed.protocol === "http:" &&
        (hostname === "127.0.0.1" || hostname === "localhost"))
    );
  }
  if (chainId === "hedera:mainnet") {
    return (
      parsed.protocol === "https:" &&
      hostname !== operatorBinding("services.company_alpha.testnet_hostname") &&
      (hostname === operatorBinding("services.company_alpha.domain") ||
        hostname.endsWith(operatorBinding("services.company_alpha.domain_suffix")))
    );
  }
  return false;
}

function fail(code, executionEvidence = null) {
  throw new WalletConnectAdapterError(code, executionEvidence);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function modeString(mode) {
  return (mode & 0o777).toString(8).padStart(4, "0");
}

function networkPolicy(origin, chainId, accountId) {
  if (!isTrustedCompanyAlphaOriginForChain(origin, chainId)) {
    fail("adapter_origin_chain_mismatch");
  }
  if (!/^0\.0\.[0-9]+$/.test(accountId)) fail("adapter_account_mismatch");
  const runtime = NETWORK_RUNTIME[chainId];
  if (!runtime) fail("adapter_chain_not_supported");
  return runtime;
}

export function adapterCapabilityDescriptor(dependencyVersions = undefined) {
  return {
    schema: "openclaw.company_alpha_walletconnect_hedera_adapter.v1",
    production_factory: "createProductionHederaWalletConnectAdapter",
    ...(dependencyVersions
      ? { dependency_versions: { ...dependencyVersions } }
      : {}),
    key_loading: "execute_only_process_bound",
    supported_methods: [...SUPPORTED_METHODS],
    execution_scope: "site_generated_hedera_wallet_requests_for_selected_account",
    settlement_evidence: [
      "sdk_hapi_receipt",
      "mirror_transaction",
      "mirror_contract_result",
      "hashscan_url",
      "selected_account_before_after",
    ],
    origin_policy: "company_alpha_finance_or_loopback",
    supported_networks: Object.keys(NETWORK_RUNTIME),
  };
}

export function validateProductionRuntimeApi({ sdk, Core, walletConnect }) {
  if (
    typeof sdk?.PrivateKey?.fromStringECDSA !== "function" ||
    typeof sdk?.PrivateKey?.fromStringED25519 !== "function" ||
    typeof Core !== "function" ||
    typeof walletConnect?.Wallet !== "function"
  ) {
    fail("adapter_dependency_api_mismatch");
  }
}

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  async getKeys() {
    return [...this.values.keys()];
  }

  async getEntries() {
    return [...this.values.entries()];
  }

  async getItem(key) {
    return this.values.get(key);
  }

  async setItem(key, value) {
    this.values.set(key, value);
  }

  async removeItem(key) {
    this.values.delete(key);
  }
}

function resolveRuntimeDataRoot() {
  let registry;
  try {
    registry = materializeOperatorValue(JSON.parse(
      readFileSync(
        path.join(WORKSPACE_ROOT, "registry", "project_topology.json"),
        "utf8",
      ),
    ));
  } catch {
    fail("adapter_project_topology_unreadable");
  }
  for (const project of registry?.projects ?? []) {
    if (
      project?.project_id === "company-alpha" &&
      project?.runtime_data_root_state === "active" &&
      typeof project?.runtime_data_root === "string"
    ) {
      return project.runtime_data_root;
    }
  }
  fail("adapter_runtime_data_root_missing");
}

function assertOwnerDirectory(directoryPath, expectedMode = undefined) {
  let lexical;
  let resolved;
  let stat;
  try {
    lexical = lstatSync(directoryPath);
    resolved = realpathSync(directoryPath);
    stat = statSync(directoryPath);
  } catch {
    fail("adapter_runtime_directory_unreadable");
  }
  if (
    lexical.isSymbolicLink() ||
    !lexical.isDirectory() ||
    !stat.isDirectory() ||
    resolved !== directoryPath ||
    stat.uid !== userInfo().uid ||
    (expectedMode && modeString(stat.mode) !== expectedMode)
  ) {
    fail("adapter_runtime_directory_invalid");
  }
}

function packageVersion(packageRoot) {
  try {
    return String(
      JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"))
        .version,
    );
  } catch {
    fail("adapter_dependency_metadata_unreadable");
  }
}

function resolvePackageRoot(runtimeRequire, runtimeRoot, packageName) {
  let resolvedModule;
  let nodeModulesRoot;
  try {
    resolvedModule = realpathSync(runtimeRequire.resolve(packageName));
    nodeModulesRoot = realpathSync(path.join(runtimeRoot, "node_modules"));
  } catch {
    fail("adapter_dependency_metadata_unreadable");
  }
  const requiredPrefix = `${nodeModulesRoot}${path.sep}`;
  let candidate = path.dirname(resolvedModule);
  for (let depth = 0; depth < 5; depth += 1) {
    if (candidate !== nodeModulesRoot && !candidate.startsWith(requiredPrefix)) {
      break;
    }
    try {
      const metadata = JSON.parse(
        readFileSync(path.join(candidate, "package.json"), "utf8"),
      );
      if (metadata?.name === packageName) return candidate;
    } catch {
      // Continue only within the pinned runtime's node_modules tree.
    }
    if (candidate === nodeModulesRoot) break;
    candidate = path.dirname(candidate);
  }
  fail("adapter_dependency_metadata_unreadable");
}

function loadProductionRuntime() {
  const runtimeRoot = path.join(
    resolveRuntimeDataRoot(),
    RUNTIME_SUBDIRECTORY,
  );
  assertOwnerDirectory(runtimeRoot, "0700");
  assertOwnerDirectory(path.join(runtimeRoot, "node_modules"));
  const runtimeRequire = createRequire(path.join(runtimeRoot, "package.json"));
  let sdk;
  let Core;
  let walletConnect;
  let jiti;
  try {
    sdk = runtimeRequire("@hashgraph/sdk");
    Core = runtimeRequire("@walletconnect/core").Core;
    jiti = runtimeRequire("jiti")(
      path.join(runtimeRoot, "package.json"),
    );
    walletConnect = jiti("@hashgraph/hedera-wallet-connect");
  } catch {
    fail("adapter_dependency_unavailable");
  }
  const dependencyRoots = {
    hedera_wallet_connect: resolvePackageRoot(
      runtimeRequire,
      runtimeRoot,
      "@hashgraph/hedera-wallet-connect",
    ),
    hashgraph_sdk: resolvePackageRoot(
      runtimeRequire,
      runtimeRoot,
      "@hashgraph/sdk",
    ),
    walletconnect_core: resolvePackageRoot(
      runtimeRequire,
      runtimeRoot,
      "@walletconnect/core",
    ),
    jiti: resolvePackageRoot(runtimeRequire, runtimeRoot, "jiti"),
  };
  const versions = Object.fromEntries(
    Object.entries(dependencyRoots).map(([name, root]) => [
      name,
      packageVersion(root),
    ]),
  );
  validateProductionRuntimeApi({ sdk, Core, walletConnect });
  return { runtimeRoot, sdk, Core, walletConnect, versions };
}

export function validateProductionRuntimeDependencies() {
  return { ...loadProductionRuntime().versions };
}

function loadBoundPrivateKey({ sdk, policy, accountId }) {
  const secretPath = path.join(
    operatorBinding("paths.state_root"),
    "secrets",
    policy.secretFile,
  );
  let lexical;
  let stat;
  let bytes;
  try {
    lexical = lstatSync(secretPath);
    stat = statSync(secretPath);
    bytes = readFileSync(secretPath);
  } catch {
    fail("adapter_signer_secret_unreadable");
  }
  if (
    lexical.isSymbolicLink() ||
    !lexical.isFile() ||
    !stat.isFile() ||
    stat.uid !== userInfo().uid ||
    modeString(stat.mode) !== "0600" ||
    bytes.includes(0) ||
    bytes.includes(13)
  ) {
    fail("adapter_signer_secret_identity_invalid");
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    bytes.fill(0);
    fail("adapter_signer_secret_encoding_invalid");
  }
  bytes.fill(0);
  if (!text.endsWith("\n")) fail("adapter_signer_secret_layout_invalid");
  const lines = text.slice(0, -1).split("\n");
  const expected = Object.values(policy.secretLayout);
  if (lines.length !== expected.length || lines.some((line) => !line)) {
    fail("adapter_signer_secret_layout_invalid");
  }
  const values = new Map();
  for (const line of lines) {
    const match = /^([A-Z0-9_]+)=([^\s]+)$/.exec(line);
    if (!match || !expected.includes(match[1]) || values.has(match[1])) {
      fail("adapter_signer_secret_layout_invalid");
    }
    values.set(match[1], match[2]);
  }
  if (
    expected.some((name) => !values.has(name)) ||
    values.get(policy.secretLayout.account) !== accountId ||
    (policy.secretLayout.keyType &&
      values.get(policy.secretLayout.keyType) !== policy.keyType)
  ) {
    fail("adapter_signer_secret_binding_mismatch");
  }
  let privateKey;
  try {
    privateKey =
      policy.keyType === "ED25519"
        ? sdk.PrivateKey.fromStringED25519(
            values.get(policy.secretLayout.privateKey),
          )
        : sdk.PrivateKey.fromStringECDSA(
            values.get(policy.secretLayout.privateKey),
          );
  } catch {
    fail("adapter_signer_private_key_invalid");
  }
  return {
    privateKey,
    keyType: policy.keyType,
  };
}

function cancellableEventOnce(emitter, name, timeoutMs, timeoutCode) {
  let settled = false;
  let promiseCancel = () => {};
  const promise = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      settled = true;
      emitter.off(name, onEvent);
      reject(new WalletConnectAdapterError(timeoutCode));
    }, timeoutMs);
    const onEvent = (event) => {
      settled = true;
      clearTimeout(timeout);
      emitter.off(name, onEvent);
      resolve(event);
    };
    emitter.on(name, onEvent);
    promiseCancel = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      emitter.off(name, onEvent);
      reject(new WalletConnectAdapterError("adapter_event_wait_cancelled"));
    };
  });
  return { promise, cancel: () => promiseCancel() };
}

function mirrorTransactionId(transactionId) {
  const match = /^(0\.0\.[0-9]+)@([0-9]+)\.([0-9]+)$/.exec(transactionId);
  if (!match) fail("adapter_transaction_id_invalid");
  return `${match[1]}-${match[2]}-${match[3]}`;
}

function integerString(value, code) {
  const rendered = String(value);
  if (!/^-?[0-9]+$/.test(rendered)) fail(code);
  if (!Number.isSafeInteger(Number(rendered))) fail(code);
  return rendered;
}

function normalizeBalanceSnapshot(payload, accountId, chainId) {
  const rows = payload?.balances;
  if (!Array.isArray(rows) || rows.length !== 1) {
    fail("adapter_balance_snapshot_invalid");
  }
  const row = rows[0];
  if (row.account !== accountId) fail("adapter_balance_account_mismatch");
  const tokens = (row.tokens ?? [])
    .map((token) => ({
      token_id: String(token.token_id),
      balance: integerString(token.balance, "adapter_token_balance_invalid"),
    }))
    .sort((left, right) => left.token_id.localeCompare(right.token_id));
  return {
    schema: "openclaw.company_alpha_walletconnect_account_snapshot.v1",
    chain_id: chainId,
    account_id: accountId,
    hbar_tinybar: integerString(row.balance, "adapter_hbar_balance_invalid"),
    tokens,
  };
}

function balanceDeltas(before, after) {
  const beforeTokens = new Map(
    before.tokens.map((row) => [row.token_id, BigInt(row.balance)]),
  );
  const afterTokens = new Map(
    after.tokens.map((row) => [row.token_id, BigInt(row.balance)]),
  );
  const tokenIds = [...new Set([...beforeTokens.keys(), ...afterTokens.keys()])].sort();
  return {
    hbar_tinybar: (
      BigInt(after.hbar_tinybar) - BigInt(before.hbar_tinybar)
    ).toString(),
    tokens: tokenIds
      .map((tokenId) => ({
        token_id: tokenId,
        amount: (
          (afterTokens.get(tokenId) ?? 0n) -
          (beforeTokens.get(tokenId) ?? 0n)
        ).toString(),
      }))
      .filter((row) => row.amount !== "0"),
  };
}

async function delay(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(fetchImpl, url, code, { allowNotFound = false } = {}) {
  let response;
  try {
    response = await fetchImpl(url, {
      headers: {
        accept: "application/json",
        "user-agent": "openclaw-company-alpha-walletconnect-signer/1",
      },
      signal: AbortSignal.timeout(MIRROR_TIMEOUT_MS),
    });
  } catch {
    fail(code);
  }
  if (allowNotFound && response?.status === 404) return null;
  if (!response?.ok) fail(code);
  try {
    return await response.json();
  } catch {
    fail(code);
  }
}

function boundedRows(rows, fields, code) {
  if (!Array.isArray(rows) || rows.length > 200) fail(code);
  return rows.map((row) =>
    Object.fromEntries(fields.map((field) => [field, row?.[field] ?? null])),
  );
}

export async function createHederaWalletConnectAdapter({
  origin,
  chainId,
  accountId,
  loadSigner,
  runtime,
  fetchImpl = globalThis.fetch,
  projectId = DEFAULT_PROJECT_ID,
  sessionTimeoutMs = SESSION_TIMEOUT_MS,
  requestTimeoutMs = REQUEST_TIMEOUT_MS,
  settlementAttempts = SETTLEMENT_ATTEMPTS,
  settlementIntervalMs = SETTLEMENT_INTERVAL_MS,
  hapiReceiptTimeoutMs = HAPI_RECEIPT_TIMEOUT_MS,
}) {
  const policy = networkPolicy(origin, chainId, accountId);
  if (!/^[0-9a-f]{32}$/i.test(projectId)) fail("adapter_project_id_invalid");
  if (
    !runtime?.sdk ||
    typeof runtime?.Core !== "function" ||
    typeof runtime?.walletConnect?.Wallet !== "function" ||
    typeof fetchImpl !== "function"
  ) {
    fail("adapter_runtime_invalid");
  }
  const { Wallet: HederaWeb3Wallet, HederaChainId, HederaSessionEvent } =
    runtime.walletConnect;
  const chainEnum =
    policy.network === "mainnet"
      ? HederaChainId.Mainnet
      : HederaChainId.Testnet;
  if (!chainEnum) fail("adapter_chain_enum_missing");
  const core = new runtime.Core({
    projectId,
    logger: "silent",
    storage: new MemoryStorage(),
    customStoragePrefix: `openclaw-company-alpha-${policy.network}-${randomUUID()}`,
    telemetryEnabled: false,
  });
  const wallet = new HederaWeb3Wallet(
    {
      core,
      metadata: {
        name: "OpenClaw CompanyAlpha WalletConnect Signer",
        description: "Account-bound CompanyAlpha wallet signer",
        url: "https://openclaw.local.invalid",
        icons: [],
      },
    },
    [chainEnum],
    [...SUPPORTED_METHODS],
    [HederaSessionEvent.AccountsChanged],
  );
  try {
    await wallet.engine.init();
  } catch {
    fail("adapter_walletconnect_init_failed");
  }

  let session = null;
  let pendingEvent = null;
  let pendingExecutionState = "not_attempted";
  let signerRecord = null;
  let closed = false;
  let executionState = "not_attempted";
  let sessionBlockedByUnknownSubmission = false;
  let sessionEnded = false;
  const queuedRequests = [];
  const seenRequestKeys = new Set();
  let requestWaiter = null;
  let requestListener = null;
  let sessionDeleteListener = null;
  const submittedResponses = new Map();
  let closeReceipt = null;

  function attachSessionListeners() {
    if (requestListener) return;
    requestListener = (event) => {
      if (requestWaiter) {
        const waiter = requestWaiter;
        requestWaiter = null;
        clearTimeout(waiter.timeout);
        waiter.resolve(event);
        return;
      }
      queuedRequests.push(event);
    };
    sessionDeleteListener = () => {
      sessionEnded = true;
      if (requestWaiter) {
        const waiter = requestWaiter;
        requestWaiter = null;
        clearTimeout(waiter.timeout);
        waiter.resolve(null);
      }
    };
    wallet.on("session_request", requestListener);
    wallet.on("session_delete", sessionDeleteListener);
  }

  function detachSessionListeners() {
    if (requestListener) wallet.off("session_request", requestListener);
    if (sessionDeleteListener) wallet.off("session_delete", sessionDeleteListener);
    requestListener = null;
    sessionDeleteListener = null;
    if (requestWaiter) {
      const waiter = requestWaiter;
      requestWaiter = null;
      clearTimeout(waiter.timeout);
      waiter.reject(new WalletConnectAdapterError("adapter_event_wait_cancelled"));
    }
  }

  function waitForSessionRequest(timeoutCode) {
    if (queuedRequests.length > 0) return Promise.resolve(queuedRequests.shift());
    if (sessionEnded) return Promise.resolve(null);
    if (requestWaiter) fail("adapter_request_wait_already_active");
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        requestWaiter = null;
        reject(new WalletConnectAdapterError(timeoutCode));
      }, requestTimeoutMs);
      requestWaiter = { resolve, reject, timeout };
    });
  }

  function parseRequestEvent(event) {
    if (
      !session ||
      typeof session.topic !== "string" ||
      event?.topic !== session.topic ||
      !(typeof event?.id === "number" || typeof event?.id === "string")
    ) {
      fail("adapter_session_request_binding_mismatch");
    }
    const requestKey = `${event.topic}:${String(event.id)}`;
    if (seenRequestKeys.has(requestKey)) {
      fail("adapter_session_request_duplicate");
    }
    seenRequestKeys.add(requestKey);
    let parsedRequest;
    try {
      parsedRequest = {
        ...wallet.parseSessionRequest(event),
        requestId: event.id,
      };
    } catch {
      fail("adapter_session_request_parse_failed");
    }
    pendingEvent = event;
    pendingExecutionState = "not_attempted";
    return {
      sessionTopic: session.topic,
      event,
      parsedRequest,
    };
  }

  async function readRelevantState(frozenIntent) {
    const transaction = frozenIntent?.transaction;
    if (!transaction) return null;
    if (transaction.transaction_type !== "ContractExecuteTransaction") {
      fail("adapter_transaction_type_not_supported");
    }
    return { kind: "contract_execution", contract_id: transaction.contract_id };
  }

  async function snapshotAccount({ frozenIntent } = {}) {
    const url = `${policy.mirrorBase}/balances?account.id=${encodeURIComponent(
      accountId,
    )}&limit=100`;
    const payload = await fetchJson(
      fetchImpl,
      url,
      "adapter_balance_snapshot_failed",
    );
    if (payload?.links?.next) fail("adapter_balance_snapshot_paginated");
    return {
      ...normalizeBalanceSnapshot(payload, accountId, chainId),
      relevant_state: await readRelevantState(frozenIntent),
    };
  }

  return {
    async captureRequest({
      pairingUri,
      origin: requestedOrigin,
      chainId: requestedChain,
      accountId: requestedAccount,
      validateProposal,
    }) {
      if (
        closed ||
        session ||
        pendingEvent ||
        requestedOrigin !== origin ||
        requestedChain !== chainId ||
        requestedAccount !== accountId ||
        typeof validateProposal !== "function"
      ) {
        fail("adapter_capture_not_admissible");
      }
      const proposalWait = cancellableEventOnce(
        wallet,
        "session_proposal",
        sessionTimeoutMs,
        "adapter_session_proposal_timeout",
      );
      attachSessionListeners();
      try {
        await wallet.pair({ uri: pairingUri });
      } catch {
        proposalWait.cancel();
        await proposalWait.promise.catch(() => {});
        fail("adapter_walletconnect_pair_failed");
      }
      const proposal = await proposalWait.promise;
      const proposalReceipt = validateProposal(proposal);
      try {
        session = await wallet.buildAndApproveSession(
          [`${chainId}:${accountId}`],
          proposal,
        );
      } catch {
        fail("adapter_session_approval_failed");
      }
      const event = await waitForSessionRequest("adapter_session_request_timeout");
      if (!event) fail("adapter_session_ended_before_request");
      return {
        proposalReceipt,
        ...parseRequestEvent(event),
      };
    },

    async waitForNextRequest() {
      if (
        closed ||
        !session ||
        pendingEvent ||
        sessionBlockedByUnknownSubmission
      ) {
        fail("adapter_request_wait_not_admissible");
      }
      const event = await waitForSessionRequest("adapter_session_idle_timeout");
      return event ? parseRequestEvent(event) : null;
    },

    snapshotAccount,

    async signMessage({ event, message }) {
      if (
        closed ||
        !session ||
        pendingEvent !== event ||
        typeof message !== "string" ||
        typeof loadSigner !== "function"
      ) {
        fail("adapter_message_sign_not_admissible");
      }
      if (!signerRecord) signerRecord = await loadSigner();
      if (
        !signerRecord?.privateKey ||
        signerRecord?.keyType !== policy.keyType
      ) {
        fail("adapter_signer_invalid");
      }
      const hederaWallet = wallet.getHederaWallet(
        chainEnum,
        accountId,
        signerRecord.privateKey,
      );
      pendingExecutionState = "response_unknown";
      try {
        await wallet.hedera_signMessage(
          event.id,
          event.topic,
          message,
          hederaWallet,
        );
      } catch {
        fail("adapter_message_response_outcome_unknown");
      }
      pendingEvent = null;
      pendingExecutionState = "completed";
      return { walletConnectResponseStatus: "delivered" };
    },

    async signAndExecute({
      event,
      transaction,
      verifySignedTransaction,
      beforeSubmit,
    }) {
      if (
        closed ||
        !session ||
        pendingEvent !== event ||
        typeof verifySignedTransaction !== "function" ||
        typeof beforeSubmit !== "function" ||
        typeof loadSigner !== "function"
      ) {
        fail("adapter_execution_not_admissible");
      }
      if (!signerRecord) signerRecord = await loadSigner();
      if (
        !signerRecord?.privateKey ||
        signerRecord?.keyType !== policy.keyType
      ) {
        fail("adapter_signer_invalid");
      }
      const hederaWallet = wallet.getHederaWallet(
        chainEnum,
        accountId,
        signerRecord.privateKey,
      );
      let signedTransaction;
      try {
        signedTransaction = await hederaWallet.signTransaction(transaction);
      } catch {
        fail("adapter_transaction_sign_failed");
      }
      verifySignedTransaction(signedTransaction);
      const signedTransactionSha256 = sha256(signedTransaction.toBytes());
      const transactionId = signedTransaction.transactionId?.toString();
      if (!transactionId) fail("adapter_signed_transaction_id_missing");
      const submissionEvidence = {
        executionAttempted: true,
        outcome: "unknown",
        signedTransactionSha256,
        transactionId,
      };
      await beforeSubmit(submissionEvidence);
      executionState = "unknown";
      pendingExecutionState = "unknown";
      let response;
      try {
        response = await hederaWallet.call(signedTransaction);
      } catch {
        sessionBlockedByUnknownSubmission = true;
        fail("adapter_transaction_submission_outcome_unknown", submissionEvidence);
      }
      executionState = "submitted";
      pendingExecutionState = "submitted";
      submittedResponses.set(transactionId, response);
      let responseJson;
      try {
        responseJson = response.toJSON();
      } catch {
        responseJson = null;
      }
      let walletConnectResponseStatus = "delivered";
      try {
        if (!responseJson) throw new Error("response serialization failed");
        await wallet.respondSessionRequest({
          topic: event.topic,
          response: {
            id: event.id,
            result: responseJson,
            jsonrpc: "2.0",
          },
        });
      } catch {
        walletConnectResponseStatus = "failed_after_submission";
      }
      pendingEvent = null;
      return {
        signedTransactionSha256,
        transactionId,
        transactionHash: responseJson?.transactionHash ?? null,
        walletConnectResponseStatus,
      };
    },

    async readSettlement({ transactionId, before, frozenIntent }) {
      const mirrorId = mirrorTransactionId(transactionId);
      const url = `${policy.mirrorBase}/transactions/${encodeURIComponent(
        mirrorId,
      )}`;
      const submittedResponse = submittedResponses.get(transactionId);
      const hapiReceiptPromise = (async () => {
        if (
          typeof submittedResponse?.getReceipt !== "function" ||
          typeof runtime.sdk?.Client?.forName !== "function"
        ) {
          return { status: "unavailable_api", consensus_status: null };
        }
        const client = runtime.sdk.Client.forName(policy.network);
        const timeoutMarker = Symbol("hapi-timeout");
        try {
          const result = await Promise.race([
            submittedResponse.getReceipt(client).then(
              (receipt) => ({ kind: "receipt", receipt }),
              (error) => ({ kind: "error", error }),
            ),
            new Promise((resolve) => {
              const timer = setTimeout(
                () => resolve(timeoutMarker),
                hapiReceiptTimeoutMs,
              );
              timer.unref?.();
            }),
          ]);
          if (result === timeoutMarker) {
            return { status: "transport_timeout", consensus_status: null };
          }
          if (result.kind === "error") {
            const status = result.error?.status?.toString?.() ?? null;
            return {
              status: status ? "consensus_failure" : "transport_error",
              consensus_status: status,
            };
          }
          const status = result.receipt?.status?.toString?.() ?? null;
          return {
            status: status === "SUCCESS" ? "confirmed" : "consensus_failure",
            consensus_status: status,
            serials: (result.receipt?.serials ?? []).map(String),
          };
        } finally {
          client.close?.();
        }
      })();
      let row = null;
      for (let attempt = 1; attempt <= settlementAttempts; attempt += 1) {
        const payload = await fetchJson(
          fetchImpl,
          url,
          "adapter_settlement_read_failed",
          { allowNotFound: true },
        );
        const rows = payload?.transactions;
        if (Array.isArray(rows) && rows.length > 0) {
          row =
            rows.find(
              (candidate) =>
                candidate?.transaction_id === mirrorId ||
                candidate?.transaction_id === transactionId,
            ) ?? null;
          if (row?.result) break;
        }
        if (attempt < settlementAttempts) await delay(settlementIntervalMs);
      }
      if (!row?.result) fail("adapter_settlement_timeout");
      let contractResult = null;
      if (frozenIntent?.transaction?.transaction_type === "ContractExecuteTransaction") {
        const contractUrl = `${policy.mirrorBase}/contracts/results/${encodeURIComponent(
          mirrorId,
        )}`;
        const payload = await fetchJson(
          fetchImpl,
          contractUrl,
          "adapter_contract_result_read_failed",
        );
        if (
          payload?.contract_id !== frozenIntent.transaction.contract_id ||
          payload?.error_message
        ) {
          fail("adapter_contract_result_not_confirmed");
        }
        contractResult = {
          contract_id: payload.contract_id,
          result: payload.result ?? row.result,
          gas_used: payload.gas_used ?? null,
          amount_tinybar: payload.amount ?? null,
          error_message: null,
          mirror_url: contractUrl,
        };
      }
      const after = await snapshotAccount({ frozenIntent });
      const hapiReceipt = await hapiReceiptPromise;
      return {
        chain_id: chainId,
        account_id: accountId,
        transaction_id: transactionId,
        transaction_hash: row.transaction_hash ?? null,
        result: row.result,
        consensus_timestamp: row.consensus_timestamp ?? null,
        mirror_url: url,
        hashscan_url: `${policy.hashscanBase}/${encodeURIComponent(transactionId)}`,
        sdk_hapi_receipt: hapiReceipt,
        mirror_transaction_receipt: {
          transaction_id: row.transaction_id,
          transaction_hash: row.transaction_hash ?? null,
          result: row.result,
          charged_tx_fee_tinybar: row.charged_tx_fee ?? null,
          entity_id: row.entity_id ?? null,
          name: row.name ?? null,
          transfers: boundedRows(
            row.transfers ?? [],
            ["account", "amount", "is_approval"],
            "adapter_transfer_evidence_invalid",
          ),
          token_transfers: boundedRows(
            row.token_transfers ?? [],
            ["account", "token_id", "amount", "is_approval"],
            "adapter_transfer_evidence_invalid",
          ),
          nft_transfers: boundedRows(
            row.nft_transfers ?? [],
            ["token_id", "serial_number", "sender_account_id", "receiver_account_id", "is_approval"],
            "adapter_transfer_evidence_invalid",
          ),
        },
        contract_result: contractResult,
        before,
        after,
        deltas: balanceDeltas(before, after),
      };
    },

    async rejectPendingRequest() {
      if (!pendingEvent) return;
      if (pendingExecutionState !== "not_attempted") {
        fail("adapter_request_rejection_after_submission_forbidden");
      }
      const event = pendingEvent;
      pendingEvent = null;
      try {
        await wallet.rejectSessionRequest(event, {
          code: 5000,
          message: "Request rejected before execution",
        });
      } catch {
        fail("adapter_request_rejection_failed");
      }
    },

    async close() {
      if (closed) return closeReceipt;
      closed = true;
      const failures = [];
      let pendingRequestStatus = pendingEvent ? "pending" : "none";
      let sessionStatus = sessionEnded
        ? "ended_by_peer"
        : session
          ? "active"
          : "none";
      if (pendingEvent && pendingExecutionState === "not_attempted") {
        try {
          await wallet.rejectSessionRequest(pendingEvent, {
            code: 5000,
            message: "Signer stopped before execution",
          });
          pendingRequestStatus = "rejected_before_submission";
        } catch {
          failures.push("pending_request_rejection_failed");
          pendingRequestStatus = "rejection_failed";
        }
        pendingEvent = null;
      } else if (pendingEvent && pendingExecutionState === "response_unknown") {
        pendingRequestStatus = "message_response_unknown_not_rejected";
      }
      if (session && !sessionEnded && executionState !== "unknown") {
        try {
          await wallet.disconnectSession({
            topic: session.topic,
            reason: { code: 6000, message: "Signer session complete" },
          });
          sessionStatus = "disconnected";
        } catch {
          failures.push("session_disconnect_failed");
          sessionStatus = "disconnect_failed";
        }
        session = null;
      } else if (session && executionState === "unknown") {
        sessionStatus = "submission_unknown_transport_closed";
        pendingRequestStatus = "submission_unknown_not_rejected";
      }
      let transportStatus = "closed";
      try {
        await core.relayer.transportClose();
      } catch {
        failures.push("transport_close_failed");
        transportStatus = "close_failed";
      } finally {
        detachSessionListeners();
        signerRecord = null;
      }
      closeReceipt = {
        status: failures.length === 0 ? "closed" : "closed_degraded",
        session_status: sessionStatus,
        pending_request_status: pendingRequestStatus,
        transport_status: transportStatus,
        execution_state: executionState,
        signer_key_status: "released",
      };
      if (failures.length > 0) closeReceipt.failure_codes = failures;
      return closeReceipt;
    },
  };
}

export async function createProductionHederaWalletConnectAdapter({
  origin,
  chainId,
  accountId,
  projectId = DEFAULT_PROJECT_ID,
  loadSigner = true,
}) {
  const policy = networkPolicy(origin, chainId, accountId);
  const runtime = loadProductionRuntime();
  return createHederaWalletConnectAdapter({
    origin,
    chainId,
    accountId,
    projectId,
    runtime,
    loadSigner:
      loadSigner === true
        ? async () => loadBoundPrivateKey({ sdk: runtime.sdk, policy, accountId })
        : null,
  });
}
