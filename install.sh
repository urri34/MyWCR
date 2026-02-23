#!/usr/bin/env bash
set -euo pipefail

# --- Helper functions -------------------------------------------------------
err() { echo "[ERROR] $*" >&2; }
info() { echo "[INFO]  $*" >&2; }
ok() { echo "[OK]    $*" >&2; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    err "Cal executar aquest script com a root (ex. sudo ./install.sh)";
    exit 1
  fi
}

check_cmd() { command -v "$1" >/dev/null 2>&1; }

# --- Start ------------------------------------------------------------------
require_root

if ! check_cmd systemctl; then
  err "No s'ha trobat systemd/systemctl. Aquest installador necessita systemd."
  exit 1
fi

# Detecta usuari per defecte
DEFAULT_RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
if [[ -z "${DEFAULT_RUN_USER}" ]]; then DEFAULT_RUN_USER="$(id -un)"; fi

read -rp "Usuari que executarà els serveis [${DEFAULT_RUN_USER}]: " RUN_USER
RUN_USER="${RUN_USER:-${DEFAULT_RUN_USER}}"

# Comprova que l'usuari existeix
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  err "L'usuari '$RUN_USER' no existeix. Crea'l (ex: sudo useradd -m '$RUN_USER') i torna-ho a provar."
  exit 1
fi

# Ruta base per defecte
DEFAULT_BASE="$(eval echo ~"$RUN_USER")/mywcr"
read -rp "Ruta base MYWCR_BASE [${DEFAULT_BASE}]: " MYWCR_BASE
MYWCR_BASE="${MYWCR_BASE:-${DEFAULT_BASE}}"

# Host/port web
read -rp "Host web (MYWCR_HOST) [0.0.0.0]: " MYWCR_HOST; MYWCR_HOST="${MYWCR_HOST:-0.0.0.0}"
read -rp "Port web (MYWCR_PORT) [34008]: " MYWCR_PORT; MYWCR_PORT="${MYWCR_PORT:-34008}"

# Credencials
read -rp "Contrasenya de login (CAMWEB_PASSWORD) (buit per desactivar login): " CAMWEB_PASSWORD
read -rp "Secret (CAMWEB_SECRET) (buit per generar-lo): " CAMWEB_SECRET
if [[ -z "${CAMWEB_SECRET}" ]]; then
  if check_cmd openssl; then
    CAMWEB_SECRET=$(openssl rand -base64 32)
  else
    CAMWEB_SECRET=$(python3 - <<'PY'
import secrets; print(secrets.token_urlsafe(32))
PY
)
  fi
fi

# Vídeo
read -rp "Dispositiu càmera (MYWCR_CAMERA_DEVICE) [/dev/video0]: " MYWCR_CAMERA_DEVICE; MYWCR_CAMERA_DEVICE="${MYWCR_CAMERA_DEVICE:-/dev/video0}"
read -rp "FPS (MYWCR_FPS) [30]: " MYWCR_FPS; MYWCR_FPS="${MYWCR_FPS:-30}"
read -rp "Resolució (MYWCR_SIZE) [1920x1080]: " MYWCR_SIZE; MYWCR_SIZE="${MYWCR_SIZE:-1920x1080}"
read -rp "Bitrate vídeo (MYWCR_VBITRATE) [4M]: " MYWCR_VBITRATE; MYWCR_VBITRATE="${MYWCR_VBITRATE:-4M}"
read -rp "GOP (MYWCR_GOP) [100]: " MYWCR_GOP; MYWCR_GOP="${MYWCR_GOP:-100}"
read -rp "RTSP URL (MYWCR_RTSP_URL) [rtsp://127.0.0.1:8554/mywcr]: " MYWCR_RTSP_URL; MYWCR_RTSP_URL="${MYWCR_RTSP_URL:-rtsp://127.0.0.1:8554/mywcr}"

# Altres
read -rp "Fer servir gunicorn per a la web? [Y/n]: " USE_GUNICORN
case "${USE_GUNICORN:-Y}" in
  y|Y) USE_GUNICORN=1;;
  *)   USE_GUNICORN=0;;
esac

