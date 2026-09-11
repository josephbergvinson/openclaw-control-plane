// Portable queue/admission regression fixture with finite diagnostic labels.
// The queue, adoption, admission policy, operation registry and tool-authority comparison remain real.
// These four cases use a mock backend; they do not contact a live provider or channel.
import { expectDefined } from "@openclaw/normalization-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MAIN_SESSION_RECOVERY_WORK_ADMISSION_OWNER } from "../../src/agents/main-session-recovery/main-session-recovery-admission.js";
import { resolveAgentTimeoutMs } from "../../src/agents/timeout.js";
import type { SessionEntry } from "../../src/config/sessions.js";
import { withSystemEventOwner } from "../../src/infra/system-event-ownership.js";
import {
  enqueueSystemEvent,
  enqueueSystemEventEntry,
  peekSystemEventEntries,
  resetSystemEventsForTest,
} from "../../src/infra/system-events.js";
import { MESSAGE_TOOL_ONLY_DELIVERY_HINT } from "../../src/plugin-sdk/message-tool-delivery-hints.js";
import { beginSessionWorkAdmission } from "../../src/sessions/session-lifecycle-admission.js";
import { normalizeSessionDeliveryState } from "../../src/utils/delivery-context.shared.js";
import { hasControlCommand } from "../../src/auto-reply/command-detection.js";
import { runReplyAgent } from "../../src/auto-reply/reply/agent-runner.runtime.js";
import { resolveReplyDirectiveRouting } from "../../src/auto-reply/reply/get-reply-directives-routing.js";
import { prepareReplyRunContext } from "../../src/auto-reply/reply/get-reply-run-context.js";
import {
  loadAgentRunnerRuntime,
  loadEmbeddedAgentRuntime,
  loadSessionUpdatesRuntime,
} from "../../src/auto-reply/reply/get-reply-run-helpers.js";
import { runPreparedReply } from "../../src/auto-reply/reply/get-reply-run.js";
import { buildDirectChatContext, buildGroupChatContext, buildGroupIntro } from "../../src/auto-reply/reply/groups.js";
import { finalizeInboundContext, finalizeInboundContextForSdk } from "../../src/auto-reply/reply/inbound-context.js";
import {
  buildInboundMetaSystemPrompt,
  buildInboundUserContextPrefix,
  resolveInboundUserContextPromptJoiner,
} from "../../src/auto-reply/reply/inbound-meta.js";
import { prepareReplyConversation } from "../../src/auto-reply/reply/prompt-session-context.js";
import { REPLY_RUN_IDLE_SETTLE_TIMEOUT_MS, createReplyOperation } from "../../src/auto-reply/reply/reply-run-registry.js";
import { getActiveReplyRunCount } from "../../src/auto-reply/reply/reply-run-registry.registry.js";
import { testing as replyRunTesting } from "../../src/auto-reply/reply/reply-run-registry.test-support.js";
import { routeReply } from "../../src/auto-reply/reply/route-reply.runtime.js";
import { drainFormattedSystemEvents } from "../../src/auto-reply/reply/session-system-events.js";
import {
  createSourceReplyDeliveryRuntime,
  readSourceReplyDeliveryRuntime,
  type SourceReplyDeliveryRuntimeOptions,
} from "../../src/auto-reply/reply/source-reply-delivery-runtime.js";
import { buildChannelSourceTurnId } from "../../src/auto-reply/reply/source-turn-id.js";
import { withReplySystemEventContext } from "../../src/auto-reply/reply/system-event-session-key.js";
import { resolveTypingMode } from "../../src/auto-reply/reply/typing-mode.js";

vi.hoisted(() => {
  process.stderr.write("[steering:boundary] module:hoisted-start\n");
});
process.stderr.write("[steering:boundary] static-imports:done\n");

vi.mock("../../src/agents/auth-profiles/session-override.js", () => ({
  resolveSessionAuthSelection: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../../src/agents/embedded-agent.runtime.js", () => ({
  abortEmbeddedAgentRun: vi.fn().mockReturnValue(false),
  isEmbeddedAgentRunActive: vi.fn().mockReturnValue(false),
  isEmbeddedAgentRunStreaming: vi.fn().mockReturnValue(false),
  preemptAndDrainEmbeddedHeartbeatRun: vi.fn().mockResolvedValue("not-heartbeat"),
  resolveActiveEmbeddedRunSessionId: vi.fn().mockReturnValue(undefined),
  resolveActiveEmbeddedRunSessionIdBySessionFile: vi.fn().mockReturnValue(undefined),
  resolveEmbeddedSessionLane: vi.fn().mockReturnValue("session:session-key"),
  waitForEmbeddedAgentRunEnd: vi.fn().mockResolvedValue(true),
}));

vi.mock("../../src/agents/harness/hook-helpers.js", () => ({
  runAgentHarnessBeforeMessageWriteHook: vi.fn((params: { message: unknown }) => params.message),
}));

// Harness selection and built-in execution are owned by their focused suites. These tests keep
// the real visible-reply policy resolver while supplying its default OpenClaw harness leaf.
const preparedReplyMockState = vi.hoisted(() => ({
  unexpectedCalls: [] as string[],
}));
const envMockState = vi.hoisted(() => ({ fastTestRuntime: true }));

