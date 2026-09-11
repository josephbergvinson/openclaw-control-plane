// Pure descriptor fixture. This does not attest installed packages or contact a relay.
import { capabilityDescriptor } from "../../scripts/company_alpha_walletconnect_agent.mjs";
import { adapterCapabilityDescriptor } from "../../scripts/lib/company_alpha_walletconnect_hedera_adapter.mjs";

process.stdout.write(JSON.stringify({
  schema: "openclaw.company_alpha_walletconnect_agent_probe.v1",
  ok: true,
  checked_at: new Date().toISOString(),
  capability: capabilityDescriptor(),
  production_adapter: adapterCapabilityDescriptor({
    hedera_wallet_connect: "2.0.4", hashgraph_sdk: "2.81.0",
    walletconnect_core: "2.23.0", jiti: "1.21.7",
  }),
  zero_effects: {
    private_key_loaded: false, relay_connected: false, session_paired: false,
    signature_created: false, transaction_submitted: false,
    settlement_queried: false, funds_moved: false, execution_attempts: 0,
  },
}));
