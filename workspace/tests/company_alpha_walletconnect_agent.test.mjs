import { binding as operatorBinding } from "../scripts/routing_operator_bindings.mjs";
import assert from "node:assert/strict";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  CompanyAlphaWalletConnectAgentError,
  runCompanyAlphaWalletConnectAgent,
} from "../scripts/company_alpha_walletconnect_agent.mjs";

const ORIGIN = "https://staging-2.company-alpha.example.invalid";
const CHAIN_ID = "hedera:mainnet";
const ACCOUNT_ID = operatorBinding("services.wallets.hedera_mainnet.account_id");
const TRANSACTION_ID = `${ACCOUNT_ID}@1787932000.123456789`;
const SIGNED_SHA256 = "c".repeat(64);
const PAIRING_URI = `wc:${"a".repeat(64)}@2?relay-protocol=irn&symKey=${"b".repeat(64)}`;

const stringValue = (value) => ({ toString: () => String(value) });
const transactionId = (accountId = ACCOUNT_ID, value = TRANSACTION_ID) => ({
  accountId: stringValue(accountId),
  toString: () => value,
});

class ContractExecuteTransaction {
  constructor({ payer = ACCOUNT_ID, contractId = "0.0.10628061" } = {}) {
    this.transactionId = transactionId(payer);
    this.contractId = stringValue(contractId);
    this.functionParameters = Buffer.from("opaque-site-generated-calldata");
  }
}

class TransferTransaction {
  constructor({ payer = ACCOUNT_ID } = {}) {
    this.transactionId = transactionId(payer);
    this.transfers = [{ accountId: ACCOUNT_ID, amount: "-1" }];
  }
}

function requestValue(overrides = {}) {
  return {
    schema: "openclaw.company_alpha_walletconnect_agent_request.v1",
    origin: ORIGIN,
    chain_id: CHAIN_ID,
    account_id: ACCOUNT_ID,
    pairing_uri: PAIRING_URI,
    captured_at_utc: "2026-08-28T18:30:00.000Z",
    ...overrides,
  };
}