vi.mock("../../src/infra/env.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../src/infra/env.js")>()),
  isFastTestRuntimeEnv: () => envMockState.fastTestRuntime,
}));

vi.mock("../../src/agents/main-session-recovery/main-session-recovery-owner-release.js", () => ({
  scheduleMainSessionRecoveryPendingTarget: vi.fn(),
}));

vi.mock("../../src/agents/main-session-recovery/main-session-recovery-state.js", () => ({
  isMainRestartRecoveryCandidate: vi.fn().mockReturnValue(false),
}));

vi.mock("../../src/agents/main-session-recovery/main-session-recovery-store.js", () => ({
  claimMainSessionRecoveryOwner: vi.fn(),
  releaseMainSessionRecoveryOwner: vi.fn(),
}));

// Provider profile discovery is owned by thinking.test.ts. Keep the real thinking-policy
// projection here while preventing an unrelated active-plugin and public-artifact graph load.
vi.mock("../../src/plugins/provider-thinking.js", () => ({
  resolveEffectiveThinkingProfile: () => undefined,
}));

vi.mock("../../src/agents/agent-tools.policy.js", () => ({
  resolveEffectiveToolPolicy: (params: {
    config: { tools?: { allow?: string[]; deny?: string[] } };
  }) => ({
    globalPolicy: params.config.tools
      ? { allow: params.config.tools.allow, deny: params.config.tools.deny }
      : undefined,
    globalProviderPolicy: undefined,
    agentPolicy: undefined,
    agentProviderPolicy: undefined,
    profile: undefined,
    providerProfile: undefined,
    profileAlsoAllow: undefined,
    providerProfileAlsoAllow: undefined,
  }),
  resolveGroupToolPolicy: () => undefined,
  resolveInheritedToolPolicyForSession: () => undefined,
  resolveSubagentToolPolicyForSession: () => undefined,
}));

vi.mock("../../src/agents/subagents/spawn/subagent-capabilities.js", () => ({
  isSubagentEnvelopeSession: vi.fn().mockReturnValue(false),
  resolveSubagentCapabilityStore: vi.fn().mockReturnValue(undefined),
}));

const selectAgentHarnessMock = vi.hoisted(() =>
  vi.fn(
    (params: {
      provider: string;
      modelId?: string;
      agentHarnessId?: string;
      agentHarnessRuntimeOverride?: string;
    }) => {
      const isSourceProviderCandidate = params.modelId === undefined;
      const isDefaultModelCandidate =
        params.provider === "anthropic" && params.modelId === "claude-opus-4-1";
      if (
        (!isSourceProviderCandidate && !isDefaultModelCandidate) ||
        params.agentHarnessId ||
        params.agentHarnessRuntimeOverride
      ) {
        preparedReplyMockState.unexpectedCalls.push("selectAgentHarness");
      }
      return { id: "openclaw", deliveryDefaults: {} };
    },
  ),
);
vi.mock("../../src/agents/harness/selection.js", () => ({
  selectAgentHarness: selectAgentHarnessMock,
}));

vi.mock("../../src/agents/model-selection.js", () => ({
  buildModelAliasIndex: vi.fn(
    (params: { cfg: { agents?: { defaults?: { models?: unknown } } } }) => {
      if (params.cfg.agents?.defaults?.models) {
        preparedReplyMockState.unexpectedCalls.push("buildModelAliasIndex");
      }
      return { byAlias: new Map(), byKey: new Map() };
    },
  ),
  resolveDefaultModelForAgent: vi.fn(
    (params: { cfg: { agents?: { defaults?: { model?: unknown } } } }) => {
      if (params.cfg.agents?.defaults?.model) {
        preparedReplyMockState.unexpectedCalls.push("resolveDefaultModelForAgent");
      }
      return { provider: "anthropic", model: "claude-opus-4-1" };
    },
  ),
  resolveModelRefFromString: vi.fn(() => {
    preparedReplyMockState.unexpectedCalls.push("resolveModelRefFromString");
    return undefined;
  }),
}));

const resolveSessionRuntimeOverrideForProviderMock = vi.hoisted(() =>
  vi.fn(
    (params: {
      entry?: {
        agentHarnessId?: string;
        agentRuntimeOverride?: string;
        modelSelectionLocked?: boolean;
      };
    }) => {
      if (
        params.entry?.agentHarnessId ||
        params.entry?.agentRuntimeOverride ||
        params.entry?.modelSelectionLocked
      ) {
        preparedReplyMockState.unexpectedCalls.push("resolveSessionRuntimeOverrideForProvider");
      }
      return undefined;
    },
  ),
);
vi.mock("../../src/agents/session-runtime-compat.js", () => ({
  resolveSessionRuntimeOverrideForProvider: resolveSessionRuntimeOverrideForProviderMock,
}));

// Provider policy projection belongs to its adapter and provider-local suites. These tests
// exercise prepared reply orchestration and supply their own model/thinking facts.
vi.mock("../../src/plugins/provider-policy-surface.js", () => ({
  resolveDirectBundledProviderPolicySurface: () => null,
  resolveTrustedExternalProviderPolicySurface: () => null,
}));

