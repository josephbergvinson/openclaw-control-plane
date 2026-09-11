import { binding as operatorBinding } from "../scripts/routing_operator_bindings.mjs";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import {
  adapterCapabilityDescriptor,
  createHederaWalletConnectAdapter,
  isTrustedCompanyAlphaOriginForChain,
  validateProductionRuntimeApi,
} from "../scripts/lib/company_alpha_walletconnect_hedera_adapter.mjs";

const ORIGIN = "https://dev.company-alpha.example.invalid";
const CHAIN_ID = "hedera:testnet";
const ACCOUNT_ID = operatorBinding("services.wallets.hedera_testnet.account_id");
const CONTRACT_ID = "0.0.19264";
const TRANSACTION_ID = `${ACCOUNT_ID}@1787932000.123456789`;
const MIRROR_TRANSACTION_ID = `${ACCOUNT_ID}-1787932000-123456789`;
const PAIRING_URI = `wc:${"a".repeat(64)}@2?relay-protocol=irn&symKey=${"b".repeat(64)}`;

const stringValue = (value) => ({ toString: () => String(value) });

test("adapter advertises the two methods CompanyAlpha uses", () => {
  assert.deepEqual(adapterCapabilityDescriptor().supported_methods, [
    "hedera_signAndExecuteTransaction",
    "hedera_signMessage",
  ]);
});

test("adapter reports observed dependency versions without treating them as policy", () => {
  const observed = {
    hedera_wallet_connect: "future-compatible",
    hashgraph_sdk: "another-compatible-build",
  };

  assert.deepEqual(
    adapterCapabilityDescriptor(observed).dependency_versions,
    observed,
  );
  assert.equal(adapterCapabilityDescriptor().dependency_versions, undefined);
});

test("production dependency validation is API-shaped rather than version-shaped", () => {
  const valid = {
    sdk: {
      PrivateKey: {
        fromStringECDSA() {},
        fromStringED25519() {},
      },
    },
    Core: class Core {},
    walletConnect: { Wallet: class Wallet {} },
  };

  assert.equal(validateProductionRuntimeApi(valid), undefined);
  assert.throws(
    () =>
      validateProductionRuntimeApi({
        ...valid,
        walletConnect: {},
      }),
    (error) => error?.code === "adapter_dependency_api_mismatch",
  );
});

test("origin classes are bound to the selected Hedera network", () => {
  assert.equal(
    isTrustedCompanyAlphaOriginForChain(
      "https://dev.company-alpha.example.invalid",
      "hedera:testnet",
    ),
    true,
  );
  assert.equal(
    isTrustedCompanyAlphaOriginForChain("http://localhost:3000", "hedera:testnet"),
    true,
  );
  assert.equal(
    isTrustedCompanyAlphaOriginForChain(
      "https://staging-2.company-alpha.example.invalid",
      "hedera:mainnet",
    ),
    true,
  );
  assert.equal(
    isTrustedCompanyAlphaOriginForChain(
      "https://company-alpha.example.invalid",
      "hedera:mainnet",
    ),
    true,
  );
  assert.equal(
    isTrustedCompanyAlphaOriginForChain(
      "https://dev.company-alpha.example.invalid",
      "hedera:mainnet",
    ),
    false,
  );
  assert.equal(
    isTrustedCompanyAlphaOriginForChain("http://localhost:3000", "hedera:mainnet"),
    false,
  );
});

class ContractExecuteTransaction {
  constructor(accountId = ACCOUNT_ID) {
    this.transactionId = {
      accountId: stringValue(accountId),
      toString: () => TRANSACTION_ID,
    };
    this.contractId = stringValue(CONTRACT_ID);
  }

  toBytes() {
    return Buffer.from("signed-site-generated-transaction");
  }
}

