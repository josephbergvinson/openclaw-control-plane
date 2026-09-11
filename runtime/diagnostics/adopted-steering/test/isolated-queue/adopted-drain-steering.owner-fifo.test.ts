// Portable owner and FIFO regression controls; source imports resolve within the checkout.
// The queue, adoption, admission policy, operation registry and tool-authority comparison remain real.
// Native harness setup is shared with the four-case routing reproduction.
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

vi.mock(import("../../src/agents/agent-tools.policy.js"), async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/agents/agent-tools.policy.js")>();
  return {
    ...actual,
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
  };
});

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

const queueNative = await import("../../src/auto-reply/reply/queue.js");
const queueStateNative = await import("../../src/auto-reply/reply/queue/state.js");
const { admitFollowupTurn } = await import("../../src/auto-reply/reply/followup-turn-admission.js");
const { runReplyAgent: runRealReplyAgent } = await import("../../src/auto-reply/reply/agent-runner-run.js");
const { prepareReplyToolAuthority, resolveFollowupRunToolAuthorityFingerprint } = await import("../../src/auto-reply/reply/reply-tool-authority.js");
const { createMockTypingController } = await import("../../src/auto-reply/reply/test-helpers.js");
const { REPLY_OPERATION_RUN_STATE } = await import("../../src/auto-reply/reply/reply-operation-run-state.js");
const { createDeferred } = await import("../../test/helpers/promise.js");

