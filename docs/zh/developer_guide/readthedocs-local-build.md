# ReadTheDocs 本地验证说明

本文档用于指导如何在本地验证 ReadTheDocs 文档构建，**不会发布到 ReadTheDocs 网站**。

## 环境准备

确保已安装 Python 3.11 和 pip，与 `.readthedocs.yaml` 中的构建环境保持一致。

## 安装依赖

在仓库根目录执行：

```bash
python -m pip install -r docs/requirements.txt
```

## 本地构建

```bash
python -m sphinx -E -b html docs docs/_build/html
```

## 查看结果

构建完成后，在浏览器中打开：

```text
<项目根目录>/docs/_build/html/index.html
```

或者使用 Python 内置的 HTTP 服务预览：

```bash
# 进入构建目录
cd docs/_build/html/

# Python 3
python -m http.server 8000

# 然后访问 http://localhost:8000/
```

> 示例：如果项目位于 `D:\code\msagent`，则打开 `D:\code\msagent\docs\_build\html\index.html`

## 常见问题

### 1. 缺少依赖

如果遇到 `ModuleNotFoundError`，请检查 `docs/requirements.txt` 并安装所有依赖：

```bash
python -m pip install -r docs/requirements.txt
```

### 2. 构建警告

Sphinx 可能会输出警告。请确认当前改动没有新增断链、缺失的 `toctree` 目标或解析错误；如果命令退出码非 0，则需要修复。

### 3. 中文显示问题

确保 `docs/conf.py` 中设置了 `language = 'zh_CN'`，并且系统支持中文字体。

## 清理构建文件

使用 `Ctrl+C` 停止预览服务，然后回到仓库根目录执行：

```bash
rm -rf docs/_build
```

## 推送到 ReadTheDocs

仓库根目录的 `.readthedocs.yaml` 指定 Python 版本、Sphinx 配置和依赖文件。项目在 ReadTheDocs 中完成导入并配置仓库集成后，推送代码会触发远端构建；本地构建本身不会发布文档。

## 参考链接

- [ReadTheDocs 配置文档](https://docs.readthedocs.io/en/stable/config-file/v2.html)
- [Sphinx 文档](https://www.sphinx-doc.org/)
