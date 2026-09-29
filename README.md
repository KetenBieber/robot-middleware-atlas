# Robot Middleware Atlas

Robot Middleware Atlas 是一组中文机器人中间件与工业通信源码深度博客，聚焦源码导读、运行时架构、工程设计与控制系统影响。当前研究对象包括 Apollo Cyber RT、Orocos RTT、YARP、Eclipse eCAL、Eclipse Zenoh、LCM、Eclipse Cyclone DDS、eProsima Fast DDS，以及 IgH EtherCAT Master 与 SOEM。

文章不按目录罗列模块，而是沿真正的问题进入源码：一条消息怎样从 public API 穿过 transport、queue、scheduler 到达 callback；对象由谁创建和拥有；哪个线程执行；数据在哪里复制；消费者过载时发生什么；这些选择怎样影响机器人闭环的延迟、抖动和数据新鲜度。

## 浏览网站

在本机先运行构建命令，再打开 `site/index.html` 预览；也可使用本地快捷入口 [index.html](index.html)。页面使用 Sphinx、`sphinx_rtd_theme`、RST/MyST 和 `toctree`，支持侧栏、页内目录、搜索、源码链接和 Previous/Next 导航。

## 内容组织

- `content/`：按页面分段的 Markdown 源稿；
- `docs/`：Sphinx 导航、手写深度章节和主题资源；
- `tools/build_sphinx_sources.py`：将 `content/` 页面生成到 Sphinx 文档树；
- `site/`：本地构建生成的 HTML（不提交到源码仓库）；
- `.github/workflows/pages.yml`：独立的 GitHub Pages 构建、验证与发布工作流；
- `.internal/`：研究、审阅、实验、QA 和历史产物，不进入网站。

写作规范见 [CONTENT_SCHEMA.md](CONTENT_SCHEMA.md)，多 Agent 的研究与发布边界见 [AGENTS.md](AGENTS.md)。具体执行顺序、**本地源码优先**、事实复用、增量审查、发现闭环与构建分级见 [REVIEW_PROTOCOL.md](REVIEW_PROTOCOL.md)。上游源码已下载到本机时，直接从本地固定提交截取真实代码段落，不默认重新克隆或拉取远程仓库。

## 构建

依赖版本锁定在 [requirements.txt](requirements.txt)。Windows PowerShell：

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe tools/build_sphinx_sources.py
./.venv/Scripts/python.exe -m sphinx -b html -W --keep-going docs site
./.venv/Scripts/python.exe tools/check_static_links.py site
./.venv/Scripts/python.exe tools/check_editorial_language.py
```

## 独立发布：方案 A

本项目是**独立的 GitHub Pages 项目网站**。源码仓库使用 `KetenBieber/robot-middleware-atlas`；发布完成后，访问路径为 `https://ketenbieber.github.io/robot-middleware-atlas/`。该仓库只提交 Markdown/文档配置/构建脚本，推送 `main` 后由本仓库的 GitHub Actions 在 Linux 上安装 Sphinx、生成并严格检查文档，直接发布 `site/` 产物。个人网站 `KetenBieber.github.io` 仅保留指向上述地址的导航链接，**不再复制编译后的 HTML、不再使用跨仓库部署脚本**。

`source-audit/` 保留在本机供 Agent 直接检索、摘录和验证固定提交的源码；它与 `.internal/`、`site/`、`docs/generated/`、`dist/` 一起被 Git 忽略。云端仅用已撰写的源码文章构建网页，不需要重新拉取任何大型上游中间件仓库。完整的首次建仓、Pages 设置、旧网站迁移与安全检查步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

本地需要完整发布前检查时，在 Windows 上执行 `make html-full`；在 Linux/CI 上执行 `make PYTHON=python html-full`。`tools/package_site.py` 仅用于需要离线 ZIP 时手动生成发布包，不参与日常 CI。

## 新增或重写文章

先提出一个真实运行时问题，再建立固定版本源码地图和完整调用链。研究过程保存在 `.internal/research/`；最终稿只能保留对读者有用的源码、解释、对象关系、线程模型、数据结构、设计取舍与控制系统含义。

短页可以在 `content/*.md` 中用 `<!-- PAGE: project/slug -->` 分页；大型问题可以直接写入 `docs/generated/<project>/`，并加入对应项目的 `toctree`。不要在发布页加入 Agent 工作记录、审阅状态、PASS/PARTIAL、证据矩阵或测试完成度。
