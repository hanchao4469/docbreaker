# Security

Do not post credentials, private URLs, documents or exploit details in public issues. For sensitive reports, use the repository's private vulnerability reporting feature if available; otherwise contact the repository owner privately before sharing details.

This release uses a shared Bearer service secret. It is intended to sit behind a trusted application or gateway, not to provide end-user authentication. Tenant identifiers are attribution/scoping metadata and do not independently authorize a user. In particular, PDF result, retry and artifact endpoints are service-level operations.

Web fetching blocks private/non-public destinations and bounds responses and redirects. Treat these controls as defense in depth: restrict container egress and isolate the service from sensitive networks. Model-assisted extraction sends page images/text to the configured external gateway. Use only documents and services you are authorized to process.

The runtime container runs as a non-root user. Keep dependencies updated, protect the data volume, use TLS at your ingress, and never commit `.env` files or model credentials.
