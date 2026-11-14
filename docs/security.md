# Local platform security

- Never commit credentials, private keys, credential-bearing DSNs, secret JSON,
  environment files, Terraform state/plans or provider response dumps.
- Preserve the existing `.env`; agents never edit it. Required configuration
  changes must be supplied in chat for the operator to apply. Do not put secrets
  in shell arguments, URLs, logs, metrics, image layers or browser storage.
- Compose injects only the database fields infrastructure needs. The API loads
  runtime configuration through `env_file`, with known provider credential keys
  cleared. Execution exclusively owns Alpaca credentials and the read-only data
  relay. Research/strategy workers receive no provider credentials. Massive's
  worker receives only its separate provider key/S3 configuration. S3 downloading
  remains disabled pending entitlement and complete credentials.
- UI API keys remain in page memory. The server-side proxy uses a fixed internal
  API origin, validates paths, bounds request bodies, and requires same-origin
  mutations. Provider credentials are never shipped to the browser.
- API/UI publication is loopback-only. Versioned routes and metrics require
  `X-API-Key`; health probes are unauthenticated. Do not expose the development
  superuser/shared-key model externally. Production requires separate identities,
  authorization, restricted runtime roles, network policy, TLS and secret handling.
- Broker submission is not armed by credentials, runtime permission or startup.
  It also requires an explicitly armed, expiring run and fresh reconciled state.
  Ambiguous POSTs retain reservations and are never automatically resubmitted.
- No hosted databases, production brokers, deployments, merges to `main`, secret
  rotation or schedule changes are authorized by local-development work. A merge
  to `main` remains a potential Supabase migration release path.
- Verification uses isolated, uniquely owned local databases/streams where possible.
  Actual authorized Paper checks retain their audit and cancel/close only run-owned
  exposure. Never use cancel-all, blanket liquidation or volume reset as cleanup.
