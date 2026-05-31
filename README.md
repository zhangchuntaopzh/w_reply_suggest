# 微信消息监测 · AI 回复建议

> **重要：本软件必须配合 CipherTalk 的 HTTP API 接口使用**，无法独立运行。  
> 请确保 CipherTalk 已在本地启动并开放 API 端口。

监测微信消息并生成 AI 回复建议。

## 功能

- **消息监测** — 自动检测新消息，支持推送监听（可调间隔，默认 3 秒）
- **AI 回复建议** — 调用大模型为每条消息生成自然得体的回复建议
- **会话管理** — 左侧会话列表，右侧消息详情，按时间降序排列
- **联系人标记** — 勾选重要联系人，LLM 生成更详细的回复
- **会话分类** — 右键会话可设置分类（家人/朋友/爱人/同学/同事/其他/敌人），LLM 根据分类调整回复语气
- **联系人搜索** — 联系人列表支持按名称实时搜索过滤
- **一键复制** — 单击回复建议自动复制到剪贴板
- **模型配置** — 内置设置面板，可配置 API 端点、模型名、API Key、Max Tokens、Temperature
- **配置持久化** — 所有设置自动保存，重启保留

## 前置条件

- CipherTalk 运行中（HTTP API 默认监听 `127.0.0.1:5031`）
- Python 3.8+（源码运行）或直接下载 exe

## 使用方式

### 直接运行 exe

```
dist\wechat_monitor.exe
```

首次运行需点击 **⚙ 模型设置** 填入 API Key 等参数并测试连接。

### 源码运行

```bash
pip install pyinstaller
python wechat_monitor.py
```

## 打包

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name wechat_monitor wechat_monitor.py
```

## 配置文件

`wechat_monitor_config.json` 生成在 exe（或脚本）同目录下，保存 LLM 配置、重要联系人、会话分类等信息。