# Copia de fitxers
read -rp "Vols copiar els fitxers del directori actual a $MYWCR_BASE? [Y/n]: " REPOCOPY
case "${REPOCOPY:-Y}" in
  n|N) DO_COPY=0;;
  *)   DO_COPY=1;;
esac

# Habilita i engega els serveis
read -rp "Vols habilitar i arrencar ara els serveis? [Y/n]: " STARTNOW
case "${STARTNOW:-Y}" in
  n|N) info "Pots arrencar més tard amb: systemctl daemon-reload && systemctl enable mywcr.target && systemctl start mywcr.target"; exit 0;;
  *) : ;;
esac

# --- Escriu /etc/mywcr/mywcr.conf ------------------------------------------
install -d -m 0755 /etc/mywcr
CONF="/etc/mywcr/mywcr.conf"
if [[ -f "$CONF" ]]; then
  cp -a "$CONF" "${CONF}.bak.$(date +%s)"
  info "S'ha fet còpia de seguretat: ${CONF}.bak.$(date +%s)"
fi

cat > "$CONF" <<EOF
# MyWCR configuració
# Generat per install.sh el $(date -u +'%Y-%m-%dT%H:%M:%SZ')
MYWCR_BASE=${MYWCR_BASE}

MYWCR_HOST=${MYWCR_HOST}
MYWCR_PORT=${MYWCR_PORT}

CAMWEB_PASSWORD=${CAMWEB_PASSWORD}
CAMWEB_SECRET=${CAMWEB_SECRET}

MYWCR_RTSP_URL=${MYWCR_RTSP_URL}
MYWCR_CAMERA_DEVICE=${MYWCR_CAMERA_DEVICE}
MYWCR_FPS=${MYWCR_FPS}
MYWCR_SIZE=${MYWCR_SIZE}
MYWCR_VBITRATE=${MYWCR_VBITRATE}
MYWCR_GOP=${MYWCR_GOP}

# Binaris (buit = autodetecta)
FFMPEG_BIN=
FFPROBE_BIN=
LSOF_BIN=
EOF
ok "Escrit $CONF"
chown root:$RUN_USER "$CONF"
ok "chown root i $RUN_USER $CONF"
chmod 640 "$CONF"
ok "chmod a 640 $CONF"

# --- Crea directoris base ---------------------------------------------------
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE"
ok "creat $MYWCR_BASE"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE/web"
ok "creat $MYWCR_BASE/web"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE/web/static"
ok "creat $MYWCR_BASE/web/static"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE/web/template"
ok "creat $MYWCR_BASE/web/template"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE/saver"
ok "creat $MYWCR_BASE/saver"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE/store"
ok "creat $MYWCR_BASE/store"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$MYWCR_BASE/log"
ok "creat $MYWCR_BASE/log"

# --- Còpia opcional del projecte al MYWCR_BASE ------------------------------
if [[ "$DO_COPY" -eq 1 ]]; then
  if check_cmd rsync; then
    rsync -a --delete --exclude 'store/' --exclude 'log/' --exclude '.git/' --exclude 'install.sh' ./ "$MYWCR_BASE/"
    ok "fitxers copiats via rsync"
  else
    info "rsync no trobat; faré una còpia simple amb cp -a (sense --delete)."
    cp -a ./ "$MYWCR_BASE/"
    ok "fitxers copiat"
  fi
  chown -R "$RUN_USER:$RUN_USER" "$MYWCR_BASE"
  ok "chown $RUN_USER:$RUN_USER de $MYWCR_BASE"
fi

# Assegura permisos d'execució
if [[ -f "$MYWCR_BASE/saver/saver.sh" ]]; then chmod +x "$MYWCR_BASE/saver/saver.sh"; ok "chmod a $MYWCR_BASE/saver/saver.sh"; fi

# --- venv -------------------------------------------------------------------
VENV_PATH="$DEFAULT_BASE/web/venv"
if [ ! -d "$VENV_PATH" ]; then
  python3 -m venv "$VENV_PATH"
  ok "S'ha creat l'entorn virtual"
else
  ok "l'entorn virtual ja existeix a $VENV_PATH (es reutilitzarà)."
