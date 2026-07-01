#!/usr/bin/env bash
set -e

LOCAL_DIR="${LOCAL_DIR:-$HOME/Рабочий стол/NtoN/}"
DEPLOY_USER="${DEPLOY_USER:-ranex}"
DEPLOY_HOST="${DEPLOY_HOST:-81.88.192.41}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/srv/projects/Nton/}"
SSH_KEY="${SSH_KEY:-}"

SERVER="$DEPLOY_USER@$DEPLOY_HOST"
SSH_OPTS=(-p "$DEPLOY_PORT")
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS+=(-i "$SSH_KEY")
fi

echo "==> Checking SSH access to $SERVER..."
if ! ssh "${SSH_OPTS[@]}" -o ConnectTimeout=8 "$SERVER" "true"; then
  cat <<EOF
SSH access failed for $SERVER.

Проверь:
  1. правильный ли пользователь: DEPLOY_USER=$DEPLOY_USER
  2. правильный ли пароль/ключ
  3. разрешён ли вход по паролю на сервере

Примеры запуска:
  DEPLOY_USER=root ./deploy.sh
  DEPLOY_USER=ranex SSH_KEY=~/.ssh/id_ed25519 ./deploy.sh

Ручная проверка:
  ssh -p $DEPLOY_PORT $SERVER
EOF
  exit 1
fi

echo "==> Uploading project to server..."
rsync -avz -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude 'node_modules' \
  --exclude '.env' \
  "$LOCAL_DIR" "$SERVER:$REMOTE_DIR"

echo "==> Starting database dependencies..."
ssh "${SSH_OPTS[@]}" "$SERVER" "cd $REMOTE_DIR && docker compose up -d postgres redis"

echo "==> Building API image..."
ssh "${SSH_OPTS[@]}" "$SERVER" "cd $REMOTE_DIR && docker compose build api"

echo "==> Running database migrations..."
ssh "${SSH_OPTS[@]}" "$SERVER" "cd $REMOTE_DIR && docker compose run --rm api alembic upgrade head"

echo "==> Recreating application containers..."
ssh "${SSH_OPTS[@]}" "$SERVER" "cd $REMOTE_DIR && docker compose up -d --force-recreate api frontend"

echo "==> Current containers:"
ssh "${SSH_OPTS[@]}" "$SERVER" "cd $REMOTE_DIR && docker compose ps"

echo "==> Last API logs:"
ssh "${SSH_OPTS[@]}" "$SERVER" "cd $REMOTE_DIR && docker compose logs api --tail=50"

echo "==> Deploy finished."
