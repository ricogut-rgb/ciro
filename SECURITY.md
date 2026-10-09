# Security

## Reporting a vulnerability

Please do not open a public issue for security-sensitive reports. Use one of
the private channels below instead:

- **GitHub private vulnerability reporting.** Go to the Security tab of this
  repository → *Report a vulnerability*. GitHub routes the report privately to
  the maintainer. (If the Security tab is not visible to you, use the email
  path below.)
- **Email.** Send a short description to the address listed in the public
  GitHub profile of the repository owner. Subject line: `security: <one-line summary>`.

Please include enough detail to reproduce (what you did, what you expected,
what actually happened). Do not include full exploit code or working payloads
in the first message — a brief description is enough to open the conversation.

## What to expect

This project is maintained by a single person as time permits; there is no SLA.
A good-faith acknowledgement will come as soon as the report is read. Fixes
land on `main` and are called out in release notes or the commit log.

## Scope

- The HTTP service in `agnostic_runtime/trigger/server.py` (bearer-token auth,
  loopback bind, confirm-only-admit gate, append-only record).
- The admitted code in `agnostic_runtime/` more broadly.

Out of scope: your deployment choices (reverse proxy, TLS, systemd hardening,
network exposure). `DEPLOY.md` has a sketch; verifying your own deploy is
your responsibility.

## Public thanks

If you want public credit for a reported finding, say so in the report and
I'll add you to a short acknowledgements list once the fix lands. Default is
no public disclosure of the reporter.
