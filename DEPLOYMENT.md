# 独立部署到 GitHub Pages（方案 A）

本项目单独使用 `KetenBieber/robot-middleware-atlas` 仓库和 GitHub Pages 工作流，不把编译后的 HTML 存进个人网站仓库。公开访问路径是 `https://ketenbieber.github.io/robot-middleware-atlas/`，个人网站只负责提供一个指向该地址的导航链接。

## 仓库边界

- **上传**：`content/` 中的文章、`docs/` 中手写导航/主题、`tools/` 构建及检查工具、`requirements.txt`、本仓库的 `.github/workflows/pages.yml` 和维护规范。
- **只在本机保留**：`source-audit/` 的六套上游源码、`.internal/` 的 Agent 审查材料和事实索引、虚拟环境、`docs/generated/`、`site/` 和 `dist/`。
- **不接触**：`E:\Keten.github.io\Keten.github.io` 个人网站的 Git 仓库、`latex-notes` 和其他个人网站源文件。两个仓库的 Git/CI 构建和部署完全独立。

`docs/generated/` 的文章与各项目 `index.rst` 均可由 `tools/build_sphinx_sources.py` 根据 `content/` 重新生成。独立仓库首次检出后，先运行生成器，再运行 Sphinx；不能仅靠静态仓库里已经存在生成文件。

## 首次在 GitHub 创建独立仓库

以下 PowerShell 命令只需执行一次；确保命令运行在中间件项目**自身根目录**，而不是它上一级的 Transformers 练习仓库：

```powershell
cd "E:\具身大模型\transformers-huggingface-exercise\robot-middleware-atlas"

# 独立初始化，避免把上级工作区及其其他练习项目带入此次推送。
git init -b main
git add .
git status --short
git diff --cached --stat

# 确认暂存列表不含 source-audit/、.internal/、site/、
# docs/generated/、dist/ 或任何 .venv/ 内容。
# 如果没有配置过 Git 提交身份，先执行：
# git config user.name "你的 GitHub 显示名"
# git config user.email "你在 GitHub 已验证的邮箱或 noreply 地址"
git commit -m "Initialize standalone robot middleware documentation"

# 如果本机 GitHub CLI 尚未登录，需由本人完成授权：
gh auth login
gh repo create KetenBieber/robot-middleware-atlas --public --source=. --remote=origin --push
```

若已经创建了远程空仓库，不要再次创建；改为在本仓库设置正确的 `origin` 并推送 `main`。不要对工作区上一级的仓库执行 `git add .`，也不需要让本仓库下载 `source-audit/` 所含的上游代码。

## 首次开启独立 Pages

在新仓库中进入 **Settings → Pages → Build and deployment → Source → GitHub Actions**。然后在 **Actions** 中运行 `Publish Robot Middleware Atlas`（或者在设置完成后向 `main` 提交一个新改动）。工作流会自动：

1. 只检出本项目的 Markdown 和脚本，设置 Python 3.12，安装 `requirements.txt` 中固定的 Sphinx 依赖。
2. 执行 `make PYTHON=python html-full`，生成全站，并以警告视为错误的方式构建；检查内部链接和禁止发布的内部审查文件。
3. 在编译产物根目录创建 `.nojekyll`，保留 Sphinx 的 `_static/`、`_sources/` 等下划线目录。
4. 通过 `actions/upload-pages-artifact` 和 `actions/deploy-pages` 发布 `site/`；**不提交** `site/` 或 `gh-pages` 分支，不触发个人网站的 Vite 构建。

验证新站点的首页、各项目首页和至少一篇包含真实源码的大型文章均能打开后，再执行下一步迁移。

## 个人网站的一次性清理

个人网站仓库目前可能仍包含旧的 `public/robot-middleware-atlas/` 快照。只有**确认新项目仓库 Pages 已发布且可访问**后，才能从个人网站仓库中删除这份旧快照，以免两套内容占用相同路径。

个人网站目前的入口位于 `content/notes/robot-middleware-atlas.md`，其 `externalUrl` 和正文链接仍指向仓库里那份旧 HTML 副本。迁移时先在**个人网站**的这个文件中将两处链接改为绝对项目站地址：

```yaml
externalUrl: "https://ketenbieber.github.io/robot-middleware-atlas/"
```

```markdown
[进入完整文档站](https://ketenbieber.github.io/robot-middleware-atlas/)
```

这次只改导航，不把生成的中间件网页重新加入个人网站仓库。之后在个人网站根目录执行：

```powershell
cd "E:\Keten.github.io\Keten.github.io"

# 先确认目标目录和入口文件没有其他需要保留的未提交编辑。
git status --short -- public/robot-middleware-atlas content/notes/robot-middleware-atlas.md

# 新站点已上线、旧快照没有需要保留的编辑后才执行：
git rm -r -- public/robot-middleware-atlas
git add -- content/notes/robot-middleware-atlas.md
git commit --only -m "Link standalone middleware Pages and remove duplicate files" -- content/notes/robot-middleware-atlas.md public/robot-middleware-atlas
git push origin main
```

这个定向提交只包含入口文件和旧静态副本的删除；不要把 `latex-notes` 等无关未提交更改混进去。

## 日常写作与发布

在本地的 `source-audit/` 直接截取固定提交源码，修改 `content/` 文稿，在本机按需要执行 `make html` 或 `make html-full`。只将源稿、配置和工具提交到中间件仓库的 `main`；该仓库的 Action 自动构建并发布。**个人网站不需要重新构建，也不需要复制、压缩、解压或同步 260 个编译文件。**

若云端构建失败，只需查看本项目的 Actions 日志，不要为修复构建问题重新下载上游源码。正式发布仍受 `REVIEW_PROTOCOL.md` 中事实核查和审查问题闭环规则约束。
