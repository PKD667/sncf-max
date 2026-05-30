#!/usr/bin/env bash
# Deploy TGV Max frontend to max.unsuspicious.org

set -e

REMOTE="root@unsuspicious.org"
APP_DIR="/var/www/max"
DOMAIN="max.unsuspicious.org"

echo "=== 1. Clone on server ==="
ssh "$REMOTE" "
    mkdir -p $APP_DIR
    cd $APP_DIR
    if [ -d .git ]; then
        git pull origin main
    else
        git clone https://github.com/yourusername/sncf-max.git .
    fi
"

echo "=== 2. Install dependencies ==="
ssh "$REMOTE" "
    cd $APP_DIR
    python3 -m venv venv
    source venv/bin/activate
    pip install flask requests rich click tabulate
    # playwright is optional for booking
"

echo "=== 3. Nginx config ==="
ssh "$REMOTE" "cat > /etc/nginx/sites-available/max << 'NGINX'
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/max /etc/nginx/sites-enabled/max
nginx -t && systemctl reload nginx
"

echo "=== 4. Launch in tmux ==="
ssh "$REMOTE" "
    tmux kill-session -t tgvmax 2>/dev/null || true
    tmux new-session -d -s tgvmax \
        'cd $APP_DIR && source venv/bin/activate && PYTHONPATH=src:\$PYTHONPATH python3 frontend/server.py'
"

echo "=== 5. Certbot ==="
ssh "$REMOTE" "certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m admin@unsuspicious.org"

echo "Done! https://$DOMAIN"