function runtimeFixture({
  pairFailure = false,
  responseFailure = false,
  submissionUnknown = false,
  rejectFailure = false,
  disconnectFailure = false,
  transportCloseFailure = false,
  requestSequence = null,
  requestIds = null,
  requestTopics = null,
} = {}) {
  const events = [];
  let wallet = null;
  let balanceReads = 0;
  let signerLoads = 0;
  const transaction = new ContractExecuteTransaction();
  const requests = requestSequence ?? [{
    method: "hedera_signAndExecuteTransaction",
    body: transaction,
  }];
  const sessionEvents = requests.map((_, index) => ({
    id: requestIds?.[index] ?? 42 + index,
    topic: requestTopics?.[index] ?? "session-topic",
  }));
  let emittedRequestIndex = 0;

  class FakeCore {
    constructor() {
      this.relayer = {
        transportClose: async () => {
          events.push("transport_close");
          if (transportCloseFailure) throw new Error("transport close failed");
        },
      };
    }
  }

  class FakeWallet extends EventEmitter {
    constructor() {
      super();
      wallet = this;
      this.engine = { init: async () => events.push("init") };
    }

    async pair() {
      events.push("pair");
      if (pairFailure) throw new Error("pair failed");
      queueMicrotask(() => this.emit("session_proposal", {
        params: {
          proposer: { metadata: { url: `${ORIGIN}/swap` } },
        },
      }));
    }

    async buildAndApproveSession() {
      events.push("approve");
      queueMicrotask(() => this.emit("session_request", sessionEvents[0]));
      return { topic: sessionEvents[0].topic };
    }

    parseSessionRequest(event) {
      events.push("parse");
      const index = sessionEvents.findIndex((candidate) => candidate.id === event.id);
      const request = requests[index];
      return {
        method: request.method,
        chainId: CHAIN_ID,
        accountId: stringValue(ACCOUNT_ID),
        body: request.body,
      };
    }

    getHederaWallet() {
      return {
        signTransaction: async () => {
          events.push("sign");
          return transaction;
        },
        call: async () => {
          events.push("submit");
          if (submissionUnknown) throw new Error("transport ended during submit");
          return {
            toJSON: () => ({ transactionHash: "walletconnect-transaction-hash" }),
            getReceipt: async () => ({
              status: stringValue("SUCCESS"),
              serials: [],
            }),
          };
        },
        sign: async () => {
          events.push("message_sign");
          return [{ signature: Buffer.from("signature") }];
        },
      };
    }

    async hedera_signMessage(id, topic, body, signer) {
      events.push(`sign_message:${body}`);
      assert.equal(id, sessionEvents[emittedRequestIndex].id);
      assert.equal(topic, sessionEvents[emittedRequestIndex].topic);
      await signer.sign([Buffer.from(body)]);
      await this.respondSessionRequest();
    }

    async respondSessionRequest() {
      events.push("respond");
      if (responseFailure) throw new Error("response delivery failed");
    }

    async rejectSessionRequest() {
      events.push("reject");
      if (rejectFailure) throw new Error("reject failed");
    }

    async disconnectSession() {
      events.push("disconnect");
      if (disconnectFailure) throw new Error("disconnect failed");
    }
  }

  const runtime = {
    Core: FakeCore,
    sdk: {
      Client: {
        forName: () => ({ close: () => events.push("hapi_close") }),
      },
    },
    walletConnect: {
      Wallet: FakeWallet,
      HederaChainId: { Testnet: "testnet", Mainnet: "mainnet" },
      HederaSessionEvent: { AccountsChanged: "accountsChanged" },
    },
  };

  const fetchImpl = async (rawUrl) => {
    const url = new URL(rawUrl);
    let payload;
    if (url.pathname.endsWith("/balances")) {
      balanceReads += 1;
      payload = {
        balances: [{
          account: ACCOUNT_ID,
          balance: balanceReads === 1 ? "1000000000" : "899000000",
          tokens: [{
            token_id: "0.0.1183558",
            balance: balanceReads === 1 ? "1000" : "50001000",
          }],
        }],
        links: { next: null },
      };
    } else if (url.pathname.endsWith(`/accounts/${ACCOUNT_ID}`)) {
      payload = {
        account: ACCOUNT_ID,
        deleted: false,
        key: { _type: "ECDSA_SECP256K1", key: "mirror-public-key" },
      };
    } else if (url.pathname.includes("/transactions/")) {
      payload = {
        transactions: [{
          transaction_id: MIRROR_TRANSACTION_ID,
          transaction_hash: "mirror-transaction-hash",
          result: "SUCCESS",
          consensus_timestamp: "1787932001.000000001",
          charged_tx_fee: "100000",
          entity_id: CONTRACT_ID,
          name: "CONTRACTCALL",
          transfers: [{
            account: ACCOUNT_ID,
            amount: "-101000000",
            is_approval: false,
          }],
          token_transfers: [{
            account: ACCOUNT_ID,
            token_id: "0.0.1183558",
            amount: "50000000",
            is_approval: false,
          }],
          nft_transfers: [],
        }],
      };
    } else if (url.pathname.includes("/contracts/results/")) {
      payload = {
        contract_id: CONTRACT_ID,
        result: "SUCCESS",
        gas_used: 500000,
        amount: 100000000,
        error_message: null,
      };
    } else {
      throw new Error(`unexpected fake Mirror URL: ${url}`);
    }
    return { ok: true, status: 200, json: async () => payload };
  };

  return {
    events,
    get wallet() {
      return wallet;
    },
    runtime,
    fetchImpl,
    transaction,
    async loadSigner() {
      signerLoads += 1;
      return {
        privateKey: { kind: "test-private-key-handle" },
        keyType: "ECDSA_SECP256K1",
      };
    },
    get signerLoads() {
      return signerLoads;
    },
    get balanceReads() {
      return balanceReads;
    },
    emitNextRequest() {
      emittedRequestIndex += 1;
      const event = sessionEvents[emittedRequestIndex];
      if (!event) throw new Error("no more fake requests");
      wallet.emit("session_request", event);
    },
  };
}

