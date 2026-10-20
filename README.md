# zcode-local-patches

给 [zcode-app-cli](https://www.npmjs.com/package/zcode-app-cli) 打本地补丁的工具包：
**一条命令装完**，装到任意设备上复刻同一套修复。

主要解决一个在生产里反复咬人的问题：

> **`Model request failed` 重试 5 次**
> 长思考块 / 超长工具调用时，引擎把整段输出缓冲起来，300 秒内一个字节都不吐；
> Node 内置 fetch（undici）默认 `bodyTimeout = 300s`，直接把连接掐断。

2026-10-20 实测（DeepSeek-V4.1 引擎）：

```
引擎: ended … reason=client-left prompt=221713 tokens=20146 after=323.68s
客户端: TypeError: terminated  at Fetch.onAborted (undici)  durationMs=324751
```

本补丁把 LLM 端点的传输层换成基于 `node:http` 的实现，**天生没有 body timeout**，
只有调用方的 AbortSignal 能中断。

---

## 支持版本（重要）

补丁锚点是按编译产物（minified bundle）的形状写的，**必须钉死版本**：

| zcode-app-cli | 状态 |
| --- | --- |
| `3.15.3-37` | ✅ 已验证 |
| 其它版本 | ❌ 锚点大概率失配，请勿混用 |

## 安装

前置：Node.js ≥ 20、npm。

```bash
# 1) 先装官方包（版本必须一致）
npm i -g zcode-app-cli@3.15.3-37

# 2) 拉本仓库并安装
git clone https://github.com/zhangjianbang-nb/zcode-local-patches.git
cd zcode-local-patches
./install.sh              # 只装请求层永久补丁（纯 JS，不需要 Python）
# 或
./install.sh --full       # 另装其余补丁（需要 python3）+ systemd 自动重打
```

安装脚本会：

1. 找到 `npm root -g` 下的 `zcode-app-cli/vendor/zcode.cjs`；
2. 把三个 shim（streamfetch / imagecap / degencap）的**源码内联**进 bundle
   （不是 `require` 外部文件，装完删掉补丁目录也不影响运行）；
3. `--full` 时再把其余补丁与守卫复制到 `~/.zcode/patches-lib`，并启用
   `systemd --user` 的 `zcode-patches.path` —— npm 升级覆写 bundle 后自动重打。

## 验证

```bash
node ~/.zcode/patches-lib/patch-zcode-permanent.cjs --check
# ✅ 已就位（内联，无外部 require）

zcode -p "Reply with the single word: ok" --mode yolo
# ok
```

⚠️ 补丁**只对新启动的进程生效**。装完请退出当前 zcode 会话再重开（或用
`zcode --resume <sess_id>` 续接上下文）。

## 卸载

```bash
./uninstall.sh             # 停用 systemd 自动重打
./uninstall.sh --restore   # 从 pristine 快照还原 bundle（撤销所有补丁）
./uninstall.sh --purge     # 删除 ~/.zcode/patches-lib（会二次确认）
```

## 补丁清单

### 默认安装（纯 JS，内联进 bundle）

| 补丁 | 作用 |
| --- | --- |
| streamfetch | LLM 端点改用 `node:http`，**移除 undici 300s bodyTimeout** |
| imagecap | 请求层图片数上限（默认不限；`ZCODE_IMAGE_CAP` 可设限），超限逐出最旧的图 |
| degencap | 截断历史里 `"I — I —"` 式 token 级周期环，避免每个请求重放垃圾 |

### `--full`（需要 python3）

| 补丁 | 作用 |
| --- | --- |
| ate-contextwindow | TUI 上下文百分比分母修正 |
| vno-buffer | autoCompact 触发阈值（按 provider_config 的 contextWindow 反推） |
| loop-guard | 工具调用死循环守卫（滑动窗口 + 文件指纹 + 三级退让） |
| slash-path | 绝对路径被误判成 `/command` 的修复 |
| truncation-resume | 截断续写文案 |
| subagent-watchdog | 子代理看门狗改为「有界延长」而非当场处决 |
| turn-failure-reason | 让 `ToolTimeout` 原样透传，不再显示成「Turn was cancelled」 |
| subagent-limit | 子代理并发闸门（默认 2） |
| zaicode-compact-threshold | 桌面/dev 版 ZCode 的 compact 阈值 |
| no-websearch | 剔除 provider-native WebSearch（直连 openai-compatible 时无实现） |
| no-safety | **删除 system prompt 里的安全防线段**（见下方风险提示） |

## 风险与注意事项

- **npm 升级会覆写 `vendor/zcode.cjs`**。没有 systemd 的环境升级后需重跑 `./install.sh`；
  有 systemd 时 path unit 会自动重放。
- `no-safety` 会移除 zcode 内置的安全约束段落。它会改变模型对危险请求的默认反应，
  请自行评估是否在 `--full` 中启用。
- 补丁会修改本地 bundle；升级/卸载前建议先 `npm i -g zcode-app-cli@3.15.3-37 --force`
  或使用 `./uninstall.sh --restore` 回到原始状态。
- 本工具包不包含任何账号、密钥或内部地址；`config/provider.example.json` 只是配置结构示例。

## 离线 / 整包安装（Release）

Release 里同时提供**已打好补丁的完整包**（`zcode-app-cli-3.15.3-37-patched.tgz`），
适合没有网络或不想在目标机跑脚本的场景：

```bash
npm i -g ./zcode-app-cli-3.15.3-37-patched.tgz
```

> 该 tarball 内含上游 ZCode 运行时（Apache-2.0）及其第三方依赖，
> 包内已保留 `LICENSE`、`LICENSES/` 与 `docs/THIRD_PARTY_CONTENT.md`。
> 分发前请保留这些文件；上游与依赖各自的条款不因本仓库的 MIT 许可而改变。

## License

本仓库的脚本与文档：MIT。
`vendor/zcode.cjs` 及其第三方依赖：见 zcode-app-cli 包内 `LICENSE` / `LICENSES/` / `docs/THIRD_PARTY_CONTENT.md`。
