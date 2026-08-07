# Baton agent mailbox protocol — v6

An agent coordination channel running **Baton protocol 6** has one SQLite
transactional authority per instance, no filename-state, and is defined
entirely by an explicit config. Consult the Baton distribution's `README.md`
for the command and storage contract.

## Instance selection

    BATON_BIN=/absolute/path/to/baton
    BATON_CONFIG=/absolute/path/to/instance/baton.json
    "$BATON_BIN" --config "$BATON_CONFIG" <command> ...

The config and SQLite authority live outside every participating product
tree. Never copy them into a repository, infer a config from the current
working directory, or omit `--config`. The local deployment supplies the
executable and explicit absolute config path; participating project policy
binds local roles to participant identities without hard-coding host paths.

Participant addresses are `<domain>.<role>`. A domain is a coordination
namespace, not necessarily a Git repository, and roles are open-ended. Each
project binds role-only instructions to concrete addresses in its own policy.

Never consume or claim through another domain's participant, even if a
message looks relevant. Cross-domain work must name the intended scoped
address.

Every identity-bearing invocation passes `--participant <address> --actor
<name> --seed <32-hex>`; use one live seed per actor instance and one consumer
path per actor/seed (never two concurrent `wait`s for the same identity).

## Working the channel

- Consume with `wait` (blocking) or `claim`; both return the lossless
  delivery (claim + envelope + body/attachment). Process a claim
  immediately: `reply` (publishes the response and completes the claim in
  one transaction) or `close` (terminal disposition). Retries are
  effectively-once: an exact retry reports `already_committed`, any
  mismatch fails closed.
- Durable review/response documents: bodies live IN the store; use
  `materialize --dir <finding folder> --prefix review|implementation-response`
  to emit the byte-exact `.md` projection for humans. Projections are
  caches; the store is the authority.
- Evidence files already in the tree travel as attachments:
  `--attach ROOT:relative/path` (hash-pinned at publication; mutation fails
  the claim).
- Broadcasts: `send-notice` (finite TTL); consume with `see`; authors may
  `expire` early.
- Never mutate the database with raw SQL; every table is guarded and
  doctor treats bypasses as corruption. `doctor`/`scan`/`dump`/`inspect`
  are the read-only views.

## Retention

Transient messages lose their bytes when consumed (identity/hashes
remain); durable messages are permanent. `gc` (any participant) collects
aged transient metadata per `retention_days`; the transition ledger and
audit tables are permanent.

Config changes use Baton's audited `regen` ceremony and require a participant
with the `config` capability; direct config/database edits are forbidden.
Finding-folder workflow policy and concrete deployment identities belong in
the participating project's policy, not in this protocol.