async function createAdapter(fixture) {
  return createHederaWalletConnectAdapter({
    origin: ORIGIN,
    chainId: CHAIN_ID,
    accountId: ACCOUNT_ID,
    loadSigner: fixture.loadSigner,
    runtime: fixture.runtime,
    fetchImpl: fixture.fetchImpl,
    settlementAttempts: 1,
    settlementIntervalMs: 0,
    hapiReceiptTimeoutMs: 100,
  });
}

async function capture(adapter) {
  return adapter.captureRequest({
    pairingUri: PAIRING_URI,
    origin: ORIGIN,
    chainId: CHAIN_ID,
    accountId: ACCOUNT_ID,
    validateProposal: () => ({ verify_status: "UNKNOWN" }),
  });
}

test("adapter pairs, signs, settles, and closes one request", async () => {
  const fixture = runtimeFixture();
  const adapter = await createAdapter(fixture);
  const captured = await capture(adapter);
  const frozenIntent = {
    transaction: {
      transaction_type: "ContractExecuteTransaction",
      contract_id: CONTRACT_ID,
    },
  };
  const before = await adapter.snapshotAccount({ frozenIntent });
  const execution = await adapter.signAndExecute({
    event: captured.event,
    transaction: captured.parsedRequest.body,
    verifySignedTransaction: (signed) => assert.equal(signed, fixture.transaction),
    beforeSubmit: (evidence) => {
      assert.equal(evidence.outcome, "unknown");
      assert.equal(evidence.transactionId, TRANSACTION_ID);
    },
  });
  const settlement = await adapter.readSettlement({
    transactionId: execution.transactionId,
    before,
    frozenIntent,
  });
  const close = await adapter.close();

  assert.equal(execution.walletConnectResponseStatus, "delivered");
  assert.equal(settlement.result, "SUCCESS");
  assert.equal(settlement.sdk_hapi_receipt.status, "confirmed");
  assert.equal(settlement.contract_result.contract_id, CONTRACT_ID);
  assert.equal(settlement.deltas.hbar_tinybar, "-101000000");
  assert.deepEqual(settlement.deltas.tokens, [{
    token_id: "0.0.1183558",
    amount: "50000000",
  }]);
  assert.deepEqual(close, {
    status: "closed",
    session_status: "disconnected",
    pending_request_status: "none",
    transport_status: "closed",
    execution_state: "submitted",
    signer_key_status: "released",
  });
  assert.deepEqual(fixture.events, [
    "init",
    "pair",
    "approve",
    "parse",
    "sign",
    "submit",
    "respond",
    "hapi_close",
    "disconnect",
    "transport_close",
  ]);
});

