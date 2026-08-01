# Security

Ink 002 controls a physical printer and should not be exposed directly to an
untrusted network.

## Deployment guidance

- Keep the Gunicorn service bound to `127.0.0.1`.
- Put remote installations behind an authenticated tunnel or reverse proxy.
- Set both `S002_WEB_USER` and a unique, randomly generated
  `S002_WEB_PASSWORD`.
- Prefer Cloudflare Access or an equivalent identity layer in addition to HTTP
  Basic authentication.
- Never commit `/etc/s002-web.env`, tunnel credential JSON files, live tunnel
  configuration, logs, or runtime job images.
- Shipping labels contain personal data. The COLIS workflow sends a downsampled
  image of the first page to the configured OpenRouter model for geometric
  detection. Do not enable it unless that processing is acceptable for your
  documents and deployment.
- Treat printed uploads as untrusted input. The app limits uploads to 16 MiB,
  PDFs to 20 pages, and rendered output to 30,000 rows.

The model is instructed not to transcribe private fields or decode symbols. Its
geometry is treated as advisory: the server snaps the detected frame, rejects
cuts through marked critical regions, and recomputes the final strip layout
locally. Parcel previews are returned with `Cache-Control: private, no-store`,
bounded to eight active analyses, and deleted whenever the process restarts.

Basic authentication protects the printing endpoint but does not provide rate
limiting, per-user authorization, audit history, or content moderation. Weak or
shared credentials are suitable only when unwanted prints are an accepted risk.

## Reporting a problem

Until a public issue tracker is configured, report security problems privately
to the repository owner. Do not include credentials, tunnel tokens, printer
addresses, or uploaded documents in a public report.