vi.mock("../../src/config/sessions/group.js", () => ({
  resolveGroupSessionKey: vi.fn().mockReturnValue(undefined),
}));

vi.mock(import("../../src/config/sessions/paths.js"), async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/config/sessions/paths.js")>();
  return {
    ...actual,
    resolveSessionFilePathCore: vi.fn().mockReturnValue("/tmp/session.jsonl"),
    resolveSessionFilePathOptions: vi.fn().mockReturnValue({}),
  };
});

const loadSessionEntryMock = vi.hoisted(() => vi.fn());
const updateAmbientTranscriptWatermarkMock = vi.hoisted(() => vi.fn().mockResolvedValue(null));

vi.mock("../../src/config/sessions/session-accessor.js", () => ({
  listSessionEntriesCore: vi.fn().mockReturnValue([]),
  loadSessionEntry: loadSessionEntryMock,
  patchSessionEntryCore: vi.fn(),
  persistSessionTranscriptTurn: vi.fn(),
}));

vi.mock("../../src/config/sessions/ambient-transcript-watermark.js", () => ({
  updateAmbientTranscriptWatermark: updateAmbientTranscriptWatermarkMock,
}));

vi.mock("../../src/globals.js", () => ({
  logVerbose: vi.fn(),
}));

vi.mock("../../src/process/command-queue.js", () => ({
  clearCommandLane: vi.fn().mockReturnValue(0),
  getQueueSize: vi.fn().mockReturnValue(0),
}));

vi.mock(import("../../src/routing/session-key.js"), async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/routing/session-key.js")>();
  return {
    ...actual,
    normalizeMainKey: () => "main",
    normalizeAgentId: vi.fn((id: string | undefined | null) => id ?? "default"),
  };
});

vi.mock("../../src/utils/provider-utils.js", () => ({
  isReasoningTagProvider: vi.fn().mockReturnValue(false),
}));

vi.mock("../../src/auto-reply/command-detection.js", () => ({
  hasControlCommand: vi.fn().mockReturnValue(false),
}));

vi.mock("../../src/auto-reply/reply/agent-runner.runtime.js", () => ({
  runReplyAgent: vi.fn().mockResolvedValue({ text: "ok" }),
}));

vi.mock("../../src/auto-reply/reply/body.js", () => ({
  applySessionHints: vi.fn().mockImplementation(async ({ baseBody }) => baseBody),
}));

const resolveCurrentTurnImagesMock = vi.hoisted(() => vi.fn().mockResolvedValue({}));
vi.mock("../../src/auto-reply/reply/current-turn-images.js", () => ({
  resolveCurrentTurnImages: resolveCurrentTurnImagesMock,
}));

vi.mock("../../src/auto-reply/reply/get-reply-fast-path.js", () => ({
  shouldUseReplyFastTestRuntime: vi.fn().mockReturnValue(false),
}));

vi.mock("../../src/auto-reply/reply/groups.js", () => ({
  buildDirectChatContext: vi.fn().mockReturnValue(""),
  buildGroupIntro: vi.fn().mockReturnValue(""),
  buildGroupChatContext: vi.fn().mockReturnValue(""),
}));

vi.mock("../../src/auto-reply/reply/inbound-meta.js", () => ({
  buildInboundMetaSystemPrompt: vi.fn().mockReturnValue(""),
  buildInboundUserContextPrefix: vi.fn().mockReturnValue(""),
  formatActiveGoalContext: vi.fn().mockReturnValue(undefined),
  refreshActiveGoalContext: (context: unknown) => context,
  resolveInboundUserContextPromptJoiner: vi.fn().mockReturnValue(undefined),
}));

vi.mock("../../src/auto-reply/reply/queue/settings-runtime.js", () => ({
  resolveQueueSettings: vi.fn().mockReturnValue({ mode: "steer" }),
}));

vi.mock("../../src/auto-reply/reply/route-reply.runtime.js", () => ({
  routeReply: vi.fn(),
}));

vi.mock("../../src/auto-reply/reply/session-updates.runtime.js", () => ({
  ensureSkillSnapshot: vi.fn().mockImplementation(async ({ sessionEntry, systemSent }) => ({
    sessionEntry,
    systemSent,
    skillsSnapshot: undefined,
  })),
}));

vi.mock("../../src/auto-reply/reply/session-system-events.js", () => ({
  drainFormattedSystemEvents: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../../src/sessions/stored-model-overrides.js", () => ({
  resolveStoredModelOverride: vi.fn(
    (params: {
      sessionEntry?: { providerOverride?: string; modelOverride?: string };
      sessionStore?: Record<string, { providerOverride?: string; modelOverride?: string }>;
    }) => {
      const entries = [params.sessionEntry, ...Object.values(params.sessionStore ?? {})];
      if (entries.some((entry) => entry?.providerOverride || entry?.modelOverride)) {
        preparedReplyMockState.unexpectedCalls.push("resolveStoredModelOverride");
      }
      return null;
    },
  ),
}));

vi.mock("../../src/auto-reply/reply/session-reset-prompt.js", () => ({
  resolveBareResetBootstrapFileAccess: vi.fn().mockReturnValue(false),
  resolveBareSessionResetPromptState: vi.fn().mockResolvedValue({
    bootstrapMode: "none",
    prompt: "A new session was started via /new or /reset.",
    shouldPrependStartupContext: true,
  }),
}));

