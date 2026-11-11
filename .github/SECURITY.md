# Security reports

Do not post credentials, private keys, environment files, customer account
details or unredacted traces in a public issue. If GitHub's private vulnerability
reporting is available for this repository, use **Security → Report a vulnerability**.
Otherwise contact the maintainer through the community contact linked in the
README to arrange a private report before sending sensitive details.

Include the affected release/commit, operating system, minimal reproduction,
impact and a redacted trace. Acknowledge limitations: the dashboard is designed
for loopback access, Stratum from ASICs is plain TCP, and an operator controlling
the host can modify its files and process. See `docs/engineering/security.md`
for the implementation boundaries.
