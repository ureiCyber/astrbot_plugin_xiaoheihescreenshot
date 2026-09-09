# 小黑盒游戏与文章截图

![小黑盒游戏截图插件 Logo](./logo.png)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的插件，通过小黑盒搜索游戏并返回截图，也支持自动解析聊天中的小黑盒分享链接并生成文章截图。

## ✨ 功能

- 🎮 **游戏搜索截图**：通过指令搜索小黑盒上的游戏并返回详情页截图。
- 🔗 **分享链接解析**：识别聊天文本链接和 QQ JSON 卡片中的小黑盒链接。
- 📰 **文章截图**：尝试展开折叠正文、加载懒加载图片，并按文章顺序展开图片。
- 🖼️ **长文章拼接**：使用分段截图与拼接，减少超长页面出现空白或正文截断的问题；无法完整截图时会停止生成并提示。
- 🧹 **页面整理**：隐藏评论、广告和部分页面控件，尽量保留文章主体。
- 📤 **QQ 图片发送**：将截图规范化为 JPEG，并通过 Base64 发送。
- 🍪 **Cookie 支持**：可填写自己的小黑盒 Cookie，用于访问需要登录的页面。

## 🚀 安装与更新

### 通过 AstrBot WebUI 安装

在 AstrBot WebUI 的插件管理中，使用“从 URL 安装”入口，填入：

```text
https://github.com/ureiCyber/astrbot_plugin_steaminfo_xiaoheihe
```

### 手动安装

将仓库克隆到 AstrBot 的 `data/plugins` 目录：

```bash
cd <AstrBot目录>/data/plugins
git clone https://github.com/ureiCyber/astrbot_plugin_steaminfo_xiaoheihe.git
```

本插件依赖 Playwright 和 Pillow。请进入插件目录，在 **AstrBot 实际使用的 Python 环境**中执行：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

如果使用 Docker，请在运行 AstrBot 的容器中安装依赖和 Chromium 浏览器。安装后重载插件。

后续可通过 AstrBot 插件管理的更新入口获取新代码，更新地址由 `metadata.yaml` 中的 `repo` 指定。重载插件只会重新加载本地代码，不会代替更新操作。

本插件标识为 `astrbot_plugin_steaminfo_xiaoheihe`。如果已安装同名插件，请先备份配置，再选择要使用的版本和更新源，避免插件目录冲突。

## 📝 使用

### 指令

| 指令 | 说明 |
| --- | --- |
| `/小黑盒 <游戏名>` | 搜索游戏并返回详情页截图 |
| `/xiaoheihe <游戏名>` | 英文别名 |

示例：

```text
/小黑盒 三角洲行动
/xiaoheihe Elden Ring
```

当前指令注册允许无前缀触发，也可以直接发送 `小黑盒 游戏名` 或 `xiaoheihe 游戏名`。

### 自动解析分享链接

启用 `enable_link_preview` 后，直接发送小黑盒链接或 QQ 小黑盒分享卡片即可触发截图。支持的链接示例：

```text
https://www.xiaoheihe.cn/...
```

页面访问限制、验证码和网站结构变化可能影响截图结果。

## ⚙️ 配置

所有配置项均可在 AstrBot WebUI 的插件配置页面中修改。

### 基础设置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `cookies` | string | `""` | 小黑盒 Cookie；只在自己的 AstrBot 中填写 |
| `enable_link_preview` | bool | `true` | 是否自动解析聊天中的小黑盒分享链接 |
| `debug` | bool | `false` | 是否输出详细调试日志 |

### 截图设置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `wait_timeout` | int | `60000` | 页面加载超时时间，单位为毫秒 |
| `render_delay` | int | `5000` | 内容框架出现后额外等待的时间，单位为毫秒 |
| `device_scale_factor` | float | `2` | 截图设备缩放因子，建议范围为 1～3 |
| `image_quality` | int | `95` | JPEG 图片质量，取值范围为 1～100 |

发送前会按消息平台的图片规格进一步压缩，最终 JPEG 质量最高为 95。

### 保留字段

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `show_game_title` | bool | `true` | 配置 schema 中保留的字段；当前实现不改变回复内容 |
| `show_online_count` | bool | `true` | 配置 schema 中保留的字段；当前实现不改变回复内容 |

## 🍪 如何获取小黑盒 Cookie

1. 在浏览器中访问并登录[小黑盒官网](https://www.xiaoheihe.cn/)。
2. 按 `F12` 打开开发者工具，进入“网络 / Network”面板并刷新页面。
3. 选择发往小黑盒网站的请求，在请求标头中找到 `Cookie`。
4. 复制 Cookie 值，粘贴到自己 AstrBot 的插件配置 `cookies` 字段。

Cookie 属于登录凭据，请勿提交到 GitHub、发送到群聊，或放入公开日志和截图中。Cookie 失效后需要重新获取。

## 🔧 技术细节

- 使用 Playwright 驱动 Chromium，以移动端视口渲染小黑盒页面。
- 浏览器实例延迟初始化并共享复用，减少重复启动开销。
- 游戏搜索使用多个页面选择器依次尝试；搜索失败时会返回当前页面截图。
- 分享链接会结合文章接口响应和页面应用状态提取正文图片，并过滤评论、回复等非正文内容。
- 截图前会展开正文和图片、滚动触发懒加载，再按短视口分段截图并拼接成长图。
- 图片发送前会转换为 JPEG，按尺寸和体积限制压缩，并通过 Base64 交给 QQ 适配器。

### 开发验证

```bash
python -m unittest discover -s tests -v
node tests/validate_embedded_js.js
```

单元测试覆盖正文处理顺序、图片提取和长图分段拼接；JavaScript 检查验证嵌入脚本语法。这些检查使用测试替身，不能替代真实 AstrBot、浏览器和消息平台的实装验证。

## 🙏 致谢

特别感谢 [xiaoruange39/astrbot_plugin_steaminfo_xiaoheihe](https://github.com/xiaoruange39/astrbot_plugin_steaminfo_xiaoheihe)：多亏你分享的项目提供了最初的灵感和实现基础，我才想到并继续完善现在这个版本。感谢原作者的创意、实现与开源分享！

同时感谢 [WhiteBr1ck/koishi-plugin-steaminfo-xiaoheihe](https://github.com/WhiteBr1ck/koishi-plugin-steaminfo-xiaoheihe) 的创意和设计，当前插件保留这份来源致谢。

## 📄 许可证

本项目使用 [MIT License](./LICENSE)，版权声明和许可文本见 [LICENSE](./LICENSE)。

网页内容来自小黑盒，相关内容权益归原权利人所有。使用时请遵守网站的服务条款和适用法律。

## 👤 作者

- 维护者：[ureiCyber](https://github.com/ureiCyber)
- 原作者及版权声明：见 [LICENSE](./LICENSE) 中的 `xiaoruange39`
