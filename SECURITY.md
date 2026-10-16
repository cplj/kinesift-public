# Security

Report suspected vulnerabilities privately to hello@kinesift.com. Include the
version, a minimal synthetic reproduction, expected behavior and observed behavior.
Please exclude private data and credentials. No response-time commitment is made.

This is a local single-user simulation. It is not a security sandbox. Do not run
untrusted code in the evaluator process. The HTTP server has no authentication,
TLS, tenant boundary or production availability guarantee; keep it on loopback.
It cannot observe effects outside the submitted simulated action transcript.
