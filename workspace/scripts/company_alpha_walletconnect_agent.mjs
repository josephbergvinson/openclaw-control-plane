#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  adapterCapabilityDescriptor,
  createProductionHederaWalletConnectAdapter,
  isTrustedCompanyAlphaOrigin,
  isTrustedCompanyAlphaOriginForChain,
  validateProductionRuntimeDependencies,
} from "./lib/company_alpha_walletconnect_hedera_adapter.mjs";

const REQUEST_SCHEMA = "openclaw.company_alpha_walletconnect_agent_request.v1";
const RECEIPT_SCHEMA = "openclaw.company_alpha_walletconnect_agent_receipt.v1";
const SUBMISSION_CHECKPOINT_SCHEMA =
  "openclaw.company_alpha_walletconnect_submission_checkpoint.v1";
const CAPABILITY_SCHEMA = "openclaw.company_alpha_walletconnect_agent_capability.v1";
const PROBE_SCHEMA = "openclaw.company_alpha_walletconnect_agent_probe.v1";
const SUPPORTED_METHODS = Object.freeze([
  "hedera_signAndExecuteTransaction",
  "hedera_signMessage",
]);
const SUPPORTED_CHAINS = Object.freeze(["hedera:testnet", "hedera:mainnet"]);

export class CompanyAlphaWalletConnectAgentError extends Error {
  constructor(code, executionEvidence = null) {
    super(code);
    this.code = code;
    this.executionEvidence = executionEvidence;
  }
}

function fail(code, executionEvidence = null) {
  throw new CompanyAlphaWalletConnectAgentError(code, executionEvidence);
}

function normalizeOrigin(value) {
  try {
    const origin = new URL(value).origin;
    if (!isTrustedCompanyAlphaOrigin(origin)) fail("origin_not_supported");
    return origin;
  } catch (error) {
    if (error instanceof CompanyAlphaWalletConnectAgentError) throw error;
    fail("origin_invalid");
  }
}

function stringValue(value) {
  return value?.toString?.() ?? String(value ?? "");
}

function assertPrivateFile(filePath) {
  if (!path.isAbsolute(filePath)) fail("request_file_not_absolute");
  let stat;
  let parent;
  try {
    stat = fs.lstatSync(filePath);
    parent = fs.lstatSync(path.dirname(filePath));
  } catch {
    fail("request_file_unavailable");
  }
  if (
    !stat.isFile() ||
    stat.isSymbolicLink() ||
    stat.uid !== process.getuid() ||
    stat.nlink !== 1 ||
    (stat.mode & 0o077) !== 0 ||
    stat.size <= 0 ||
    stat.size > 64 * 1024 ||
    !parent.isDirectory() ||
    parent.isSymbolicLink() ||
    parent.uid !== process.getuid() ||
    (parent.mode & 0o777) !== 0o700
  ) {
    fail("request_file_not_private");
  }
}

function readRequest(filePath) {
  assertPrivateFile(filePath);
  let value;
  try {
    value = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    fail("request_file_invalid_json");
  }
  const origin = normalizeOrigin(value?.origin);
  if (
    value?.schema !== REQUEST_SCHEMA ||
    !SUPPORTED_CHAINS.includes(value?.chain_id) ||
    !/^0\.0\.[0-9]+$/.test(value?.account_id) ||
    typeof value?.pairing_uri !== "string" ||
    !value.pairing_uri.startsWith("wc:") ||
    /\s/.test(value.pairing_uri)
  ) {
    fail("request_binding_invalid");
  }
  if (!isTrustedCompanyAlphaOriginForChain(origin, value.chain_id)) {
    fail("origin_chain_mismatch");
  }
  return { ...value, origin };
}

function consumeRequest(filePath) {
  try {
    fs.unlinkSync(filePath);
    const directory = fs.openSync(path.dirname(filePath), "r");
    try {
      fs.fsyncSync(directory);
    } finally {
      fs.closeSync(directory);
    }
  } catch {
    fail("request_file_consume_failed");
  }
}

function assertedOrigin(candidate, expected, code) {
  if (typeof candidate !== "string" || normalizeOrigin(candidate) !== expected) {
    fail(code);
  }
}

function validateProposal(proposal, expectedOrigin) {
  assertedOrigin(
    proposal?.params?.proposer?.metadata?.url,
    expectedOrigin,
    "proposal_origin_mismatch",
  );
  const verified = proposal?.verifyContext?.verified;
  if (verified?.origin) {
    assertedOrigin(verified.origin, expectedOrigin, "proposal_origin_mismatch");
  }
  return {
    claimed_origin: expectedOrigin,
    verify_status: verified?.validation ?? "UNAVAILABLE",
  };
}