describe("adopted-source owner and FIFO fences", () => {
  beforeEach(async () => {
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
  });

  afterEach(async () => {
    vi.useRealTimers();
    resetSystemEventsForTest();
    expect(preparedReplyMockState.unexpectedCalls).toEqual([]);
  });



type FenceRun = Parameters<typeof queueNative.enqueueFollowupRun>[1];
type FenceOperation = ReturnType<typeof createReplyOperation>;
type FencePrepared = ReturnType<typeof requireLastRunReplyAgentCall>;

async function withOwnerFence(
  name: string,
  options: { collect?: boolean; sourceAbort?: AbortController },
  body: (fixture: {
    key: string;
    sources: FenceRun[];
    admittedSource: () => FenceRun;
    operation: () => FenceOperation;
    injections: Array<{ owner: FenceOperation; prompt: string }>;
    backendCancels: Map<FenceOperation, ReturnType<typeof vi.fn>>;
    sourceAdopted: Array<ReturnType<typeof vi.fn>>;
    sourceSettled: Array<ReturnType<typeof vi.fn>>;
    correctionAdopted: ReturnType<typeof vi.fn>;
    correctionSettled: ReturnType<typeof vi.fn>;
    runState: Record<string, unknown>;
    prepareCorrection: () => Promise<FencePrepared>;
    execute: (prepared: FencePrepared) => Promise<unknown>;
    replaceOwner: () => FenceOperation;
    enqueueOrdinary: (id: string) => FenceRun;
    parkPredecessor: () => NonNullable<ReturnType<typeof queueNative.parkSteerCandidate>>;
  }) => Promise<void>,
) {
  const key = `agent:main:discord:channel:owner-fence-${name}`;
  const settings = { mode: "steer" as const, debounceMs: 0, cap: 10 };
  const collectSettings = { ...settings, mode: "collect" as const };
  const { resolveQueueSettings } = await import("../../src/auto-reply/reply/queue/settings-runtime.js");
  vi.mocked(resolveQueueSettings).mockReturnValue(settings);
  const typing = createMockTypingController();
  const entered = createDeferred();
  const release = createDeferred();
  const drained = createDeferred();
  const operations: FenceOperation[] = [];
  const executions: Promise<unknown>[] = [];
  const executionErrors: unknown[] = [];
  const injections: Array<{ owner: FenceOperation; prompt: string }> = [];
  const backendCancels = new Map<FenceOperation, ReturnType<typeof vi.fn>>();
  const sourceAdopted = [vi.fn(async () => {}), vi.fn(async () => {})];
  const sourceSettled = [vi.fn(), vi.fn()];
  const correctionAdopted = vi.fn(async () => {});
  const correctionSettled = vi.fn();
  const runState: Record<string, unknown> = {};
  let sources: FenceRun[] = [];
  let source: FenceRun | undefined;
  let admittedSource: FenceRun | undefined;
  let current: FenceOperation | undefined;
  let drainEntered = false;
  let sourceRunCount = 0;

  const makeParams = (text: string) => baseParams({
    ...ownerParams(), conversation: undefined, cfg: {}, agentId: "main",
    sessionKey: key, sessionId: "owner-fence-session", isNewSession: false, typing,
    ctx: { Body: text, Provider: "discord", Surface: "discord", AccountId: "default", ReplyToMode: "off", From: "channel:fixture", To: "channel:fixture", SenderId: "fixture-owner" },
    sessionCtx: { Body: text, Provider: "discord", Surface: "discord", AccountId: "default", ReplyToMode: "off", From: "channel:fixture", To: "channel:fixture", SenderId: "fixture-owner" },
  });
  const requireSource = () => {
    if (!source) throw new Error("fence source was not prepared");
    return source;
  };
  const requireOperation = () => {
    if (!current) throw new Error("fence operation was not admitted");
    return current;
  };
  const requireAdmittedSource = () => {
    if (!admittedSource) throw new Error("fence drain source is missing");
    return admittedSource;
  };
  const activate = (operation: FenceOperation, ownerSource: FenceRun) => {
    current = operation;
    operations.push(operation);
    operation.bindToolAuthoritySnapshot(prepareReplyToolAuthority(ownerSource));
    const fingerprint = operation.bindToolAuthorityRoute({ provider: ownerSource.run.provider, model: ownerSource.run.model });
    const cancel = vi.fn();
    backendCancels.set(operation, cancel);
    operation.attachBackend({
      kind: "embedded", runId: `owner-fence-backend-${operations.length}`,
      toolAuthorityFingerprint: fingerprint,
      sourceReplyDeliveryMode: ownerSource.run.sourceReplyDeliveryMode,
      taskSuggestionDeliveryMode: ownerSource.run.taskSuggestionDeliveryMode,
      cancel,
      messageInjectionV2: {
        version: 2, isAvailable: () => true,
        queueMessage: async (message, injectionOptions, assertCurrent) => {
          assertCurrent();
          injectionOptions?.onQueueAccepted?.(true);
          injections.push({ owner: operation, prompt: message });
        },
      },
    });
    operation.setPhase("running");
  };
  const makeWaiter = (id: string): FenceRun => ({
    ...requireSource(), run: { ...requireSource().run },
    prompt: `request ${id}`, transcriptPrompt: `request ${id}`, messageId: id,
    abortSignal: undefined, queueAbortSignal: undefined, steerPending: undefined,
    turnAdoptionLifecycle: { admission: "exclusive", onAdopted: vi.fn(async () => {}), onSettled: vi.fn() },
  });
  const execute = (prepared: FencePrepared) => {
    const task = runRealReplyAgent(prepared);
    executions.push(task);
    // Observe rejection while a test deliberately holds a native steering reservation.
    // The original promise is still awaited by the case and by cleanup.
    void task.catch((error) => executionErrors.push(error));
    return task;
  };
  try {
    await runPreparedReply(makeParams("prepare the checklist"));
    source = requireLastRunReplyAgentCall().followupRun;
    source.messageId = "source-first";
    source.abortSignal = options.sourceAbort?.signal;
    source.turnAdoptionLifecycle = { admission: "exclusive", onAdopted: sourceAdopted[0], onSettled: sourceSettled[0] };
    if (options.collect) {
      sources = [0, 1].map((index) => ({
        ...requireSource(), run: { ...requireSource().run },
        prompt: `collected source ${index + 1}`, transcriptPrompt: `collected source ${index + 1}`,
        messageId: `source-collected-${index + 1}`,
        originatingChannel: "discord" as const, originatingTo: "channel:fixture", originatingChatType: "channel",
        // Independent source lifecycles can be collected without claiming exclusive ingress.
        turnAdoptionLifecycle: { onAdopted: sourceAdopted[index], onSettled: sourceSettled[index] },
      }));
    } else {
      sources = [source];
    }
    const drainSource = async (queued: FenceRun) => {
      drainEntered = true;
      sourceRunCount += 1;
      try {
        if (sourceRunCount !== 1) throw new Error("unexpected followup execution after source drain");
        admittedSource = queued;
        const admitted = await admitFollowupTurn({ queued, defaults: { typing, typingMode: "never", defaultModel: queued.run.model, sessionKey: key } });
        expect(admitted.kind).toBe("admitted");
        if (admitted.kind !== "admitted") throw new Error("fence source was not admitted");
        activate(admitted.turn.operation, queued);
        entered.resolve();
        await release.promise;
        queueNative.completeFollowupRunLifecycle(queued);
      } catch (error) {
        entered.reject(error);
        throw error;
      } finally {
        drained.resolve();
      }
    };
    for (const item of sources) {
      expect(queueNative.enqueueFollowupRun(key, item, options.collect ? collectSettings : settings, "message-id", undefined, false)).toBe(true);
    }
    queueNative.scheduleFollowupDrain(key, drainSource);
    await entered.promise;
    expect(sourceRunCount).toBe(1);
    expect(queueNative.getFollowupQueueDepth(key)).toBe(0);
    for (const [index, item] of sources.entries()) {
      expect(sourceAdopted[index]).toHaveBeenCalledOnce();
      expect(sourceSettled[index]).not.toHaveBeenCalled();
      expect(queueStateNative.getExistingFollowupQueue(key)?.inFlight.has(item)).toBe(true);
    }
    await body({
      key, sources, admittedSource: requireAdmittedSource, operation: requireOperation,
      injections, backendCancels, sourceAdopted, sourceSettled, correctionAdopted, correctionSettled, runState,
      prepareCorrection: async () => {
        const params = makeParams("it's in Drive, use that one");
        params.opts = { [REPLY_OPERATION_RUN_STATE]: runState };
        await runPreparedReply(params);
        const prepared = requireLastRunReplyAgentCall();
        prepared.followupRun.messageId = "correction";
        prepared.followupRun.turnAdoptionLifecycle = { admission: "exclusive", onAdopted: correctionAdopted, onSettled: correctionSettled };
        prepared.typingMode = "never";
        prepared.isRunActive = () => current?.result === null;
        expect(prepared.followupRun.prompt).toContain("it's in Drive, use that one");
        expect(resolveFollowupRunToolAuthorityFingerprint(prepared.followupRun)).toBe(requireOperation().toolAuthorityFingerprint);
        return prepared;
      },
      execute,
      replaceOwner: () => {
        const previous = requireOperation();
        previous.complete();
        const successor = createReplyOperation({ sessionKey: key, sessionId: previous.sessionId, resetTriggered: false });
        activate(successor, requireAdmittedSource());
        expect(successor).not.toBe(previous);
        expect(successor.key).toBe(previous.key);
        expect(successor.sessionId).toBe(previous.sessionId);
        expect(successor.toolAuthorityFingerprint).toBe(previous.toolAuthorityFingerprint);
        return successor;
      },
      enqueueOrdinary: (id) => {
        const waiter = makeWaiter(id);
        expect(queueNative.enqueueFollowupRun(key, waiter, settings, "message-id", undefined, false)).toBe(true);
        return waiter;
      },
      parkPredecessor: () => {
        const predecessor = queueNative.parkSteerCandidate(key, makeWaiter("earlier-steer"), settings, async () => {
          preparedReplyMockState.unexpectedCalls.push("parked-predecessor-followup-execution");
          throw new Error("unexpected predecessor fallback execution");
        });
        expect(predecessor).toBeDefined();
        if (!predecessor) throw new Error("earlier steering reservation missing");
        return predecessor;
      },
    });
    for (const [index] of sources.entries()) expect(sourceSettled[index]).not.toHaveBeenCalled();
  } finally {
    // Invalidate all parked/fallback continuations before releasing any operation.
    queueNative.clearSessionQueues([key]);
    release.resolve();
    if (drainEntered) await drained.promise;
    await Promise.allSettled(executions);
    for (const operation of operations) operation.complete();
    expect(queueStateNative.getExistingFollowupQueue(key)).toBeUndefined();
    expect(executionErrors).toEqual([]);
  }
}

function expectRetainedCorrection(fixture: Parameters<Parameters<typeof withOwnerFence>[2]>[0]) {
  expect(fixture.injections).toEqual([]);
  expect(fixture.correctionAdopted).not.toHaveBeenCalled();
  expect(fixture.correctionSettled).not.toHaveBeenCalled();
  expect(fixture.runState.admission).toEqual({ status: "accepted", mode: "followup" });
  const items = queueStateNative.getExistingFollowupQueue(fixture.key)?.items ?? [];
  expect(items.filter((item) => item.messageId === "correction")).toHaveLength(1);
}

it("does not inject an adopted-source correction into a same-key successor created after preparation", async () => {
  await withOwnerFence("successor-after-prepare", {}, async (fixture) => {
    const original = fixture.operation();
    const prepared = await fixture.prepareCorrection();
    const successor = fixture.replaceOwner();
    await fixture.execute(prepared);
    expectRetainedCorrection(fixture);
    expect(fixture.backendCancels.get(successor)).not.toHaveBeenCalled();
    expect(successor.abortSignal.aborted).toBe(false);
    expect(fixture.sources[0].run.sessionId).toBe(original.sessionId);
  });
});

it("keeps an ordinary predecessor that arrives after preparation ahead of the correction", async () => {
  await withOwnerFence("predecessor-after-prepare", {}, async (fixture) => {
    const prepared = await fixture.prepareCorrection();
    fixture.enqueueOrdinary("earlier-ordinary");
    await fixture.execute(prepared);
    expectRetainedCorrection(fixture);
    expect(queueStateNative.getExistingFollowupQueue(fixture.key)?.items.map((item) => item.messageId)).toEqual(["source-first", "earlier-ordinary", "correction"]);
  });
});

it("does not reuse steering authority after the retained source is aborted", async () => {
  const sourceAbort = new AbortController();
  await withOwnerFence("source-aborted", { sourceAbort }, async (fixture) => {
    const prepared = await fixture.prepareCorrection();
    sourceAbort.abort();
    await fixture.execute(prepared);
    expectRetainedCorrection(fixture);
    expect(sourceAbort.signal.aborted).toBe(true);
    expect(fixture.sources[0].abortSignal).toBe(sourceAbort.signal);
  });
});

it("lets a parked correction steer before an ordinary waiter appended later", async () => {
  await withOwnerFence("later-waiter", {}, async (fixture) => {
    const predecessor = fixture.parkPredecessor();
    const prepared = await fixture.prepareCorrection();
    const pending = fixture.execute(prepared);
    await expect.poll(() => queueStateNative.getExistingFollowupQueue(fixture.key)?.items.some((item) => item.messageId === "correction" && Boolean(item.steerPending)), { timeout: 5000, interval: 5 }).toBe(true);
    fixture.enqueueOrdinary("later-ordinary");
    expect(queueStateNative.getExistingFollowupQueue(fixture.key)?.items.map((item) => item.messageId)).toEqual(["source-first", "earlier-steer", "correction", "later-ordinary"]);
    expect(fixture.injections).toEqual([]);
    predecessor.accepted(true);
    predecessor.consume();
    await pending;
    expect(fixture.injections).toHaveLength(1);
    expect(fixture.injections[0].owner).toBe(fixture.operation());
    expect(fixture.injections[0].prompt).toContain("it's in Drive, use that one");
    expect(fixture.correctionAdopted).toHaveBeenCalledOnce();
    expect(fixture.correctionSettled).toHaveBeenCalledOnce();
    expect(fixture.runState.admission).toEqual({ status: "accepted", mode: "steer" });
    expect(queueStateNative.getExistingFollowupQueue(fixture.key)?.items.map((item) => item.messageId)).toEqual(["source-first", "later-ordinary"]);
  });
});

it("falls back in place if the active owner changes while the correction is parked", async () => {
  await withOwnerFence("successor-while-parked", {}, async (fixture) => {
    const predecessor = fixture.parkPredecessor();
    const prepared = await fixture.prepareCorrection();
    const pending = fixture.execute(prepared);
    await expect.poll(() => queueStateNative.getExistingFollowupQueue(fixture.key)?.items.some((item) => item.messageId === "correction" && Boolean(item.steerPending)), { timeout: 5000, interval: 5 }).toBe(true);
    const successor = fixture.replaceOwner();
    fixture.enqueueOrdinary("later-ordinary");
    predecessor.accepted(true);
    predecessor.consume();
    await pending;
    expectRetainedCorrection(fixture);
    expect(fixture.backendCancels.get(successor)).not.toHaveBeenCalled();
    expect(successor.abortSignal.aborted).toBe(false);
    expect(queueStateNative.getExistingFollowupQueue(fixture.key)?.items.map((item) => item.messageId)).toEqual(["source-first", "correction", "later-ordinary"]);
  });
});

it("retains distinct in-flight collected sources as a blocker even after their queue items are consumed", async () => {
  await withOwnerFence("collected-sources", { collect: true }, async (fixture) => {
    const queue = queueStateNative.getExistingFollowupQueue(fixture.key);
    expect(queue?.items).toEqual([]);
    expect(queue?.inFlight.size).toBe(2);
    expect(fixture.admittedSource()).not.toBe(fixture.sources[0]);
    expect(fixture.admittedSource()).not.toBe(fixture.sources[1]);
    expect(fixture.admittedSource().prompt).toContain("collected source 1");
    expect(fixture.admittedSource().prompt).toContain("collected source 2");
    const prepared = await fixture.prepareCorrection();
    await fixture.execute(prepared);
    expectRetainedCorrection(fixture);
    expect(queueStateNative.getExistingFollowupQueue(fixture.key)?.inFlight.size).toBe(2);
    expect(queueStateNative.getExistingFollowupQueue(fixture.key)?.items.map((item) => item.messageId)).toEqual(["correction"]);
  });
});

});