fi
source "$VENV_PATH/bin/activate"
ok "activat venv"
python -m pip install --upgrade pip setuptools wheel flask gunicorn
ok "instal·lades pip setuptools wheel flask gunicorn"

# --- Unitats systemd --------------------------------------------------------
SVC1="/etc/systemd/system/mywcr-mediamtx.service"
SVC2="/etc/systemd/system/mywcr-saver.service"
SVC3="/etc/systemd/system/mywcr-web.service"

cat > "$SVC1" <<EOF
[Unit]
Description=MyWCR - RTSP server (mediamtx)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
# Comprovacions prèvies
ExecStartPre=/usr/bin/test -x "${MYWCR_BASE}/web/mediamtx"
ExecStartPre=/usr/bin/test -r "${MYWCR_BASE}/web/mediamtx.yml"
# Engega mediamtx
ExecStart="${MYWCR_BASE}/web/mediamtx" "${MYWCR_BASE}/web/mediamtx.yml"

Restart=always
RestartSec=5

# (Opcional) Evitar bucles d’engegada
StartLimitBurst=20

[Install]
WantedBy=multi-user.target
EOF
ok "Generat $SVC1"

cat > "$SVC2" <<EOF
[Unit]
Description=MyWCR - capturador i enregistrament
After=network-online.target mywcr-mediamtx.service
Wants=network-online.target mywcr-mediamtx.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
EnvironmentFile=/etc/mywcr/mywcr.conf
# Carrega config explícitament i executa l'script amb les variables
ExecStart=/usr/bin/env bash -lc 'source /etc/mywcr/mywcr.conf; exec "${MYWCR_BASE}/saver/saver.sh"'
WorkingDirectory=/
Restart=on-failure
RestartSec=3
Nice=5
IOSchedulingClass=best-effort
IOSchedulingPriority=4

[Install]
WantedBy=multi-user.target
EOF
ok "Generat $SVC2"

if [[ "$USE_GUNICORN" -eq 1 ]]; then
  WEB_CMD="/bin/bash -lc 'set -ae; set -a; source /etc/mywcr/mywcr.conf; set +a; exec "${MYWCR_BASE}/web/venv/bin/gunicorn" --workers 2 --bind "${MYWCR_HOST}:${MYWCR_PORT}" --chdir "${MYWCR_BASE}/web" app:app'"
else
  WEB_CMD="/usr/bin/env bash -lc 'source /etc/mywcr/mywcr.conf; exec python3 "${MYWCR_BASE}/web/app.py"'"
fi

cat > "$SVC3" <<EOF
[Unit]
Description=MyWCR - web (Flask)
After=network-online.target mywcr-saver.service mywcr-mediamtx.service
Wants=network-online.target mywcr-saver.service mywcr-mediamtx.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
EnvironmentFile=/etc/mywcr/mywcr.conf
ExecStart=${WEB_CMD}
WorkingDirectory=/
Restart=on-failure
RestartSec=3
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
EOF
ok "Generat $SVC3"

# --- Habilita i arrenca -----------------------------------------------------
systemctl daemon-reload || true
ok "daemon-reload"
systemctl enable mywcr-mediamtx.service || true
ok "enable mywcr-mediamtx"
systemctl enable mywcr-saver.service  || true
ok "enable mywcr-saver"
systemctl enable mywcr-web.service  || true
ok "enable mywcr-web"
systemctl stop mywcr-web.service || true
ok "stop mywcr-web"
systemctl stop mywcr-saver.service || true
ok "stop mywcr-saver"
systemctl stop mywcr-mediamtx.service || true
ok "stop mywcr-mediamtx"
systemctl start mywcr-mediamtx.service || true
ok "start mywcr-mediamtx"
systemctl start mywcr-saver.service || true
ok "start mywcr-saver"
systemctl start mywcr-web.service || true
ok "start mywcr-web"

ok "Serveis habilitats i arrencats."
echo "URL web: http://${MYWCR_HOST}:${MYWCR_PORT}" >&2

# Mostra estat web
systemctl --no-pager --full status mywcr-web.service || true