vi.mock("../../src/auto-reply/reply/typing-mode.js", async (importOriginal) => ({
  ...(await importOriginal()),
  resolveTypingMode: vi.fn().mockReturnValue("off"),
}));

function createGatewayDrainingError(): Error {
  const error = new Error("Gateway is draining for restart; new tasks are not accepted");
  error.name = "GatewayDrainingError";
  return error;
}

const ROOM_EVENT_MESSAGE_TOOL_DIRECTIVE =
  "Treat this message as observed room activity, not a request. You were not explicitly tagged or mentioned in this room event. Default: stay silent. Only respond if you have something useful, substantial, or important to add. A previous mention or reply is not an invitation to keep talking. To respond visibly, use message(action=send); your final text here stays private either way.";

function createInboundBody<T extends string>(body: T) {
  return { Body: body, RawBody: body, CommandBody: body };
}

function createSessionBody<T extends string>(body: T) {
  return { Body: body, BodyStripped: body };
}

function createProviderSurface<T extends string>(provider: T) {
  return { Provider: provider, Surface: provider };
}

function createInboundTurn<
  TBody extends string,
  TProvider extends string,
  TChatType extends string,
>(body: TBody, provider: TProvider, chatType: TChatType) {
  return { ...createInboundBody(body), ...createProviderSurface(provider), ChatType: chatType };
}

function createSessionTurn<
  TBody extends string,
  TProvider extends string,
  TChatType extends string,
>(body: TBody, provider: TProvider, chatType: TChatType) {
  return { ...createSessionBody(body), ...createProviderSurface(provider), ChatType: chatType };
}

function baseParams(
  overrides: Partial<Parameters<typeof runPreparedReply>[0]> = {},
): Parameters<typeof runPreparedReply>[0] {
  const defaults = {
    ctx: {
      ...createInboundBody(""),
      ThreadHistoryBody: "Earlier message in this thread",
      OriginatingChannel: "slack",
      OriginatingTo: "C123",
      ChatType: "group",
    },
    sessionCtx: {
      ...createSessionBody(""),
      ThreadHistoryBody: "Earlier message in this thread",
      media: [{ path: "/tmp/input.png" }],
      Provider: "slack",
      ChatType: "group",
      OriginatingChannel: "slack",
      OriginatingTo: "C123",
    },
    cfg: { session: {}, channels: {}, agents: { defaults: {} } },
    agentId: "default",
    agentDir: "/tmp/agent",
    agentCfg: {},
    sessionCfg: {},
    commandAuthorized: true,
    command: {
      surface: "slack",
      channel: "slack",
      isAuthorizedSender: true,
      abortKey: "session-key",
      ownerList: [],
      senderIsOwner: false,
      rawBodyNormalized: "",
      commandBodyNormalized: "",
    } as never,
    commandSource: "",
    allowTextCommands: true,
    directives: {
      hasThinkDirective: false,
      thinkLevel: undefined,
    } as never,
    defaultActivation: "always",
    resolvedThinkLevel: "high",
    resolvedVerboseLevel: "off",
    resolvedReasoningLevel: "off",
    resolvedElevatedLevel: "off",
    elevatedEnabled: false,
    elevatedAllowed: false,
    blockStreamingEnabled: false,
    resolvedBlockStreamingBreak: "message_end",
    modelState: {
      resolveDefaultThinkingLevel: async () => "medium",
      resolveThinkingCatalog: async () => [],
    } as never,
    provider: "anthropic",
    model: "claude-opus-4-1",
    typing: {
      onReplyStart: vi.fn().mockResolvedValue(undefined),
      cleanup: vi.fn(),
    } as never,
    defaultModel: "claude-opus-4-1",
    timeoutMs: 30_000,
    isNewSession: true,
    resetTriggered: false,
    systemSent: true,
    sessionKey: "session-key",
    workspaceDir: "/tmp/workspace",
    abortedLastRun: false,
  };
  const ctx = overrides.ctx ?? defaults.ctx;
  const sessionCtx = overrides.sessionCtx ?? defaults.sessionCtx;
  const resolveTestCanonicalText = (value: Record<string, unknown>) => {
    const { commandText, agentText, rawText } = finalizeInboundContextForSdk({ ...value });
    return { commandText, agentText, rawText };
  };
  const sessionText = resolveTestCanonicalText(sessionCtx);
  return {
    ...defaults,
    ...overrides,
    conversation:
      overrides.conversation ??
      prepareReplyConversation({
        ctx: sessionCtx,
        sessionEntry:
          overrides.sessionStore?.[overrides.sessionKey ?? defaults.sessionKey] ??
          overrides.sessionEntry,
        isHeartbeat: overrides.opts?.isHeartbeat,
      }),
    ctx: { ...ctx, ...resolveTestCanonicalText(ctx) },
    sessionCtx: {
      ...sessionCtx,
      ...sessionText,
      agentText:
        typeof sessionCtx.BodyStripped === "string"
          ? sessionCtx.BodyStripped
          : sessionText.agentText,
    },
  } as Parameters<typeof runPreparedReply>[0];
}