function privateFiles(t, value = requestValue()) {
  const directory = mkdtempSync(path.join(tmpdir(), "openclaw-wc-agent-test-"));
  chmodSync(directory, 0o700);
  const requestFile = path.join(directory, "request.json");
  const receiptFile = path.join(directory, "receipt.json");
  const submissionFile = `${receiptFile}.submission.json`;
  writeFileSync(requestFile, `${JSON.stringify(value)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(requestFile, 0o600);
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return { directory, requestFile, receiptFile, submissionFile };
}

function proposal({
  origin = ORIGIN,
  validation = "UNKNOWN",
  isScam = false,
  includeVerify = true,
} = {}) {
  const value = {
    params: { proposer: { metadata: { url: `${origin}/swap` } } },
  };
  if (includeVerify) {
    value.verifyContext = {
      verified: { origin, validation, isScam },
    };
  }
  return value;
}

function settlement({ result = "SUCCESS", hapiStatus = "confirmed" } = {}) {
  return {
    chain_id: CHAIN_ID,
    account_id: ACCOUNT_ID,
    transaction_id: TRANSACTION_ID,
    transaction_hash: "mirror-transaction-hash",
    result,
    sdk_hapi_receipt: {
      status: hapiStatus,
      consensus_status: hapiStatus === "confirmed" ? "SUCCESS" : "FAIL_INVALID",
    },
    mirror_transaction_receipt: { result },
    mirror_url: "https://mainnet-public.mirrornode.hedera.com/api/v1/transactions/fake",
    hashscan_url: "https://hashscan.io/mainnet/transaction/fake",
    before: { hbar_tinybar: "1000", tokens: [] },
    after: { hbar_tinybar: "999", tokens: [] },
    deltas: { hbar_tinybar: "-1", tokens: [] },
  };
}

function fakeAdapter({
  body = new ContractExecuteTransaction(),
  method = "hedera_signAndExecuteTransaction",
  sequence = null,
  parsedChainId = CHAIN_ID,
  parsedAccountId = ACCOUNT_ID,
  proposalValue = proposal(),
  signedTransaction = body,
  settlementValue = settlement(),
  settlementError = null,
  submissionUnknown = false,
  messageResponseUnknown = false,
} = {}) {
  const calls = [];
  const event = { id: 41, topic: "session-topic" };
  const requestSequence = sequence ?? [{ method, body }];
  let requestIndex = 0;
  const capturedRequest = () => {
    const current = requestSequence[requestIndex];
    if (!current) return null;
    return {
      proposalReceipt: null,
      event: { ...event, id: event.id + requestIndex },
      parsedRequest: {
        method: current.method,
        chainId: current.chainId ?? parsedChainId,
        accountId: stringValue(current.accountId ?? parsedAccountId),
        body: current.body,
      },
    };
  };
  return {
    calls,
    async captureRequest({ validateProposal }) {
      calls.push("capture");
      const captured = capturedRequest();
      captured.proposalReceipt = validateProposal(proposalValue);
      return captured;
    },
    async waitForNextRequest() {
      calls.push("wait_next");
      requestIndex += 1;
      return capturedRequest();
    },
    async rejectPendingRequest() {
      calls.push("reject");
    },
    async snapshotAccount() {
      calls.push("snapshot");
      return { hbar_tinybar: "1000", tokens: [] };
    },
    async signMessage() {
      calls.push("sign_message");
      if (messageResponseUnknown) {
        throw new CompanyAlphaWalletConnectAgentError(
          "adapter_message_response_outcome_unknown",
        );
      }
      return { walletConnectResponseStatus: "delivered" };
    },
    async signAndExecute({ verifySignedTransaction, beforeSubmit }) {
      calls.push("sign_execute");
      verifySignedTransaction(signedTransaction);
      const evidence = {
        executionAttempted: true,
        outcome: "unknown",
        signedTransactionSha256: SIGNED_SHA256,
        transactionId: TRANSACTION_ID,
      };
      await beforeSubmit(evidence);
      if (submissionUnknown) {
        throw new CompanyAlphaWalletConnectAgentError(
          "adapter_transaction_submission_outcome_unknown",
          evidence,
        );
      }
      return {
        signedTransactionSha256: SIGNED_SHA256,
        transactionId: TRANSACTION_ID,
        transactionHash: "mirror-transaction-hash",
        walletConnectResponseStatus: "delivered",
      };
    },
    async readSettlement() {
      calls.push("settlement");
      if (settlementError) throw settlementError;
      return settlementValue;
    },
    async close() {
      calls.push("close");
      return {
        status: "closed",
        session_status: submissionUnknown
          ? "submission_unknown_transport_closed"
          : "disconnected",
        pending_request_status: submissionUnknown
          ? "submission_unknown_not_rejected"
          : "none",
        transport_status: "closed",
        execution_state: submissionUnknown ? "unknown" : "submitted",
        signer_key_status: "released",
      };
    },
  };
}

async function runAgent(t, {
  mode = "execute",
  request = requestValue(),
  adapter = fakeAdapter(),
} = {}) {
  const files = privateFiles(t, request);
  const factoryCalls = [];
  const receipt = await runCompanyAlphaWalletConnectAgent({
    mode,
    requestFile: files.requestFile,
    receiptFile: files.receiptFile,
    adapterFactory: async (options) => {
      factoryCalls.push(options);
      return adapter;
    },
  });
  return { ...files, receipt, adapter, factoryCalls };
}

test("dry-run captures once without loading a key, signing, or producing an effect", async (t) => {
  const adapter = fakeAdapter();
  const result = await runAgent(t, { mode: "dry-run", adapter });

  assert.equal(result.receipt.status, "validated_no_effect");
  assert.deepEqual(result.factoryCalls, [{
    origin: ORIGIN,
    chainId: CHAIN_ID,
    accountId: ACCOUNT_ID,
    loadSigner: false,
  }]);
  assert.deepEqual(adapter.calls, ["capture", "reject", "close"]);
  assert.equal(result.receipt.execution, null);
  assert.equal(result.receipt.settlement, null);
  assert.equal(existsSync(result.submissionFile), false);
});

test("dry-run enforces message shape and transaction payer binding", async (t) => {
  for (const entry of [
    {
      adapter: fakeAdapter({ method: "hedera_signMessage", body: {} }),
      code: "wallet_message_body_invalid",
    },
    {
      adapter: fakeAdapter({
        body: new ContractExecuteTransaction({ payer: "0.0.999" }),
      }),
      code: "transaction_payer_mismatch",
    },
  ]) {
    const result = await runAgent(t, { mode: "dry-run", adapter: entry.adapter });
    assert.equal(result.receipt.status, "failed");
    assert.equal(result.receipt.failure_code, entry.code);
    assert.equal(result.receipt.request_count, 0);
  }
});

for (const [name, body] of [
  ["opaque contract execution", new ContractExecuteTransaction()],
  ["non-contract site transaction", new TransferTransaction()],
]) {
  test(`execute accepts ${name} with UNKNOWN origin verification`, async (t) => {
    const adapter = fakeAdapter({ body, signedTransaction: body });
    const result = await runAgent(t, { adapter });

    assert.equal(result.receipt.status, "succeeded", JSON.stringify(result.receipt));
    assert.equal(result.receipt.proposal.verify_status, "UNKNOWN");
    assert.equal(result.receipt.transaction.type, body.constructor.name);
    assert.deepEqual(adapter.calls, [
      "capture",
      "snapshot",
      "sign_execute",
      "settlement",
      "wait_next",
      "close",
    ]);
  });
}

test("one session signs a CompanyAlpha login and order message without exposing either", async (t) => {
  const login = "Sign this message from CompanyAlpha. Nonce: private-login-nonce";
  const order = "PartialFillLimitOrder private canonical body";
  const adapter = fakeAdapter({
    sequence: [
      { method: "hedera_signMessage", body: login },
      { method: "hedera_signMessage", body: order },
    ],
  });
  const result = await runAgent(t, { adapter });

  assert.equal(result.receipt.status, "succeeded");
  assert.equal(result.receipt.request_count, 2);
  assert.deepEqual(result.receipt.requests, [
    { index: 1, method: "hedera_signMessage", status: "succeeded" },
    { index: 2, method: "hedera_signMessage", status: "succeeded" },
  ]);
  assert.deepEqual(adapter.calls, [
    "capture",
    "sign_message",
    "wait_next",
    "sign_message",
    "wait_next",
    "close",
  ]);
  assert.equal(result.receipt.transaction, null);
  assert.equal(result.receipt.execution, null);
  assert.equal(result.receipt.settlement, null);
  assert.equal(existsSync(result.submissionFile), false);
  const encoded = readFileSync(result.receiptFile, "utf8");
  assert.equal(encoded.includes(login), false);
  assert.equal(encoded.includes(order), false);
  assert.equal(encoded.includes("signature"), false);
});

test("message response ambiguity is reported without message disclosure", async (t) => {
  const message = "Sign this message from CompanyAlpha. Nonce: private";
  const result = await runAgent(t, {
    adapter: fakeAdapter({
      method: "hedera_signMessage",
      body: message,
      messageResponseUnknown: true,
    }),
  });

  assert.equal(result.receipt.status, "message_response_unknown");
  assert.equal(
    result.receipt.failure_code,
    "adapter_message_response_outcome_unknown",
  );
  assert.equal(result.receipt.request_count, 0);
  assert.equal(readFileSync(result.receiptFile, "utf8").includes(message), false);
});

test("provider verification metadata stays advisory after exact origin binding", async (t) => {
  const missingVerify = await runAgent(t, {
    mode: "dry-run",
    adapter: fakeAdapter({ proposalValue: proposal({ includeVerify: false }) }),
  });
  assert.equal(missingVerify.receipt.status, "validated_no_effect");
  assert.equal(missingVerify.receipt.proposal.verify_status, "UNAVAILABLE");

  const scamAdapter = fakeAdapter({
    proposalValue: proposal({ validation: "VALID", isScam: true }),
  });
  const scam = await runAgent(t, { mode: "dry-run", adapter: scamAdapter });
  assert.equal(scam.receipt.status, "validated_no_effect");
  assert.deepEqual(scamAdapter.calls, ["capture", "reject", "close"]);
});

test("origin, chain, account, payer, and signed-payer mismatches fail before submission", async (t) => {
  const cases = [
    {
      name: "origin",
      adapter: fakeAdapter({
        proposalValue: proposal({ origin: "https://dev.company-alpha.example.invalid" }),
      }),
      code: "proposal_origin_mismatch",
    },
    {
      name: "chain",
      adapter: fakeAdapter({ parsedChainId: "hedera:testnet" }),
      code: "wallet_request_binding_mismatch",
    },
    {
      name: "account",
      adapter: fakeAdapter({ parsedAccountId: "0.0.999" }),
      code: "wallet_request_binding_mismatch",
    },
    {
      name: "method",
      adapter: fakeAdapter({ method: "hedera_signTransaction" }),
      code: "wallet_request_binding_mismatch",
    },
    {
      name: "message body",
      adapter: fakeAdapter({ method: "hedera_signMessage", body: {} }),
      code: "wallet_message_body_invalid",
    },
    {
      name: "payer",
      adapter: fakeAdapter({ body: new ContractExecuteTransaction({ payer: "0.0.999" }) }),
      code: "transaction_payer_mismatch",
    },
    {
      name: "signed payer",
      adapter: fakeAdapter({
        signedTransaction: new ContractExecuteTransaction({ payer: "0.0.999" }),
      }),
      code: "signed_transaction_payer_mismatch",
    },
  ];

  for (const entry of cases) {
    await t.test(entry.name, async (nested) => {
      const result = await runAgent(nested, { adapter: entry.adapter });
      assert.equal(result.receipt.status, "failed");
      assert.equal(result.receipt.failure_code, entry.code);
      assert.equal(
        entry.adapter.calls.filter((call) => call === "sign_execute").length,
        entry.name === "signed payer" ? 1 : 0,
      );
      assert.ok(!entry.adapter.calls.includes("settlement"));
      assert.equal(entry.adapter.calls.at(-1), "close");
    });
  }
});

test("one transaction request is handled once, consumed, and closed without retry", async (t) => {
  const result = await runAgent(t);

  assert.equal(result.receipt.status, "succeeded");
  for (const call of [
    "capture",
    "snapshot",
    "sign_execute",
    "settlement",
    "wait_next",
    "close",
  ]) {
    assert.equal(result.adapter.calls.filter((candidate) => candidate === call).length, 1);
  }
  assert.throws(() => lstatSync(result.requestFile), { code: "ENOENT" });
  const checkpoint = JSON.parse(readFileSync(result.submissionFile, "utf8"));
  assert.equal(
    checkpoint.schema,
    "openclaw.company_alpha_walletconnect_submission_checkpoint.v1",
  );
  assert.equal(checkpoint.status, "prepared_for_single_submit");
  assert.equal(checkpoint.transaction.id, TRANSACTION_ID);
  assert.equal(checkpoint.signed_transaction_sha256, SIGNED_SHA256);
  assert.equal(checkpoint.submission_may_have_occurred, true);
  assert.equal(checkpoint.retry_allowed_without_reconciliation, false);
  assert.equal(lstatSync(result.submissionFile).mode & 0o777, 0o600);
});

test("multi-transaction sessions retain settlement evidence for every request", async (t) => {
  const result = await runAgent(t, {
    adapter: fakeAdapter({
      sequence: [
        {
          method: "hedera_signAndExecuteTransaction",
          body: new ContractExecuteTransaction(),
        },
        {
          method: "hedera_signAndExecuteTransaction",
          body: new ContractExecuteTransaction(),
        },
      ],
    }),
  });

  assert.equal(result.receipt.status, "succeeded");
  assert.equal(result.receipt.request_count, 2);
  for (const request of result.receipt.requests) {
    assert.equal(request.method, "hedera_signAndExecuteTransaction");
    assert.equal(request.status, "succeeded");
    assert.equal(request.settlement.result, "SUCCESS");
    assert.equal(request.execution.walletConnectResponseStatus, "delivered");
  }
  assert.equal(existsSync(result.submissionFile), true);
  assert.equal(existsSync(`${result.receiptFile}.submission-2.json`), true);
});

test("receipt is private, exclusive, and contains no WalletConnect material", async (t) => {
  const result = await runAgent(t, { mode: "dry-run" });
  const stat = lstatSync(result.receiptFile);
  const encoded = readFileSync(result.receiptFile, "utf8");

  assert.equal(stat.isFile(), true);
  assert.equal(stat.isSymbolicLink(), false);
  assert.equal(stat.mode & 0o777, 0o600);
  assert.equal(stat.nlink, 1);
  assert.ok(!encoded.includes("wc:"));
  assert.ok(!encoded.includes("symKey="));
  assert.ok(!encoded.includes("pairing_uri"));
  assert.equal(JSON.parse(encoded).pairing_material_exposed, false);

  const collisionFiles = privateFiles(t);
  writeFileSync(collisionFiles.receiptFile, "preserve-existing\n", {
    encoding: "utf8",
    mode: 0o600,
  });
  await assert.rejects(
    runCompanyAlphaWalletConnectAgent({
      mode: "dry-run",
      requestFile: collisionFiles.requestFile,
      receiptFile: collisionFiles.receiptFile,
      adapterFactory: async () => fakeAdapter(),
    }),
    (error) => error?.code === "receipt_file_write_failed",
  );
  assert.equal(readFileSync(collisionFiles.receiptFile, "utf8"), "preserve-existing\n");
});

for (const failure of [
  {
    name: "Mirror reports a failed transaction",
    adapter: fakeAdapter({
      settlementValue: settlement({ result: "CONTRACT_REVERT_EXECUTED" }),
    }),
    code: "transaction_settlement_failed",
  },
  {
    name: "HAPI reports consensus failure",
    adapter: fakeAdapter({
      settlementValue: settlement({ hapiStatus: "consensus_failure" }),
    }),
    code: "transaction_settlement_failed",
  },
  {
    name: "Mirror settlement cannot be read",
    adapter: fakeAdapter({
      settlementError: new CompanyAlphaWalletConnectAgentError(
        "adapter_settlement_read_failed",
      ),
    }),
    code: "adapter_settlement_read_failed",
  },
]) {
  test(`${failure.name} never yields a succeeded receipt`, async (t) => {
    const result = await runAgent(t, { adapter: failure.adapter });
    assert.ok(!result.receipt.status.startsWith("succeeded"));
    assert.equal(result.receipt.status, "submitted_unconfirmed");
    assert.equal(result.receipt.failure_code, failure.code);
    assert.equal(result.adapter.calls.at(-1), "close");
  });
}

test("ambiguous submission is typed submitted_unknown with stable transaction identifiers", async (t) => {
  const adapter = fakeAdapter({ submissionUnknown: true });
  const result = await runAgent(t, { adapter });

  assert.equal(result.receipt.status, "submitted_unknown");
  assert.equal(
    result.receipt.failure_code,
    "adapter_transaction_submission_outcome_unknown",
  );
  assert.equal(result.receipt.execution.transactionId, TRANSACTION_ID);
  assert.equal(
    result.receipt.execution.signedTransactionSha256,
    SIGNED_SHA256,
  );
  assert.equal(result.receipt.execution.outcome, "unknown");
  assert.equal(adapter.calls.filter((call) => call === "sign_execute").length, 1);
  assert.ok(!adapter.calls.includes("settlement"));
  assert.equal(
    result.receipt.close.session_status,
    "submission_unknown_transport_closed",
  );
  const checkpoint = JSON.parse(readFileSync(result.submissionFile, "utf8"));
  assert.equal(checkpoint.transaction.id, TRANSACTION_ID);
  assert.equal(checkpoint.retry_allowed_without_reconciliation, false);
});

test("an ambiguous transaction stops later requests in the same session", async (t) => {
  const adapter = fakeAdapter({
    submissionUnknown: true,
    sequence: [
      { method: "hedera_signMessage", body: "login" },
      {
        method: "hedera_signAndExecuteTransaction",
        body: new ContractExecuteTransaction(),
      },
      { method: "hedera_signMessage", body: "must not be signed" },
    ],
  });
  const result = await runAgent(t, { adapter });

  assert.equal(result.receipt.status, "submitted_unknown");
  assert.deepEqual(result.receipt.requests, [
    { index: 1, method: "hedera_signMessage", status: "succeeded" },
  ]);
  assert.equal(adapter.calls.filter((call) => call === "sign_message").length, 1);
  assert.equal(adapter.calls.filter((call) => call === "sign_execute").length, 1);
  assert.equal(adapter.calls.filter((call) => call === "wait_next").length, 1);
});
