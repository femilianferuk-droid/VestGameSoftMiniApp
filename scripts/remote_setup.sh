#!/usr/bin/env bash
# One-time server bootstrap for vest-mini-app on a fresh Ubuntu VPS.
# Run ON THE SERVER as root: bash remote_setup.sh
#
# Installs system packages, creates the app dir, systemd unit,
# nginx site (vestgamesoft.shop) and the `ves` CLI helper.

set -euo pipefail

APP_DIR="/opt/vest-mini-app"
SERVICE_NAME="vest-mini-app"
DOMAIN="vestgamesoft.shop"
PORT=8080

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip nginx git certbot python3-certbot-nginx rsync

mkdir -p "${APP_DIR}"

echo "==> Writing systemd unit"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Vest Mini App (Flask/Gunicorn)
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
Restart=on-failure
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}

echo "==> Writing nginx site"
cat > /etc/nginx/sites-available/${DOMAIN} <<EOF
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/${DOMAIN} /etc/nginx/sites-enabled/${DOMAIN}
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> Installing ves CLI"
cat > /usr/local/bin/ves <<'EOF'
#!/usr/bin/env bash
# ves — control the vest-mini-app service.
set -euo pipefail
SERVICE_NAME="vest-mini-app"
APP_DIR="/opt/vest-mini-app"

cmd="${1:-status}"
case "$cmd" in
  start|stop|restart)
    systemctl "$cmd" "$SERVICE_NAME"
    systemctl --no-pager --lines=5 status "$SERVICE_NAME"
    ;;
  status)
    systemctl --no-pager status "$SERVICE_NAME"
    ;;
  logs)
    journalctl -u "$SERVICE_NAME" -f --no-pager -n 100
    ;;
  health)
    curl -fsS http://127.0.0.1:8080/healthz && echo
    ;;
  env)
    "${EDITOR:-nano}" "$APP_DIR/.env"
    ;;
  *)
    echo "Usage: ves {start|stop|restart|status|logs|health|env}"
    exit 1
    ;;
esac
EOF
chmod +x /usr/local/bin/ves

echo "==> Done."
echo "Next steps:"
echo "  1. Deploy code with ./deploy.sh from your machine (this creates ${APP_DIR})."
echo "  2. Create ${APP_DIR}/.env (copy from .env.example, fill real secrets)."
echo "  3. ves restart"
echo "  4. Once DNS for ${DOMAIN} points here, run:"
echo "     certbot --nginx -d ${DOMAIN} -d www.${DOMAIN}"