function runPrepared(overrides: Partial<Parameters<typeof runPreparedReply>[0]> = {}) {
  return runPreparedReply(baseParams(overrides));
}

function ownerParams(): Parameters<typeof runPreparedReply>[0] {
  const params = baseParams();
  params.command = {
    ...(params.command as Record<string, unknown>),
    senderIsOwner: true,
  } as never;
  return params;
}

type MockCallSource = {
  mock: {
    calls: ReadonlyArray<ReadonlyArray<unknown>>;
  };
};

function requireMockCallArg(mock: MockCallSource, label: string, index = 0): unknown {
  const call = mock.mock.calls[index];
  if (!call) {
    throw new Error(`${label} call ${index} missing`);
  }
  return call[0];
}

function requireRunReplyAgentCall(index = 0) {
  const call = vi.mocked(runReplyAgent).mock.calls[index]?.[0];
  if (!call) {
    throw new Error(`runReplyAgent call ${index} missing`);
  }
  return call;
}

function requireLastRunReplyAgentCall() {
  const calls = vi.mocked(runReplyAgent).mock.calls;
  const call = calls[calls.length - 1]?.[0];
  if (!call) {
    throw new Error("last runReplyAgent call missing");
  }
  return call;
}


vi.mock("../../src/auto-reply/reply/agent-runner-memory.js", () => ({
  runSessionCompactionIfNeeded: async ({ sessionEntry }: { sessionEntry: unknown }) => sessionEntry,
  runMemoryFlushIfNeeded: async () => { throw new Error("unexpected model-execution path"); },
}));
vi.mock("../../src/auto-reply/reply/agent-runner-utils.js", async (importOriginal) => ({
  ...(await importOriginal()),
  resolveQueuedReplyExecutionConfig: async (config: unknown) => config,
  resolveQueuedReplyRuntimeConfig: (config: unknown) => config,
}));
vi.mock("../../src/auto-reply/reply/agent-runner-auto-fallback.js", async (importOriginal) => ({
  ...(await importOriginal()),
  resolveRunAfterAutoFallbackPrimaryProbeRecheck: ({ run }: { run: unknown }) => run,
}));

// These cases exercise admission/steering only. An unexpected model execution
// must fail the test, including when native callers catch the thrown error.
vi.mock("../../src/auto-reply/reply/agent-runner-execution.js", () => ({
  executeAgentTurn: async () => {
    preparedReplyMockState.unexpectedCalls.push("executeAgentTurn");
    throw new Error("unexpected model-execution boundary in queue/admission unit fixture");
  },
}));

// These four cases inspect active steering or enqueueing. They never execute
// a future queued model run, start a fresh model run, or reset the session.
// Keep those dormant boundaries explicit and fail even if a native caller catches.
vi.mock("../../src/auto-reply/reply/followup-runner.js", () => ({
  createFollowupRunner: () => async () => {
    preparedReplyMockState.unexpectedCalls.push("queued-followup-execution");
    throw new Error("unexpected queued followup execution in queue/admission fixture");
  },
}));
vi.mock("../../src/auto-reply/reply/agent-runner-execute.js", () => ({
  executePreparedReplyAgentRun: async () => {
    preparedReplyMockState.unexpectedCalls.push("executePreparedReplyAgentRun");
    throw new Error("unexpected prepared model execution in queue/admission fixture");
  },
  createReplyAgentRestartRecoveryController: () => {
    preparedReplyMockState.unexpectedCalls.push("createReplyAgentRestartRecoveryController");
    throw new Error("unexpected model execution preparation in queue/admission fixture");
  },
}));
vi.mock("../../src/auto-reply/reply/agent-runner-session-reset.js", () => ({
  resetReplyRunSession: async () => {
    preparedReplyMockState.unexpectedCalls.push("resetReplyRunSession");
    throw new Error("unexpected session reset in queue/admission fixture");
  },
}));

process.stderr.write("[steering:boundary] import:queue:enter\n");
const queueNative = await import("../../src/auto-reply/reply/queue.js");
process.stderr.write("[steering:boundary] import:queue:exit\n");
process.stderr.write("[steering:boundary] import:queue-state:enter\n");
const queueStateNative = await import("../../src/auto-reply/reply/queue/state.js");
process.stderr.write("[steering:boundary] import:queue-state:exit\n");
process.stderr.write("[steering:boundary] import:followup-turn-admission:enter\n");
const { admitFollowupTurn } = await import("../../src/auto-reply/reply/followup-turn-admission.js");
process.stderr.write("[steering:boundary] import:followup-turn-admission:exit\n");
process.stderr.write("[steering:boundary] import:agent-runner-run:enter\n");
const { runReplyAgent: runRealReplyAgent } = await import("../../src/auto-reply/reply/agent-runner-run.js");
process.stderr.write("[steering:boundary] import:agent-runner-run:exit\n");
process.stderr.write("[steering:boundary] import:reply-tool-authority:enter\n");
const { prepareReplyToolAuthority, resolveFollowupRunToolAuthorityFingerprint } = await import("../../src/auto-reply/reply/reply-tool-authority.js");
process.stderr.write("[steering:boundary] import:reply-tool-authority:exit\n");
process.stderr.write("[steering:boundary] import:test-helpers:enter\n");
const { createMockTypingController } = await import("../../src/auto-reply/reply/test-helpers.js");
process.stderr.write("[steering:boundary] import:test-helpers:exit\n");
process.stderr.write("[steering:boundary] import:reply-operation-run-state:enter\n");
const { REPLY_OPERATION_RUN_STATE } = await import("../../src/auto-reply/reply/reply-operation-run-state.js");
process.stderr.write("[steering:boundary] import:reply-operation-run-state:exit\n");
process.stderr.write("[steering:boundary] import:promise-helper:enter\n");
const { createDeferred } = await import("../../test/helpers/promise.js");
process.stderr.write("[steering:boundary] import:promise-helper:exit\n");

