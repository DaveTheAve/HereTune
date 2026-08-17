# Security policy

## Supported versions

Security fixes target the latest public HereTune release. Update promptly when security fixes are published; a media server is a poor place to practice software archaeology.

## Intended network model

HereTune is designed primarily for a **trusted private LAN**. By default it listens on `0.0.0.0` so a headset can reach it, and the dashboard has no password until you configure one.

Do **not** expose port `8777` directly to the public Internet.

For additional LAN protection, set these in `config.toml`:

```toml
[server]
dashboard_username = "your-name"
dashboard_password = "use-a-real-password"
```

Those credentials protect the HereTune dashboard/admin API with HTTP Basic authentication. HereSphere Web API authentication is separate and uses `username`, `password`, and/or `auth_token`.

HTTP Basic authentication is **access control, not transport encryption**. On plain `http://`, credentials and media traffic are not protected like HTTPS traffic. Keep HereTune on a trusted private network or place it behind a TLS-capable reverse proxy if your environment requires encrypted transport.

Source-file deletion remains disabled by default (`allow_file_delete = false`). Keep it that way unless you specifically need it and understand the consequences.

Project repository: [https://github.com/DaveTheAve/HereTune](https://github.com/DaveTheAve/HereTune)

## Reporting a vulnerability

Please do **not** post an exploitable vulnerability as a public GitHub issue. Use the repository's GitHub private vulnerability reporting feature at [https://github.com/DaveTheAve/HereTune](https://github.com/DaveTheAve/HereTune) when available, or contact the project maintainer privately.

Include:

- HereTune version;
- Windows/Python versions;
- affected route/feature;
- reproduction steps;
- whether the issue requires LAN access, authentication, or a malicious media file;
- impact you believe is possible.

Please avoid sending actual private media, library databases, or unsanitized logs unless specifically requested.

## Data handling

HereTune performs analysis locally. It does not include telemetry or an outbound analytics client. Your filenames, paths, ratings, analysis results, and logs remain on the machine running HereTune unless **you** copy/share them elsewhere.
