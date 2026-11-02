#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/ubuntu/research2-city
WORKER_URL=https://geewoker.onrender.com
if [ "$(id -un)" != ubuntu ]; then
    echo '请使用当前服务器的 ubuntu 用户执行。'
    exit 1
fi
sudo -n true
if ss -lntH | awk '{print $4}' | grep -Eq ':7861$'; then
    if ! sudo systemctl is-active --quiet research2-city; then
        echo '7861端口已被其他程序占用，未执行安装。请先把此提示发给我。'
        exit 1
    fi
fi
echo '[1/5] 确认新版取数服务已上线'
HEALTH=$(curl -fsS --connect-timeout 15 --max-time 120 "$WORKER_URL/health")
printf '%s' "$HEALTH" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "city-boundary-temperature-v1" in d.get("capabilities", []), "Render城市边界接口尚未更新"'

echo '[2/5] 建立独立目录，下载新版文件'
mkdir -p "$APP_DIR"
ARCHIVE=$(mktemp /tmp/research2-city.XXXXXX.zip)
trap 'rm -f -- "$ARCHIVE"' EXIT
curl -fsS --connect-timeout 15 --max-time 120 "$WORKER_URL/research2/bundle.zip" -o "$ARCHIVE"
python3 - "$ARCHIVE" "$APP_DIR" <<'PY'
import pathlib
import sys
import zipfile
target = pathlib.Path(sys.argv[2]).resolve()
if target != pathlib.Path('/home/ubuntu/research2-city'):
    raise SystemExit('安装目标目录异常')
allowed = {'research2_simple_ai.py', 'server_app.py', 'remote_backend.py',
           'configure_server.py', 'requirements.txt', 'research2-city.service', 'README.txt'}
with zipfile.ZipFile(sys.argv[1]) as archive:
    if set(archive.namelist()) != allowed or len(archive.infolist()) != len(allowed):
        raise SystemExit('安装包文件清单异常，已停止')
    for item in archive.infolist():
        if item.file_size > 1024 * 1024:
            raise SystemExit('安装包文件过大')
        (target / item.filename).write_bytes(archive.read(item))
PY

echo '[3/5] 安装独立Python环境，首次下载可能需要几分钟'
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi
python3 -m venv "$APP_DIR/.venv"
if ! "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --prefer-binary \
    --timeout 45 --retries 1 -i https://mirrors.cloud.tencent.com/pypi/simple -r "$APP_DIR/requirements.txt"; then
    "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --prefer-binary \
        --timeout 45 --retries 1 -r "$APP_DIR/requirements.txt"
fi
echo '[4/5] 沿用原取数配置，检查新版连接'
"$APP_DIR/.venv/bin/python" "$APP_DIR/configure_server.py"
"$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from dotenv import dotenv_values
from remote_backend import RemoteBackend
config = dotenv_values(pathlib.Path(sys.argv[1]) / 'server.env')
RemoteBackend(config['GEE_WORKER_URL'], config['WORKER_TOKEN']).boundary('合肥')
print('新版边界接口和取数口令检查通过。')
PY
mkdir -p "$APP_DIR/gradio-cache"
chmod 700 "$APP_DIR/gradio-cache"
echo '[5/5] 启动独立网页，设置开机运行'
sudo install -m 644 "$APP_DIR/research2-city.service" /etc/systemd/system/research2-city.service
sudo systemctl daemon-reload
sudo systemctl enable research2-city.service
sudo systemctl restart research2-city.service
READY=0
for attempt in $(seq 1 30); do
    if curl -fs --max-time 2 http://127.0.0.1:7861/ -o /dev/null; then
        READY=1
        break
    fi
    sleep 2
done
if [ "$READY" != 1 ]; then
    echo '新版尚未启动，请查看下面的日志：'
    sudo journalctl -u research2-city.service -n 25 --no-pager
    exit 1
fi
echo '新版已在7861端口启动，关闭终端后仍会继续运行。'
echo '若公网IP仍为121.5.5.98，新网址为 http://121.5.5.98:7861'
echo '请在腾讯云防火墙放行TCP 7861；旧网页仍使用原来的地址。'