function transactionSummary(transaction) {
  const transactionType = transaction?.constructor?.name ?? "UnknownTransaction";
  const transactionId = stringValue(transaction?.transactionId);
  const payerAccount = stringValue(transaction?.transactionId?.accountId);
  const contractId =
    stringValue(transaction?.contractId ?? transaction?._contractId) || null;
  return { transactionType, transactionId, payerAccount, contractId };
}

function transactionContext(summary) {
  if (summary.transactionType !== "ContractExecuteTransaction") return null;
  return {
    transaction: {
      transaction_type: summary.transactionType,
      contract_id: summary.contractId,
    },
  };
}

function validateParsedRequest(parsed, request) {
  if (
    !SUPPORTED_METHODS.includes(parsed?.method) ||
    parsed?.chainId !== request.chain_id ||
    stringValue(parsed?.accountId) !== request.account_id
  ) {
    fail("wallet_request_binding_mismatch");
  }
  if (parsed.method === "hedera_signMessage") {
    if (typeof parsed.body !== "string") fail("wallet_message_body_invalid");
    return { method: parsed.method, summary: null, context: null };
  }
  const summary = transactionSummary(parsed.body);
  if (summary.payerAccount && summary.payerAccount !== request.account_id) {
    fail("transaction_payer_mismatch");
  }
  return {
    method: parsed.method,
    summary,
    context: transactionContext(summary),
  };
}

function safeExecutionEvidence(error) {
  const evidence = error?.executionEvidence;
  if (!evidence || typeof evidence !== "object") return null;
  return {
    executionAttempted: evidence.executionAttempted === true,
    outcome: typeof evidence.outcome === "string" ? evidence.outcome : "unknown",
    signedTransactionSha256:
      typeof evidence.signedTransactionSha256 === "string"
        ? evidence.signedTransactionSha256
        : null,
    transactionId:
      typeof evidence.transactionId === "string" ? evidence.transactionId : null,
  };
}

function publicReceipt({
  status,
  mode,
  request,
  proposal = null,
  summary = null,
  execution = null,
  settlement = null,
  requests = [],
  close = null,
  failureCode = null,
}) {
  return {
    schema: RECEIPT_SCHEMA,
    status,
    mode,
    origin: request.origin,
    chain_id: request.chain_id,
    account_id: request.account_id,
    captured_at_utc: request.captured_at_utc ?? null,
    proposal,
    transaction: summary
      ? {
          type: summary.transactionType,
          id: summary.transactionId || null,
          contract_id: summary.contractId,
        }
      : null,
    execution,
    settlement,
    requests,
    request_count: requests.length,
    close,
    failure_code: failureCode,
    pairing_material_exposed: false,
  };
}

function writePrivateJsonExclusive(filePath, payload, errorCode) {
  if (!path.isAbsolute(filePath)) fail("receipt_file_not_absolute");
  const encoded = `${JSON.stringify(payload)}\n`;
  if (
    encoded.includes("wc:") ||
    encoded.includes("symKey=") ||
    encoded.includes("pairing_uri")
  ) {
    fail("receipt_contains_pairing_material");
  }
  let descriptor = null;
  try {
    descriptor = fs.openSync(filePath, "wx", 0o600);
    fs.writeFileSync(descriptor, encoded, { encoding: "utf8" });
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    const directory = fs.openSync(path.dirname(filePath), "r");
    try {
      fs.fsyncSync(directory);
    } finally {
      fs.closeSync(directory);
    }
  } catch {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch {
        // Preserve the original typed write failure.
      }
    }
    fail(errorCode);
  }
}

function writeReceipt(filePath, payload) {
  writePrivateJsonExclusive(filePath, payload, "receipt_file_write_failed");
}

function writeSubmissionCheckpoint(
  receiptFile,
  request,
  summary,
  evidence,
  requestIndex,
) {
  const suffix =
    requestIndex === 1
      ? ".submission.json"
      : `.submission-${requestIndex}.json`;
  writePrivateJsonExclusive(
    `${receiptFile}${suffix}`,
    {
      schema: SUBMISSION_CHECKPOINT_SCHEMA,
      status: "prepared_for_single_submit",
      recorded_at: new Date().toISOString(),
      origin: request.origin,
      chain_id: request.chain_id,
      account_id: request.account_id,
      transaction: {
        type: summary.transactionType,
        id: evidence.transactionId,
        contract_id: summary.contractId,
      },
      signed_transaction_sha256: evidence.signedTransactionSha256,
      submission_may_have_occurred: true,
      retry_allowed_without_reconciliation: false,
      pairing_material_exposed: false,
    },
    "submission_checkpoint_write_failed",
  );
}

