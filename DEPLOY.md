# Deploy

A sketch for running the service on a Linux host behind a reverse proxy.
These are **example** values; the real production configuration is not in
this repo. Replace every `example.com`, path, user, and token placeholder
with your own before running anything.

## Shape

- One systemd unit runs the service as a non-login user on loopback.
- A reverse proxy (Caddy or Nginx) terminates TLS and forwards to the
  service.
- The Bearer token is kept in a root-only env file, read by systemd.
- Runtime state lives under the service user's home.

The service never needs to be reachable from the public internet directly;
only the reverse proxy does. The service's own bind stays on 127.0.0.1.

## systemd unit (example)

Save as `/etc/systemd/system/ciro.service`:

```
[Unit]
Description=CIRO service
After=network.target

[Service]
Type=simple
User=ciro
Group=ciro
WorkingDirectory=/opt/ciro
EnvironmentFile=/etc/ciro/ciro.env
ExecStart=/usr/bin/python3 -m agnostic_runtime.trigger.server
Restart=on-failure
RestartSec=5s

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ciro/.agnostic_runtime
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

## Env file (example)

Save as `/etc/ciro/ciro.env` with mode `0600`, owner `root:root`:

```
AGNOSTIC_TRIGGER_TOKEN=REPLACE_WITH_A_LONG_RANDOM_STRING
AGNOSTIC_TRIGGER_PORT=8770
AGNOSTIC_TRIGGER_STATE=/home/ciro/.agnostic_runtime/threads.json
CATHEDRAL_BRIDGE_DIR=/home/ciro/.agnostic_runtime/cathedral
```

Generate the token on the host, print it once, record it in a password
store, and never put it in version control:

```
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Caddy reverse proxy (example)

In `/etc/caddy/Caddyfile`:

```
example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8770
}
```

Caddy will obtain a certificate automatically; adjust for your own cert
provisioning. If you expect to run the service behind a proxy that
terminates TLS, there is nothing to change on the service side — it stays
on loopback and expects the Bearer token on the forwarded request.

## Nginx reverse proxy (example)

If you prefer Nginx:

```
server {
    listen 443 ssl http2;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8770;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

## Bring up

```
sudo useradd -m -s /usr/sbin/nologin ciro
sudo mkdir -p /opt/ciro /etc/ciro /home/ciro/.agnostic_runtime
# ... copy the repository to /opt/ciro and chown to the service user
sudo install -m 0600 -o root -g root ciro.env /etc/ciro/ciro.env
sudo systemctl daemon-reload
sudo systemctl enable --now ciro
sudo systemctl status ciro
```

To put the service in maintenance (refuses to route new work with a
plain-English message, no code deploy needed):

```
sudo -u ciro touch /home/ciro/.agnostic_runtime/BETA_PAUSED
# ... later, when ready:
sudo -u ciro rm /home/ciro/.agnostic_runtime/BETA_PAUSED
```

## Backups

The one load-bearing file is the append-only record under
`$CATHEDRAL_BRIDGE_DIR/plate.jsonl`. It is append-only and gzip-friendly;
a nightly `rsync` or `restic` snapshot of the state directory is enough.
Secrets live in `/etc/ciro/ciro.env` and should be backed up separately
with the rest of your secret store.
