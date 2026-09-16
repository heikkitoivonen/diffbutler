# Security policy

DiffButler is pre-alpha and has no supported production release yet. Security fixes
will be made on the default branch during development.

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** option if private reporting
is enabled. If it is unavailable, contact a maintainer privately using their published
contact information. If no contact is listed, open an issue requesting a private
security contact without describing the vulnerability or including sensitive data.

Include affected versions/commits, reproduction steps with synthetic data, impact,
and any proposed fix. Never include live tokens, private source, or real session logs.
Do not publish exploit details before maintainers have had an opportunity to respond.

## Scope

Credential exposure, unintended forge writes, unsafe subprocess invocation, terminal
handoff, cleanup deleting unrelated files, and leakage of private review data are
especially relevant to the planned design. Ordinary defects belong in the issue tracker.

Maintainers should enable GitHub private vulnerability reporting before public release.