async function safeClose(adapter) {
  try {
    return await adapter.close();
  } catch {
    return {
      status: "close_failed",
      session_status: "unknown",
      pending_request_status: "unknown",
      transport_status: "unknown",
      execution_state: "unknown",
      signer_key_status: "process_exited",
    };
  }
}

export function capabilityDescriptor() {
  return {
    schema: CAPABILITY_SCHEMA,
    execution_modes: ["dry-run", "execute"],
    request_schema: REQUEST_SCHEMA,
    receipt_schema: RECEIPT_SCHEMA,
    supported_methods: [...SUPPORTED_METHODS],
    supported_chains: [...SUPPORTED_CHAINS],
    origin_policy: "company_alpha_finance_or_loopback",
    controls: {
      browser_request_used_as_supplied: true,
      contract_or_router_allowlist: false,
      amount_or_asset_policy: false,
      semantic_calldata_decoder: false,
      private_key_process_bound: true,
      session_handles_multiple_requests: true,
      retry_unknown_submission: false,
      wallet_extension_required: false,
    },
  };
}

export function probe() {
  const versions = validateProductionRuntimeDependencies();
  const adapter = adapterCapabilityDescriptor(versions);
  return {
    schema: PROBE_SCHEMA,
    ok: true,
    checked_at: new Date().toISOString(),
    capability: capabilityDescriptor(),
    production_adapter: adapter,
    zero_effects: {
      private_key_loaded: false,
      relay_connected: false,
      session_paired: false,
      signature_created: false,
      transaction_submitted: false,
      settlement_queried: false,
      funds_moved: false,
      execution_attempts: 0,
    },
  };
}

