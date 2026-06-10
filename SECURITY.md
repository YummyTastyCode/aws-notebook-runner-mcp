# Security Policy

## Supported Versions

This project is currently alpha. Security fixes will target the latest published
version.

## Reporting a Vulnerability

Please open a private security advisory on GitHub if the repository supports it,
or contact the maintainer privately before publishing exploit details.

Do not include AWS credentials, account IDs, private S3 bucket names, notebook
contents, or logs containing secrets in public issues.

## Security Model

This MCP server can start paid AWS compute when explicitly enabled. Keep the
following controls in place:

- Use least-privilege IAM policies.
- Restrict S3 access to a dedicated bucket/prefix.
- Keep `AWS_NOTEBOOK_RUNNER_ENABLE_EXECUTION=false` by default.
- Require the confirmation token for paid compute.
- Allowlist instance types.
- Set max runtime and max estimated cost limits.
- Review generated plans before execution.