process.stderr.write("[steering:boundary] preparation-probe-imports:enter\n");
const preparationContextNative = await import("../../src/auto-reply/reply/get-reply-run-context.js");
const preparationAdmissionNative = await import("../../src/auto-reply/reply/get-reply-run-admission.js");
const preparationExecutionNative = await import("../../src/auto-reply/reply/get-reply-run-execute.js");
const inboundContextNative = await import("../../src/auto-reply/reply/inbound-context.js");
const conversationNative = await import("../../src/auto-reply/reply/prompt-session-context.js");
const runtimePolicyNative = await import("../../src/auto-reply/reply/runtime-policy-session-key.js");
const thinkingRuntimeNative = await import("../../src/agents/thinking-runtime.js");
const currentImagesNative = await import("../../src/auto-reply/reply/current-turn-images.js");
const transcriptRecorderNative = await import("../../src/sessions/user-turn-transcript.js");
const replyThreadingNative = await import("../../src/auto-reply/reply/reply-threading.js");
const bundledChannelNative = await import("../../src/channels/plugins/bundled.js");
process.stderr.write("[steering:boundary] preparation-probe-imports:exit\n");

const preparationProbeRestorers: Array<() => void> = [];
function emitPreparationBoundary(label: string): void {
  try {
    process.stderr.write(`[steering:preparation] ${label}\n`);
  } catch {
    // A diagnostic write must not alter the observed call or reject a side observer.
  }
}

function observePreparationCall(
  namespace: object,
  method: string,
  label: string,
  observeSettlement = false,
): void {
  const target = namespace as Record<string, (...args: unknown[]) => unknown>;
  const originalExport = target[method];
  const existingMock = vi.isMockFunction(originalExport) ? originalExport : undefined;
  // An existing vi.fn is already a spy: invoking it after replacing its implementation
  // would recurse. Capture its implementation and restore that same implementation.
  const actual = existingMock ? existingMock.getMockImplementation() : originalExport;
  if (typeof actual !== "function") {
    throw new TypeError("Expected a preparation function implementation for the diagnostic probe");
  }
  const spy = vi.spyOn(target, method);
  spy.mockImplementation(function (this: unknown, ...args: unknown[]) {
    emitPreparationBoundary(`${label}:enter`);
    try {
      const result = Reflect.apply(actual, this, args);
      emitPreparationBoundary(`${label}:return`);
      if (observeSettlement) {
        // Native async preparation exports and the existing async image mock use this observer.
        // Both branches return void; the original promise and rejection remain unchanged.
        void (result as Promise<unknown>).then(
          () => emitPreparationBoundary(`${label}:fulfilled`),
          () => emitPreparationBoundary(`${label}:rejected`),
        );
      }
      return result;
    } catch (error) {
      emitPreparationBoundary(`${label}:throw`);
      throw error;
    }
  });
  preparationProbeRestorers.push(() => {
    if (existingMock) existingMock.mockImplementation(actual);
    else spy.mockRestore();
  });
}