export async function runCompanyAlphaWalletConnectAgent({
  mode,
  requestFile,
  receiptFile,
  adapterFactory = createProductionHederaWalletConnectAdapter,
}) {
  if (!new Set(["dry-run", "execute"]).has(mode)) fail("mode_invalid");
  const request = readRequest(requestFile);
  let adapter = null;
  let proposal = null;
  let summary = null;
  let execution = null;
  let settlement = null;
  let close = null;
  const requests = [];
  let transactionCount = 0;
  let transactionInProgress = false;
  let responseDegraded = false;
  try {
    adapter = await adapterFactory({
      origin: request.origin,
      chainId: request.chain_id,
      accountId: request.account_id,
      loadSigner: mode === "execute",
    });
    consumeRequest(requestFile);
    let captured = await adapter.captureRequest({
      pairingUri: request.pairing_uri,
      origin: request.origin,
      chainId: request.chain_id,
      accountId: request.account_id,
      validateProposal: (candidate) => validateProposal(candidate, request.origin),
    });
    proposal = captured.proposalReceipt;
    if (mode === "dry-run") {
      const parsed = captured.parsedRequest;
      const validated = validateParsedRequest(parsed, request);
      summary = validated.summary;
      await adapter.rejectPendingRequest();
      requests.push({ index: 1, method: parsed.method, status: "validated" });
      close = await safeClose(adapter);
      const receipt = publicReceipt({
        status: "validated_no_effect",
        mode,
        request,
        proposal,
        summary,
        requests,
        close,
      });
      writeReceipt(receiptFile, receipt);
      return receipt;
    }
    while (captured) {
      const index = requests.length + 1;
      const parsed = captured.parsedRequest;
      const validated = validateParsedRequest(parsed, request);
      if (validated.method === "hedera_signMessage") {
        const result = await adapter.signMessage({
          event: captured.event,
          message: parsed.body,
        });
        requests.push({
          index,
          method: parsed.method,
          status:
            result?.walletConnectResponseStatus === "delivered"
              ? "succeeded"
              : "response_degraded",
        });
        responseDegraded ||= result?.walletConnectResponseStatus !== "delivered";
      } else {
        transactionCount += 1;
        summary = validated.summary;
        const context = validated.context;
        const before = await adapter.snapshotAccount({ frozenIntent: context });
        transactionInProgress = true;
        execution = await adapter.signAndExecute({
          event: captured.event,
          transaction: parsed.body,
          verifySignedTransaction: (signed) => {
            const signedSummary = transactionSummary(signed);
            if (signedSummary.payerAccount !== request.account_id) {
              fail("signed_transaction_payer_mismatch");
            }
          },
          beforeSubmit: async (evidence) => {
            writeSubmissionCheckpoint(
              receiptFile,
              request,
              summary,
              evidence,
              transactionCount,
            );
          },
        });
        settlement = await adapter.readSettlement({
          transactionId: execution.transactionId,
          before,
          frozenIntent: context,
        });
        if (
          settlement?.result !== "SUCCESS" ||
          settlement?.sdk_hapi_receipt?.status === "consensus_failure"
        ) {
          fail("transaction_settlement_failed", {
            executionAttempted: true,
            outcome: "settled_failure",
            signedTransactionSha256: execution.signedTransactionSha256,
            transactionId: execution.transactionId,
          });
        }
        responseDegraded ||=
          execution?.walletConnectResponseStatus !== "delivered";
        transactionInProgress = false;
        requests.push({
          index,
          method: parsed.method,
          status:
            execution?.walletConnectResponseStatus === "delivered"
              ? "succeeded"
              : "response_degraded",
          transaction: {
            type: summary.transactionType,
            id: execution.transactionId,
            contract_id: summary.contractId,
          },
          execution,
          settlement,
        });
      }
      try {
        captured = await adapter.waitForNextRequest();
      } catch (error) {
        if (error?.code === "adapter_session_idle_timeout") break;
        throw error;
      }
    }
    close = await safeClose(adapter);
    const successStatus =
      close?.status !== "closed"
        ? "succeeded_close_degraded"
        : responseDegraded
          ? "succeeded_response_degraded"
          : "succeeded";
    const receipt = publicReceipt({
      status: successStatus,
      mode,
      request,
      proposal,
      summary,
      execution,
      settlement,
      requests,
      close,
    });
    writeReceipt(receiptFile, receipt);
    return receipt;
  } catch (error) {
    close = close ?? (adapter ? await safeClose(adapter) : null);
    const evidence = safeExecutionEvidence(error);
    const effectiveExecution = execution ?? evidence;
    const status =
      error?.code === "adapter_message_response_outcome_unknown"
        ? "message_response_unknown"
        : evidence?.outcome === "unknown"
        ? "submitted_unknown"
        : transactionInProgress && effectiveExecution
          ? "submitted_unconfirmed"
          : requests.length > 0
            ? "failed_after_partial_success"
          : "failed";
    const receipt = publicReceipt({
      status,
      mode,
      request,
      proposal,
      summary,
      execution: effectiveExecution,
      settlement,
      requests,
      close,
      failureCode: error?.code ?? "walletconnect_agent_failed",
    });
    writeReceipt(receiptFile, receipt);
    return receipt;
  }
}

function parseArgs(argv) {
  if (argv.length === 2 && argv[0] === "--probe" && argv[1] === "--json") {
    return { mode: "probe" };
  }
  if (
    argv.length !== 6 ||
    !["--dry-run", "--execute"].includes(argv[0]) ||
    argv[1] !== "--request-file" ||
    argv[3] !== "--receipt-file" ||
    argv[5] !== "--json"
  ) {
    fail("usage_invalid");
  }
  return {
    mode: argv[0] === "--execute" ? "execute" : "dry-run",
    requestFile: argv[2],
    receiptFile: argv[4],
  };
}

const isMain =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  try {
    const args = parseArgs(process.argv.slice(2));
    if (args.mode === "probe") {
      process.stdout.write(`${JSON.stringify(probe())}\n`);
      process.exit(0);
    }
    const receipt = await runCompanyAlphaWalletConnectAgent(args);
    const ok =
      receipt.status === "validated_no_effect" ||
      receipt.status.startsWith("succeeded");
    process.stdout.write(`${JSON.stringify({
      ok,
      status: receipt.status,
      origin: receipt.origin,
      chain_id: receipt.chain_id,
      account_id: receipt.account_id,
      transaction_id: receipt.execution?.transactionId ?? null,
      failure_code: receipt.failure_code,
      pairing_material_exposed: false,
    })}\n`);
    process.exit(ok ? 0 : 1);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({
      ok: false,
      status: "failed",
      error: error?.code ?? "walletconnect_agent_failed",
      pairing_material_exposed: false,
    })}\n`);
    process.exit(1);
  }
}
