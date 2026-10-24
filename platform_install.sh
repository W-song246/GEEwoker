#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/ubuntu/temperature-platform
WORKER_URL=https://geewoker.onrender.com
if [ "$(id -un)" != ubuntu ]; then
    echo '请使用当前服务器的 ubuntu 用户执行此安装程序。'
    exit 1
fi

echo '[1/6] 准备服务器运行环境'
sudo -n true
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv nginx
mkdir -p "$APP_DIR"
ARCHIVE=$(mktemp /tmp/temperature-platform.XXXXXX.zip)
trap 'rm -f -- "$ARCHIVE"' EXIT

echo '[2/6] 下载并展开平台文件'
curl -fsS --connect-timeout 15 --max-time 180 "$WORKER_URL/platform/bundle.zip" -o "$ARCHIVE"
python3 - "$ARCHIVE" "$APP_DIR" <<'PY'
import pathlib
import sys
import zipfile
target = pathlib.Path(sys.argv[2]).resolve()
allowed = {'app.py', 'requirements.txt', 'configure_server.py', 'nginx.conf',
           'temperature-platform.service', 'platform.env.example', '使用说明.txt'}
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = set(archive.namelist())
    if names != allowed:
        raise SystemExit('安装包文件清单异常，已停止安装。')
    for item in archive.infolist():
        if item.file_size > 1024 * 1024 or item.filename not in allowed:
            raise SystemExit('安装包内容异常，已停止安装。')
        (target / item.filename).write_bytes(archive.read(item))
PY

echo '[3/6] 安装 Python 运行环境，下载可能需要几分钟'
python3 -m venv "$APP_DIR/.venv"
if ! "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --prefer-binary \
    --timeout 45 --retries 1 -i https://mirrors.cloud.tencent.com/pypi/simple \
    -r "$APP_DIR/requirements.txt"; then
    echo '腾讯云软件源暂不可用，改用官方 PyPI 重试。'
    "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --prefer-binary \
        --timeout 45 --retries 1 -r "$APP_DIR/requirements.txt"
fi

echo '[4/6] 生成私有配置和登录信息'
BOOTSTRAP_WORKER_TOKEN="${WORKER_TOKEN:-}" "$APP_DIR/.venv/bin/python" "$APP_DIR/configure_server.py"
mkdir -p "$APP_DIR/data" "$APP_DIR/gradio-cache"
chmod 700 "$APP_DIR/data" "$APP_DIR/gradio-cache"

echo '[5/6] 配置开机自动运行和网站入口'
sudo install -m 644 "$APP_DIR/temperature-platform.service" /etc/systemd/system/temperature-platform.service
sudo systemctl daemon-reload
sudo systemctl enable temperature-platform.service
sudo systemctl restart temperature-platform.service

if [ -f /etc/nginx/sites-available/default ] && [ ! -e /etc/nginx/sites-available/default.before-temperature ]; then
    sudo cp -p /etc/nginx/sites-available/default /etc/nginx/sites-available/default.before-temperature
fi
sudo install -m 644 "$APP_DIR/nginx.conf" /etc/nginx/sites-available/temperature-platform
if [ -L /etc/nginx/sites-enabled/default ]; then
    sudo unlink /etc/nginx/sites-enabled/default
fi
sudo ln -sfn /etc/nginx/sites-available/temperature-platform /etc/nginx/sites-enabled/temperature-platform
sudo nginx -t
sudo systemctl reload nginx

echo '[6/6] 检查界面是否启动'
READY=0
for attempt in $(seq 1 30); do
    if curl -fs --max-time 3 http://127.0.0.1:7860/ -o /dev/null; then
        READY=1
        break
    fi
    sleep 2
done
if [ "$READY" != 1 ]; then
    echo '界面未按时启动，请截图下面的日志。'
    sudo journalctl -u temperature-platform.service -n 25 --no-pager
    exit 1
fi
echo '安装完成，平台地址：http://121.5.5.98'
echo '请在文件管理器中打开：/home/ubuntu/temperature-platform/登录信息.txt'
echo '账号密码只保存在服务器私有文件中，没有输出到终端。'
echo '关闭此终端后，平台仍会继续运行。'
