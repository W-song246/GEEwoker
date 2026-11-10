#!/usr/bin/env bash
set -euo pipefail
umask 077
APP_DIR=/home/ubuntu/research2-explorer
WORKER_URL=https://geewoker.onrender.com
if [ "$(id -un)" != ubuntu ]; then
    echo '请在服务器上用 ubuntu 用户运行。'; exit 1
fi
sudo -n true
if ss -lntH | awk '{print $4}' | grep -Eq ':7862$'; then
    if ! sudo systemctl is-active --quiet research2-explorer; then
        echo '7862端口被其他程序占用，未修改项目。'; exit 1
    fi
fi
echo '[1/6] 检查新版卫星接口'
curl -fsS --connect-timeout 15 --max-time 120 "$WORKER_URL/health" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); assert {"temperature-explorer-v1","vegetation-ndvi-v1","temperature-vegetation-relationship-v1"}.issubset(d.get("capabilities", [])), "温度植被联合分析接口尚未就绪，请等待Render部署完成后重试。"'

echo '[2/6] 下载并核验独立部署包，保留旧网页'
ARCHIVE=$(mktemp /tmp/research2-explorer.XXXXXX.zip)
trap 'rm -f -- "$ARCHIVE"' EXIT
if [ -n "${1:-}" ]; then
    cp -- "$1" "$ARCHIVE"
else
    curl -fsS --connect-timeout 15 --max-time 180 "$WORKER_URL/research2/explorer/bundle.zip" -o "$ARCHIVE"
fi
python3 - "$ARCHIVE" "$APP_DIR" <<'PY'
import hashlib,json,pathlib,shutil,sys,zipfile
from datetime import datetime
target=pathlib.Path(sys.argv[2]).resolve()
if target != pathlib.Path('/home/ubuntu/research2-explorer'):
    raise SystemExit('安装路径异常，已停止')
target.mkdir(parents=True,exist_ok=True)
if (target/'server.env').is_symlink() or (target/'.venv').is_symlink():
    raise SystemExit('新版配置或环境目录是符号链接，已停止以免修改其他项目。')
with zipfile.ZipFile(sys.argv[1]) as archive:
    manifest=json.loads(archive.read('manifest.json'))
    allowed=set(manifest['sha256'])|{'manifest.json'}
    if set(archive.namelist())!=allowed or len(archive.infolist())!=len(allowed):
        raise SystemExit('部署包文件清单异常')
    verified={}
    for name,digest in manifest['sha256'].items():
        path=(target/name).resolve()
        if not path.is_relative_to(target) or archive.getinfo(name).file_size>8*1024*1024:
            raise SystemExit('部署包路径或大小异常')
        data=archive.read(name)
        if hashlib.sha256(data).hexdigest()!=digest:
            raise SystemExit('部署包内容校验失败')
        verified[name]=data
    backup=target/'backups'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    for name,data in verified.items():
        dest=target/name
        if dest.exists():
            saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    (target/'manifest.json').write_bytes(archive.read('manifest.json'))
config=target/'server.env'
if not config.exists():
    old=pathlib.Path('/home/ubuntu/research2-city/server.env')
    if not old.is_file():raise SystemExit('未找到原网页的server.env，请把原配置复制到新版目录；不要将密钥发到聊天。')
    shutil.copy2(old,config)
config.chmod(0o600)
print('代码已核验；原配置已沿用，未显示任何凭据。')
PY

echo '[3/6] 建立独立Python环境'
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi
python3 -m venv "$APP_DIR/.venv"
if ! "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --prefer-binary --timeout 45 --retries 1 \
    -i https://mirrors.cloud.tencent.com/pypi/simple -r "$APP_DIR/requirements.txt"; then
    "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --prefer-binary --timeout 45 --retries 1 -r "$APP_DIR/requirements.txt"
fi
echo '[4/6] 检查凭据已配置（不输出凭据）'
"$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import pathlib,sys
from dotenv import dotenv_values
path=pathlib.Path(sys.argv[1]);config=dotenv_values(path/'server.env')
if not (config.get('WORKER_TOKEN') or '').strip():raise SystemExit('原Worker配置为空，请检查server.env。')
print('Worker配置：已配置；智谱配置：'+('已配置' if (config.get('ZHIPU_API_KEY') or '').strip() else '未配置（真实查询仍可使用）'))
PY
echo '[5/6] 启动新版7862服务'
sudo install -m 644 "$APP_DIR/research2-explorer.service" /etc/systemd/system/research2-explorer.service
sudo systemctl daemon-reload
sudo systemctl enable research2-explorer.service
sudo systemctl restart research2-explorer.service
READY=0
for attempt in $(seq 1 30); do
    if curl -fs --max-time 2 http://127.0.0.1:7862/api/status -o /dev/null; then READY=1;break;fi
    sleep 2
done
if [ "$READY" != 1 ]; then
    echo '新版尚未启动，旧7861网页保留。请检查 sudo journalctl -u research2-explorer -n 40 --no-pager';exit 1
fi
echo '新版网页已启动：http://121.5.5.98:7862/'
echo '请打开7862地址查看新版；原7861地址显示旧网页。'
echo '[6/6] 执行一次真实卫星取数检查（不调用AI）'
"$APP_DIR/.venv/bin/python" - <<'PY'
import requests,time
session=requests.Session()
response=session.post('http://127.0.0.1:7862/api/query',json={'text':'合肥蜀山区2025年7月温度和植被','auto_ai':False},timeout=15)
response.raise_for_status();job=response.json().get('job_id')
if not job:raise SystemExit('安装后的查询条件校验未通过')
for attempt in range(100):
    result=session.get('http://127.0.0.1:7862/api/jobs/'+job,timeout=10).json()
    if result.get('status')=='done':
        if result.get('errors') or {row.get('metric','temperature') for row in result.get('results',[])} != {'temperature','ndvi'}:
            print('网页已启动，但真实取数未通过：'+ '; '.join(row.get('message','未完成') for row in result.get('errors',[])))
            raise SystemExit(1)
        for data in result['results']:
            assert data['source_mode']=='live' and data['query']['district']=='蜀山区'
            assert 0<=data['summary']['coverage']<=100
            print(data.get('metric','temperature')+'真实接口已返回；有效像元：'+str(data['summary']['count'])+'，覆盖率：'+str(data['summary']['coverage'])+'%。')
        break
    time.sleep(3)
else:
    raise SystemExit('网页已启动，真实卫星查询仍在等待。请稍后在网页中查看；不是演示数据回退。')
PY
echo '新版已启动：http://121.5.5.98:7862/'
echo '原7861网页保留。若公网无法打开，请在云服务器防火墙放行TCP 7862。'
echo '重启：sudo systemctl restart research2-explorer'
echo '停止新版：sudo systemctl stop research2-explorer'