process.stderr.write("[steering:boundary] describe:enter\n");
describe("isolated adopted-drain steering", () => {
  beforeEach(async () => {
    process.stderr.write("[steering:boundary] beforeEach:enter\n");
    preparedReplyMockState.unexpectedCalls.length = 0;
    loadSessionEntryMock.mockReset();
    updateAmbientTranscriptWatermarkMock.mockClear();
    vi.clearAllMocks();
    vi.mocked(buildDirectChatContext).mockReturnValue("");
    vi.mocked(buildGroupIntro).mockReturnValue("");
    vi.mocked(buildGroupChatContext).mockReturnValue("");
    vi.mocked(buildInboundUserContextPrefix).mockReset().mockReturnValue("");
    vi.mocked(resolveInboundUserContextPromptJoiner).mockReturnValue(undefined);
    vi.mocked(hasControlCommand).mockReturnValue(false);
    resolveCurrentTurnImagesMock.mockReset().mockResolvedValue({});
    replyRunTesting.resetReplyRunRegistry();
    observePreparationCall(preparationContextNative, "prepareReplyRunContext", "context", true);
    observePreparationCall(preparationAdmissionNative, "prepareReplyRunAdmission", "admission", true);
    observePreparationCall(preparationExecutionNative, "executePreparedReplyRun", "execute", true);
    observePreparationCall(inboundContextNative, "finalizeInboundContextForSdk", "normalize-input");
    observePreparationCall(conversationNative, "prepareReplyConversation", "conversation");
    observePreparationCall(runtimePolicyNative, "resolveRuntimePolicySessionKey", "runtime-policy");
    observePreparationCall(thinkingRuntimeNative, "resolveEffectiveAgentRuntime", "thinking-runtime");
    observePreparationCall(currentImagesNative, "resolveCurrentTurnImages", "current-images", true);
    observePreparationCall(transcriptRecorderNative, "createUserTurnTranscriptRecorder", "transcript-recorder");
    observePreparationCall(replyThreadingNative, "resolveReplyToMode", "reply-to-mode");
    observePreparationCall(bundledChannelNative, "getBundledChannelPlugin", "bundled-channel");
    process.stderr.write("[steering:boundary] beforeEach:exit\n");
  });

  afterEach(async () => {
    process.stderr.write("[steering:boundary] afterEach:enter\n");
    for (const restore of preparationProbeRestorers.splice(0).reverse()) restore();
    vi.useRealTimers();
    resetSystemEventsForTest();
    expect(preparedReplyMockState.unexpectedCalls).toEqual([]);
    process.stderr.write("[steering:boundary] afterEach:exit\n");
  });


  // Positive behavior, two negative controls, and an empty-queue steering control.
  // Only the adopted-current scenario should fail on the unmodified 81a source.
  it.each(["empty-control", "adopted-current", "older-ready", "different-authority"] as const)(
    "preserves source ownership and steering for %s",
    async (scenario) => {
      process.stderr.write(`[steering:boundary] case:${scenario}:enter\n`);
      const key = `agent:main:discord:channel:fixture-${scenario}`;
      const settings = { mode: "steer" as const, debounceMs: 0, cap: 10 };
      process.stderr.write(`[steering:boundary] case:${scenario}:queue-settings-import:enter\n`);
      const { resolveQueueSettings } = await import("../../src/auto-reply/reply/queue/settings-runtime.js");
      process.stderr.write(`[steering:boundary] case:${scenario}:queue-settings-import:exit\n`);
      vi.mocked(resolveQueueSettings).mockReturnValue(settings);
      const sourceAdopted = vi.fn(async () => {});
      const sourceSettled = vi.fn();
      const correctionAdopted = vi.fn(async () => {});
      const correctionSettled = vi.fn();
      const entered = createDeferred();
      const release = createDeferred();
      const drained = createDeferred();
      const injected = vi.fn();
      const runState: Record<string, unknown> = {};
      const typing = createMockTypingController();
      let operation: ReturnType<typeof createReplyOperation> | undefined;
      const makeParams = (body: string) => baseParams({
        ...ownerParams(),
        conversation: undefined,
        cfg: {},
        agentId: "main",
        sessionKey: key,
        sessionId: "fixture-session",
        isNewSession: false,
        typing,
        ctx: { Body: body, Provider: "discord", Surface: "discord", AccountId: "default", ReplyToMode: "off", From: "channel:fixture", To: "channel:fixture", SenderId: "fixture-owner" },
        sessionCtx: { Body: body, Provider: "discord", Surface: "discord", AccountId: "default", ReplyToMode: "off", From: "channel:fixture", To: "channel:fixture", SenderId: "fixture-owner" },
      });
      try {
        process.stderr.write(`[steering:boundary] case:${scenario}:prepare-source:enter\n`);
        const firstParams = makeParams("prepare the checklist");
        process.stderr.write(`[steering:boundary] case:${scenario}:source-params:exit\n`);
        await runPreparedReply(firstParams);
        process.stderr.write(`[steering:boundary] case:${scenario}:prepare-source:exit\n`);
        const source = requireLastRunReplyAgentCall().followupRun;
        expect(source.prompt).toContain("prepare the checklist");
        source.messageId = "fixture-first";
        source.turnAdoptionLifecycle = { admission: "exclusive", onAdopted: sourceAdopted, onSettled: sourceSettled };
        const activate = (active: ReturnType<typeof createReplyOperation>) => {
          operation = active;
          active.bindToolAuthoritySnapshot(prepareReplyToolAuthority(source));
          const fingerprint = active.bindToolAuthorityRoute({ provider: source.run.provider, model: source.run.model });
          active.attachBackend({
            kind: "embedded",
            runId: "fixture-active-run",
            toolAuthorityFingerprint: fingerprint,
            sourceReplyDeliveryMode: source.run.sourceReplyDeliveryMode,
            taskSuggestionDeliveryMode: source.run.taskSuggestionDeliveryMode,
            cancel: vi.fn(),
            messageInjectionV2: {
              version: 2,
              isAvailable: () => true,
              queueMessage: async (message, options, assertCurrent) => {
                assertCurrent();
                options?.onQueueAccepted?.(true);
                injected(message);
                // This fixture attests the mocked provider boundary, not a real transcript write.
                return;
              },
            },
          });
          active.setPhase("running");
        };
        if (scenario === "empty-control") {
          activate(createReplyOperation({ sessionKey: key, sessionId: source.run.sessionId, resetTriggered: false }));
          process.stderr.write(`[steering:boundary] case:${scenario}:source-lifecycle:enter\n`);
          await queueNative.admitFollowupRunLifecycle(source);
          process.stderr.write(`[steering:boundary] case:${scenario}:source-lifecycle:exit\n`);
        } else {
          expect(queueNative.enqueueFollowupRun(key, source, settings, "message-id", async (queued) => {
            try {
              process.stderr.write(`[steering:boundary] case:${scenario}:drain-admission:enter\n`);
              const admitted = await admitFollowupTurn({ queued, defaults: { typing, typingMode: "never", defaultModel: source.run.model, sessionKey: key } });
              process.stderr.write(`[steering:boundary] case:${scenario}:drain-admission:exit\n`);
              expect(admitted.kind).toBe("admitted");
              if (admitted.kind !== "admitted") throw new Error("source did not acquire its queued operation");
              activate(admitted.turn.operation);
              entered.resolve();
              process.stderr.write(`[steering:boundary] case:${scenario}:source-release:enter\n`);
              await release.promise;
              process.stderr.write(`[steering:boundary] case:${scenario}:source-release:exit\n`);
              queueNative.completeFollowupRunLifecycle(queued);
            } catch (error) {
              entered.reject(error);
              throw error;
            } finally {
              drained.resolve();
            }
          })).toBe(true);
          process.stderr.write(`[steering:boundary] case:${scenario}:drain-entered:enter\n`);
          await entered.promise;
          process.stderr.write(`[steering:boundary] case:${scenario}:drain-entered:exit\n`);
          expect(sourceAdopted).toHaveBeenCalledOnce();
          expect(queueNative.getFollowupQueueDepth(key)).toBe(0);
          expect(queueStateNative.getExistingFollowupQueue(key)?.inFlight.has(source)).toBe(true);
        }
        if (!operation) throw new Error("active operation missing");
        if (scenario === "older-ready") {
          expect(queueNative.enqueueFollowupRun(key, { ...source, messageId: "fixture-older-waiter", prompt: "older pending request", turnAdoptionLifecycle: { onAdopted: vi.fn() } }, settings, "message-id", undefined, false)).toBe(true);
        }
        process.stderr.write(`[steering:boundary] case:${scenario}:correction-params:enter\n`);
        const correctionParams = makeParams("it's in Drive, use that one");
        process.stderr.write(`[steering:boundary] case:${scenario}:correction-params:exit\n`);
        correctionParams.opts = { [REPLY_OPERATION_RUN_STATE]: runState };
        process.stderr.write(`[steering:boundary] case:${scenario}:prepare-correction:enter\n`);
        await runPreparedReply(correctionParams);
        process.stderr.write(`[steering:boundary] case:${scenario}:prepare-correction:exit\n`);
        const prepared = requireLastRunReplyAgentCall();
        expect(prepared.followupRun.prompt).toContain("it's in Drive, use that one");
        prepared.followupRun.messageId = "fixture-correction";
        prepared.followupRun.turnAdoptionLifecycle = { admission: "exclusive", onAdopted: correctionAdopted, onSettled: correctionSettled };
        prepared.typingMode = "never";
        prepared.isRunActive = () => operation?.result === null;
        if (scenario === "different-authority") prepared.followupRun.toolsAllow = [];
        const fingerprint = resolveFollowupRunToolAuthorityFingerprint(prepared.followupRun);
        expect(fingerprint === operation.toolAuthorityFingerprint).toBe(scenario !== "different-authority");
        process.stderr.write(`[steering:boundary] case:${scenario}:run-reply-agent:enter\n`);
        await runRealReplyAgent(prepared);
        process.stderr.write(`[steering:boundary] case:${scenario}:run-reply-agent:exit\n`);
        const expectSteer = scenario === "empty-control" || scenario === "adopted-current";
        expect(injected).toHaveBeenCalledTimes(expectSteer ? 1 : 0);
        expect(correctionAdopted).toHaveBeenCalledTimes(expectSteer ? 1 : 0);
        expect(correctionSettled).toHaveBeenCalledTimes(expectSteer ? 1 : 0);
        expect(runState.admission).toEqual({ status: "accepted", mode: expectSteer ? "steer" : "followup" });
        expect(sourceAdopted).toHaveBeenCalledOnce();
        expect(sourceSettled).not.toHaveBeenCalled();
        const waiting = queueStateNative.getExistingFollowupQueue(key)?.items ?? [];
        expect(waiting.some((item) => item.messageId === "fixture-correction")).toBe(!expectSteer);
        if (scenario === "older-ready") expect(waiting.map((item) => item.messageId)).toEqual(["fixture-first", "fixture-older-waiter", "fixture-correction"]);
      } finally {
        process.stderr.write(`[steering:boundary] case:${scenario}:finally:enter\n`);
        // Invalidate queued continuations before releasing the mocked tool boundary.
        queueNative.clearSessionQueues([key]);
        release.resolve();
        process.stderr.write(`[steering:boundary] case:${scenario}:finally-drained:enter\n`);
        if (scenario !== "empty-control" && operation) await drained.promise;
        process.stderr.write(`[steering:boundary] case:${scenario}:finally-drained:exit\n`);
        operation?.complete();
        process.stderr.write(`[steering:boundary] case:${scenario}:finally:exit\n`);
      }
      process.stderr.write(`[steering:boundary] case:${scenario}:exit\n`);
    },
  );
});
process.stderr.write("[steering:boundary] describe:exit\n");
