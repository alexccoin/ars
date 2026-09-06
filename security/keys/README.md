# `security/keys`

**This directory holds policy and references. It never holds key material.**

Nothing here is a key, a token, a certificate, or anything that could become one. If you
are about to add a file whose contents would be bad to read out loud, you are in the wrong
directory. `.gitignore` blocks everything here except `*.md` for exactly that reason.

## Where the real material lives

| Key | Location | Managed by |
|---|---|---|
| OAuth tokens (Gmail, GitHub) | macOS Keychain, service `ars.token-vault`, account `<provider>:<account>` | `ars_auth.vault.TokenVault` |
| Token vault master key (fallback only) | Keychain item `ars.token-vault.master-key`, or `{data_dir}/vault.keys/master.key` mode 0600 | `ars_auth.vault._FernetFileBackend` |

The fallback path exists so A.R.S boots on a machine with no Keychain, per the local-first
rule that it must work with no accounts and no network. When it is in use the vault reports
`Protection.FERNET_LOCAL_KEY` rather than claiming encryption at rest it does not have —
a key sitting next to its own ciphertext is obfuscation, and the UI should say so.

## Rules

1. No key material in the repo, in an environment variable committed anywhere, or in a
   fixture. `.env.example` carries names and empty values only.
2. Rotating the vault master key makes every existing ciphertext undecryptable. That is
   intentional: rotation means the user re-authorises their accounts.
3. Deleting a key is a real deletion. `TokenVault.revoke` unlinks the ciphertext and
   removes the Keychain item; there is no tombstone and no `.bak`.
4. Tokens never reach the reasoning layer. See
   [`../threat-models/guard.md`](../threat-models/guard.md), boundary B5.
