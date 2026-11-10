# 温度交互查询新版

本项目是原研究2网页的独立新版，继续使用Python、NASA MOD11A1 V6.1、GEE和智谱。旧Gradio网页及7861端口保留。新版为了可靠处理地图双击，使用现有Python依赖FastAPI提供页面和接口，Leaflet负责地图，Plotly负责交互图表，无需Node运行环境。

## 已实现的功能

- 城市/市辖区的单日、整月、日期范围；多区域同时间、同区域多日期/月。一次最多4区域、6时段、共8组，单时段最多93天。
- 左侧OSM底图、地名、边界，右侧卫星温度图，所有影像使用固定色标。单日有数据也保留影像。多组影像分别保留。
- 单击不取数，按住拖动，双击任意经纬度查询。展示所在原生像元温度/真实网格边框、周边半径1000米影像和统计、两种范围各自的逐日变化。超出所选行政区的点仍可查，说明仅代表点击坐标。快速换点忽略旧响应。
- 日期热力图、空间像元密度图、小提琴图、箱线图、岭线图。分布来自最多3000个固定种子像元样本，不用区域最低/均温/最高伪造。小样本和零方差组不强行估计密度。
- 逐日表默认折叠，每个区域/日期组独立，跨月再拆表，标题明确、紧凑滚动、CSV下载，缺测保留为空。
- 选点结果另有折叠表和CSV，分别标明所在像元温度、周边圆内统计，避免混淆。
- 基于真实日均序列、空间统计、分布和区域差值调用智谱分析，单独支持选点分析、重试。未配置、限流或失败时保留数据，以“自动统计，非AI模型分析”标明降级。
- 图像全屏、PNG下载、图表交互与PNG导出，手机端上下排列。温度数据、边界文件、底图、质量筛选和统计口径均有说明。

## 文件分工

- `app.py`：启动服务、会话与任务、地图选点、图片与CSV接口。
- `query_parser.py`、`city_catalog.json`：自然语言条件、追问、组合与日期校验。
- `backend.py`：真实Render调用，以及明确隔离的合成演示后端。
- `analysis.py`：数据事实、趋势/对比统计和智谱分析。
- `static/index.html`、`style.css`、`app.js`：网页排版、Leaflet地图双击及Plotly图表。
- `static/vendor/`：固定版本的本地前端依赖，不在网页加载时依赖CDN。
- `research2-explorer.service`、`install.sh`：独立Ubuntu服务和安装脚本。

## 部署和真实数据

先将配套Worker更新发布，新增 `temperature_explorer_api.py`，在Worker `app.py` 注册新路由。旧路由保留。Worker健康接口必须包含 `temperature-explorer-v1`。

服务器Ubuntu用户运行：

```bash
curl -fsS https://geewoker.onrender.com/research2/explorer/install.sh -o /tmp/research2_explorer_install.sh && bash /tmp/research2_explorer_install.sh
```

也可先上传部署ZIP和安装脚本，然后执行 `bash install.sh /上传后的完整路径/research2_explorer_bundle.zip`。

目标目录 `/home/ubuntu/research2-explorer`，端口7862，服务 `research2-explorer`。安装沿用 `/home/ubuntu/research2-city/server.env`，保留旧站。第一次更新前确认云防火墙允许TCP 7862。代码更新会备份到新版目录的 `backups/` 中。GEE服务私钥继续只保存在Render，浏览器和公开包没有个人凭据。

安装器启动服务后自动执行一次真实卫星查询。只有这一步通过，才能称服务器真实取数检查通过；本地合成演示和离线计算图测试不代表线上GEE验证。

## 本地运行

Python 3.10或以上，在本目录创建虚拟环境并安装 `requirements.txt`。

```bash
python -m venv .venv
# 激活环境后：
python -m pip install -r requirements.txt
python app.py --demo --port 7862
```

打开 http://127.0.0.1:7862/ 。`--demo` 会在网页明显标注“合成数据交互演示”，不调用GEE/AI，不用于研究。真实运行请在本目录放已有 `server.env`（GEE_WORKER_URL、WORKER_TOKEN、ZHIPU_API_KEY、可选ZHIPU_MODEL），去掉 `--demo`；新版Worker必须已部署。不会在真实取数出错时自动使用演示数据。

## 验证和限制

`test_explorer.py` 验证解析、会话、任务、CSV、AI事实和并发选点。`test_worker_graph.py` 在源码工作区中用Earth Engine官方离线测试框架验证原生网格和计算图，需要原Worker依赖。`check_browser.mjs` 是本机浏览器检查脚本，不属于生产运行依赖。

约1公里分辨率不表示建筑温度；小圆范围常只有少量像元。像元覆盖率与有效日期数不同。合成影像的空间均温与逐日区域均温的简单平均不同。AI不替代观测、不能证明温度变化的因果。