test("one session handles message, transaction, and message requests in order", async () => {
  const transaction = new ContractExecuteTransaction();
  const fixture = runtimeFixture({
    requestSequence: [
      { method: "hedera_signMessage", body: "CompanyAlpha sign-in challenge" },
      { method: "hedera_signAndExecuteTransaction", body: transaction },
      { method: "hedera_signMessage", body: "PartialFillLimitOrder canonical" },
    ],
  });
  const adapter = await createAdapter(fixture);
  const first = await capture(adapter);
  assert.equal(first.parsedRequest.method, "hedera_signMessage");
  await adapter.signMessage({ event: first.event, message: first.parsedRequest.body });

  const secondWait = adapter.waitForNextRequest();
  fixture.emitNextRequest();
  const second = await secondWait;
  assert.equal(second.parsedRequest.method, "hedera_signAndExecuteTransaction");
  const before = await adapter.snapshotAccount();
  const execution = await adapter.signAndExecute({
    event: second.event,
    transaction: second.parsedRequest.body,
    verifySignedTransaction: () => {},
    beforeSubmit: async () => {},
  });
  await adapter.readSettlement({
    transactionId: execution.transactionId,
    before,
    frozenIntent: null,
  });

  const thirdWait = adapter.waitForNextRequest();
  fixture.emitNextRequest();
  const third = await thirdWait;
  assert.equal(third.parsedRequest.method, "hedera_signMessage");
  await adapter.signMessage({ event: third.event, message: third.parsedRequest.body });
  const close = await adapter.close();

  assert.equal(fixture.signerLoads, 1);
  assert.equal(fixture.balanceReads, 2);
  assert.equal(close.status, "closed");
  assert.equal(close.execution_state, "submitted");
  assert.deepEqual(
    fixture.events.filter((event) => event.startsWith("sign_message:")),
    [
      "sign_message:CompanyAlpha sign-in challenge",
      "sign_message:PartialFillLimitOrder canonical",
    ],
  );
  assert.equal(fixture.events.filter((event) => event === "submit").length, 1);
});

for (const entry of [
  {
    name: "duplicate request id",
    requestIds: [42, 42],
    requestTopics: null,
    code: "adapter_session_request_duplicate",
  },
  {
    name: "different session topic",
    requestIds: null,
    requestTopics: ["session-topic", "other-session-topic"],
    code: "adapter_session_request_binding_mismatch",
  },
]) {
  test(`session rejects a ${entry.name} before signing`, async () => {
    const fixture = runtimeFixture({
      requestSequence: [
        { method: "hedera_signMessage", body: "first" },
        { method: "hedera_signMessage", body: "second" },
      ],
      requestIds: entry.requestIds,
      requestTopics: entry.requestTopics,
    });
    const adapter = await createAdapter(fixture);
    const first = await capture(adapter);
    await adapter.signMessage({ event: first.event, message: first.parsedRequest.body });

    const next = adapter.waitForNextRequest();
    fixture.emitNextRequest();
    await assert.rejects(next, (error) => error?.code === entry.code);
    await adapter.close();
    assert.equal(fixture.events.filter((event) => event === "message_sign").length, 1);
  });
}

test("pair failure removes the proposal listener and still permits containment close", async () => {
  const fixture = runtimeFixture({ pairFailure: true });
  const adapter = await createAdapter(fixture);

  await assert.rejects(
    capture(adapter),
    (error) => error?.code === "adapter_walletconnect_pair_failed",
  );
  assert.equal(fixture.wallet.listenerCount("session_proposal"), 0);
  const close = await adapter.close();
  assert.equal(close.status, "closed");
  assert.equal(close.session_status, "none");
  assert.deepEqual(fixture.events, ["init", "pair", "transport_close"]);
});

