# Routing adapter dependencies

The Python adapters use the standard library except for PyYAML and the optional
PostgreSQL adapter's Psycopg driver. `requirements.txt` records the observed
versions. Install them in the Python environment used to execute the selected
adapter; installing them does not establish provider access.

```sh
python3 -m venv /absolute/path/to/adapter-python
/absolute/path/to/adapter-python/bin/python -m pip install -r requirements.txt
```

The WalletConnect adapter has a separate, private runtime directory under the
configured Company Alpha runtime data root. Copy `walletconnect/package.json`
and `walletconnect/package-lock.json` into a directory named
`openclaw-walletconnect-testnet-runtime`, make that directory owner-only
(`0700`), and run `npm ci` there with the configured Node executable on `PATH`.
The historical directory name covers both supported networks. No dependency
install, key import, relay connection or transaction is performed by copying
these manifests.

The lockfile pins the full npm dependency graph. Direct packages are
`@hashgraph/hedera-wallet-connect` 2.0.4, `@hashgraph/proto` 2.25.0,
`@hashgraph/sdk` 2.81.0, `@walletconnect/core` 2.23.0 and `jiti` 1.21.7.
The adapter checks package ownership, paths, versions and required APIs before
using this runtime. Standalone signer probes resolve the SDK through the
configured Company Alpha server repository, or an explicitly supplied
`HEDERA_SDK_ROOT`; install the same SDK version at that selected location.

Bind the WalletConnect project ID, Company Alpha domain and testnet hostname,
and the two Hedera account IDs in `operator.json`. Mainnet and testnet remain
separate selections. In the configured state directory, provision the private
signer files under `secrets/` with exactly the layouts checked by the adapters.
The package contains no keys. A capability probe reads dependency metadata and
reports zero wallet effects. It does not establish signer custody, connect a
session, sign, submit a transaction or prove settlement.

Provider access remains separate: install and authenticate the CLI selected by
each route, bind its exact account, and run that route's exact read probe. A
successful probe for one account or operation is not evidence for another.
