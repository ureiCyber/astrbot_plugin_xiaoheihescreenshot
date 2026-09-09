# 小黑盒游戏与文章截图

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的插件，支持搜索小黑盒游戏，以及自动解析聊天中的小黑盒分享链接并返回截图。本版本着重改进移动端文章正文展开、图片展开和长文章截图。

## 功能

- **游戏搜索**：通过“小黑盒”或“xiaoheihe”指令搜索游戏并返回页面截图。
- **分享链接解析**：识别文本链接和 QQ JSON 卡片中的小黑盒链接。
- **完整正文与图片**：尝试展开折叠正文、加载懒加载图片，并将文章轮播图片按顺序展开。
- **长文章截图**：使用分段截图与拼接，减少超长页面出现空白或正文截断的问题；无法完整截图时返回错误提示。
- **页面整理**：隐藏评论、广告和部分页面控件，保留文章主体。
- **QQ 图片发送**：规范化为 JPEG，按尺寸与体积压缩，并通过 Base64 发送。
- **Cookie 配置**：可填写自己的小黑盒 Cookie，用于需要登录的页面。

## 安装与更新

在 AstrBot WebUI 的插件管理中，使用从 URL 安装的入口，填入本仓库地址：

```text
https://github.com/ureiCyber/astrbot_plugin_steaminfo_xiaoheihe
```

也可以将仓库克隆到 AstrBot 的 `data/plugins` 目录：

```bash
git clone https://github.com/ureiCyber/astrbot_plugin_steaminfo_xiaoheihe.git
```

本插件依赖 Playwright 和 Pillow。请在 **AstrBot 实际使用的 Python 环境**中安装插件依赖与 Chromium；以下命令在插件目录执行：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

如果使用 Docker，请在运行 AstrBot 的容器环境中完成依赖安装，并按部署方式保留浏览器依赖。安装后重载插件。

后续通过 AstrBot 插件管理的更新入口获取本仓库的新代码，更新地址由 `metadata.yaml` 中的 `repo` 指定。重载插件只会重新加载本地代码。

本版本沿用插件标识 `astrbot_plugin_steaminfo_xiaoheihe`。如果已安装原作者版本，请先备份配置，再选择要使用的版本和更新源，避免同名插件目录冲突。

## 使用

| 指令 | 说明 |
| --- | --- |
| `/小黑盒 <游戏名>` | 搜索游戏并返回截图 |
| `/xiaoheihe <游戏名>` | 英文别名 |

```text
/小黑盒 三角洲行动
/xiaoheihe Elden Ring
```

当前指令注册允许无前缀触发，也可以发送 `小黑盒 游戏名` 或 `xiaoheihe 游戏名`。

启用 `enable_link_preview` 后，直接发送小黑盒链接或 QQ 小黑盒分享卡片，即可触发截图。页面访问限制、验证码和网站结构变化可能影响结果。

## 配置

在 AstrBot WebUI 的插件配置中修改以下设置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `cookies` | 空字符串 | 自己的小黑盒 Cookie；只在自己的 AstrBot 中填写 |
| `wait_timeout` | `60000` | 页面加载超时，单位毫秒 |
| `render_delay` | `5000` | 额外渲染等待时间，单位毫秒 |
| `device_scale_factor` | `2` | 截图缩放因子，配置建议范围为 1～3 |
| `image_quality` | `95` | JPEG 质量；发送前可能进一步压缩 |
| `enable_link_preview` | `true` | 是否自动解析小黑盒分享链接 |
| `debug` | `false` | 是否输出调试日志 |

配置 schema 还保留了 `show_game_title` 和 `show_online_count`，但当前实现只发送截图，这两项暂不影响回复。

### 获取 Cookie

1. 在浏览器中访问并登录[小黑盒官网](https://www.xiaoheihe.cn/)。
2. 按 `F12` 打开开发者工具，进入“网络 / Network”面板并刷新页面。
3. 选择发往小黑盒网站的请求，在请求标头中复制 `Cookie` 的值。
4. 将其粘贴到自己 AstrBot 的插件配置 `cookies` 字段。

Cookie 属于登录凭据，请勿提交到 GitHub、发送到群聊，或放入公开日志和截图中。失效后需要重新获取。

## 开发验证

```bash
python -m unittest discover -s tests -v
node tests/validate_embedded_js.js
```

单元测试覆盖正文处理顺序、图片提取和长图分段拼接等行为；JavaScript 检查验证嵌入脚本语法。这些检查使用测试替身，不能替代真实 AstrBot、浏览器和消息平台的实装验证。

## 致谢

特别感谢 [xiaoruange39/astrbot_plugin_steaminfo_xiaoheihe](https://github.com/xiaoruange39/astrbot_plugin_steaminfo_xiaoheihe)。这个项目提供了最初的灵感与基础，让我想到并继续完善了现在这个版本；感谢原作者的创意、实现与开源分享！

同时保留原项目的致谢：感谢 [WhiteBr1ck/koishi-plugin-steaminfo-xiaoheihe](https://github.com/WhiteBr1ck/koishi-plugin-steaminfo-xiaoheihe) 的创意和设计。

## 许可证与作者

本项目沿用 [MIT License](./LICENSE)，保留原作者 `xiaoruange39` 的版权声明。本仓库由 [ureiCyber](https://github.com/ureiCyber) 维护。

网页内容来自小黑盒，相关内容权益归原权利人所有。使用时请遵守网站的服务条款和适用法律。