test("close reports rejection, disconnect, and transport failures truthfully", async () => {
  const fixture = runtimeFixture({
    rejectFailure: true,
    disconnectFailure: true,
    transportCloseFailure: true,
  });
  const adapter = await createAdapter(fixture);
  await capture(adapter);
  const close = await adapter.close();

  assert.deepEqual(close, {
    status: "closed_degraded",
    session_status: "disconnect_failed",
    pending_request_status: "rejection_failed",
    transport_status: "close_failed",
    execution_state: "not_attempted",
    signer_key_status: "released",
    failure_codes: [
      "pending_request_rejection_failed",
      "session_disconnect_failed",
      "transport_close_failed",
    ],
  });
});

test("WalletConnect response failure preserves successful settlement evidence", async () => {
  const fixture = runtimeFixture({ responseFailure: true });
  const adapter = await createAdapter(fixture);
  const captured = await capture(adapter);
  const before = await adapter.snapshotAccount();
  const execution = await adapter.signAndExecute({
    event: captured.event,
    transaction: captured.parsedRequest.body,
    verifySignedTransaction: () => {},
    beforeSubmit: async () => {},
  });
  const settlement = await adapter.readSettlement({
    transactionId: execution.transactionId,
    before,
    frozenIntent: null,
  });
  const close = await adapter.close();

  assert.equal(execution.walletConnectResponseStatus, "failed_after_submission");
  assert.equal(settlement.result, "SUCCESS");
  assert.equal(settlement.sdk_hapi_receipt.status, "confirmed");
  assert.equal(close.execution_state, "submitted");
  assert.ok(fixture.events.includes("respond"));
  assert.ok(fixture.events.includes("disconnect"));
});

test("message response ambiguity is never followed by request rejection", async () => {
  const fixture = runtimeFixture({
    responseFailure: true,
    requestSequence: [{ method: "hedera_signMessage", body: "login" }],
  });
  const adapter = await createAdapter(fixture);
  const captured = await capture(adapter);

  await assert.rejects(
    adapter.signMessage({ event: captured.event, message: captured.parsedRequest.body }),
    (error) => error?.code === "adapter_message_response_outcome_unknown",
  );
  const close = await adapter.close();

  assert.equal(close.status, "closed");
  assert.equal(close.session_status, "disconnected");
  assert.equal(
    close.pending_request_status,
    "message_response_unknown_not_rejected",
  );
  assert.ok(fixture.events.includes("message_sign"));
  assert.ok(fixture.events.includes("respond"));
  assert.ok(!fixture.events.includes("reject"));
});

test("unknown submission returns typed evidence without rejection or disconnect", async () => {
  const fixture = runtimeFixture({ submissionUnknown: true });
  const adapter = await createAdapter(fixture);
  const captured = await capture(adapter);

  let error;
  try {
    await adapter.signAndExecute({
      event: captured.event,
      transaction: captured.parsedRequest.body,
      verifySignedTransaction: () => {},
      beforeSubmit: async () => {},
    });
  } catch (candidate) {
    error = candidate;
  }
  assert.equal(error?.code, "adapter_transaction_submission_outcome_unknown");
  assert.equal(error?.executionEvidence?.outcome, "unknown");
  assert.equal(error?.executionEvidence?.transactionId, TRANSACTION_ID);
  assert.match(error?.executionEvidence?.signedTransactionSha256, /^[0-9a-f]{64}$/);

  const close = await adapter.close();
  assert.equal(close.execution_state, "unknown");
  assert.equal(close.session_status, "submission_unknown_transport_closed");
  assert.equal(close.pending_request_status, "submission_unknown_not_rejected");
  assert.ok(!fixture.events.includes("respond"));
  assert.ok(!fixture.events.includes("reject"));
  assert.ok(!fixture.events.includes("disconnect"));
  assert.equal(fixture.events.at(-1), "transport_close");
});
