# pyaether-bridge

[English](README.md) | [日本語](README.ja.md) | **繁體中文**

把 Empyrean Aether / PyAether 接成命令列工具與 MCP 伺服器：一個常駐 daemon
在目標上維持單一 pyAether session，另一份離線 API 目錄讓約 28,000 個符號可以
檢索。全部只使用 Python 3.9 標準函式庫，沒有任何第三方相依。

**PyAether 裝在哪裡，就用哪一種傳輸方式**（同一套 CLI 與 MCP 伺服器，只差一個
環境變數）：

| PyAether 安裝位置 | `PYAETHER_TRANSPORT` | 進入方式 |
| --- | --- | --- |
| Docker 容器內 | `docker`（預設） | `docker exec` |
| 公司／實驗室的伺服器上 | `ssh` | `ssh`，沿用你現有的設定與金鑰 |
| 與 bridge 同一台 Linux 機器 | `local` | 直接在本機啟動行程 |

> **非官方專案。** 與華大九天（Empyrean）沒有任何隸屬、背書或支援關係。本專案
> 不包含、也不散布任何廠商軟體、二進位檔或文件。你需要自己**已授權**的 Aether
> 安裝與授權。目標環境的準備請見 [docs/INSTALL.md](docs/INSTALL.md)。
>
> 若華大九天（Empyrean Technology）認為本專案涉及任何侵權，請
> [開立 Issue](https://github.com/Matthew-Laplace/pyaether-bridge/issues)
> 通知我們，我們會盡快修改或移除相關內容。

```bash
git clone https://github.com/Matthew-Laplace/pyaether-bridge.git
cd pyaether-bridge
./bin/pyaether version
```

## 架構

```
宿主端行程                   宿主端 daemon               目標
------------                 -------------               ------
bin/pyaether  ┐                                         /tmp/pyaether-bridge/
bin/pyaether-mcp ├─ unix socket ─→ daemon ── transport ──→ session_bridge.py
MCP client     ┘   (NDJSON)      (單一寫入者鎖)            常駐 pyAether session
                                               │          (pyAether 只 import 一次)
                      傳輸方式：docker exec / ssh / local bash
                                                                    │
離線 API 目錄 data/catalog.sqlite ←── api build ←── Sphinx docs/html ┘
```

- CLI 與 MCP 伺服器只透過 unix socket 跟 daemon 溝通，本身不直接碰 Docker 或 SSH。
- daemon 把請求轉給目標上的常駐 Python session。該 session 只 import pyAether
  **一次**；之後的程式碼都在同一個命名空間執行，所以變數可以跨 `exec` 保留。
- 三種傳輸都經由**登入 shell**（`bash -lc`）啟動，因為 Aether 的 `AETHER_*`、
  `LD_LIBRARY_PATH`、`PYTHONPATH` 是由 image 或伺服器的 profile 提供。
- OpenAccess 的寫入本質上是單一行程，因此 daemon 用一把鎖把所有請求序列化。
- bridge 不讀取也不儲存任何憑證：SSH 認證交給 `ssh` 自己處理（建議使用金鑰 +
  ssh-agent），並強制 `BatchMode`，讓背景呼叫在需要互動輸入密碼時直接失敗，
  而不是卡住。

## 安裝

不需要安裝、不需要 pip，也不會寫入系統目錄。clone 之後直接呼叫專案內的可執行檔
（以下指令都假設目前目錄是專案根目錄）：

```bash
./bin/pyaether version
```

需求：

- 宿主端 `python3`（實測 3.9.6）。
- 目標上已安裝 Aether（含內附的 Python 3.9 與 PyAether），而且能取得授權。
- 依你選的傳輸方式具備連線手段：Docker CLI，或到伺服器的 SSH 存取。

## 連接你的 PyAether

**Docker（預設）**

```bash
./bin/pyaether status                      # transport=docker，容器 empyrean-gui
export PYAETHER_CONTAINER=my-aether        # 換成你自己的容器名稱
```

**SSH 到伺服器**

```bash
export PYAETHER_TRANSPORT=ssh
export PYAETHER_SSH_HOST=aether@lab-server # 也可以寫 ~/.ssh/config 的別名
# 選用：PYAETHER_SSH_PORT=2222、PYAETHER_SSH_OPTS="-o StrictHostKeyChecking=no"
./bin/pyaether status
```

bridge 會把 `session_bridge.py` 部署到伺服器上的 `PYAETHER_REMOTE_DIR`
（預設 `/tmp/pyaether-bridge`），並在該處啟動常駐 session。宿主端不需要安裝任何
東西。如果伺服器上的解譯器不叫 `python3.9`，用 `PYAETHER_PYTHON` 指定。

**本機（bridge 與 Aether 在同一台機器上）**

```bash
export PYAETHER_TRANSPORT=local
./bin/pyaether exec -c 'import pyAether; print(pyAether.__file__)'
```

以上設定也可以寫進 `~/.cache/pyaether-bridge/config.json`（見
[環境變數](#環境變數)），不必在每個 shell 重新 export。

## CLI 用法

```bash
./bin/pyaether status                 # 目標 + daemon + session 狀態
./bin/pyaether status --json          # 原始 JSON

./bin/pyaether daemon start           # 啟動宿主端 daemon（session 採延遲啟動）
./bin/pyaether daemon status
./bin/pyaether daemon restart
./bin/pyaether daemon stop

./bin/pyaether exec -c 'pyAether.emyInitDb()'
./bin/pyaether exec -f script.py --timeout 300
echo 'import pyAether; print(pyAether.__file__)' | ./bin/pyaether exec

./bin/pyaether api build --docs ./data/docs --jobs 4
./bin/pyaether api stats
./bin/pyaether api search emyInitDb --limit 10
./bin/pyaether api show pyAether.emyInitDb
./bin/pyaether version
```

慣例：人類可讀輸出為英文，`--json` 輸出原始 JSON。結束碼 `0` 表示成功、`1` 表示
執行期失敗（daemon 起不來、目錄資料庫不存在、`exec` 拋出例外等）、`2` 表示參數
錯誤。`exec` 會先原樣印出被執行內容的 stdout，再印最後一個運算式的值；錯誤訊息
走 stderr。

設定 `PYAETHER_BRIDGE_NO_AUTOSTART=1` 時，`status` 只回報目前狀態，不會啟動任何
東西。

## 註冊 MCP 伺服器

`bin/pyaether-mcp` 是一個 stdio 類型的 MCP 伺服器（NDJSON JSON-RPC），Codex 可以
直接拉起：

```toml
# ~/.codex/config.toml —— 確定要改自己的設定後再寫入
[mcp_servers.pyaether]
command = "/usr/bin/python3"
args = ["/absolute/path/to/pyaether-bridge/bin/pyaether-mcp"]
```

提供四個工具：`pyaether_status`、`pyaether_api_search`、`pyaether_api_help`、
`pyaether_exec`。寫入真正的 `config.toml` 屬於使用者設定變更，本專案不會代為
修改。

## 離線 API 目錄

目錄是從**你自己那份** Aether 安裝內附的 Sphinx 文件建立的（`objects.inv` 加上
產生的 API 頁面）。文件位於 `$AETHER_ROOT/tools/pyaether/docs/html`；
`api build` 會依序找 `$PYAETHER_DOCS_DIR`、使用者設定裡的 `docs_dir`、
`$AETHER_ROOT`，以及常見安裝根目錄（`/opt/empyrean/*`、`~/empyrean/*`）。

當 Aether 裝在容器裡，先把文件複製出來（`data/` 已被 git 忽略）：

```bash
CTR=empyrean-gui
DOCS=$(docker exec "$CTR" bash -lc 'ls -d "$AETHER_ROOT"/tools/pyaether/docs/html')
docker cp "$CTR:$DOCS" ./data/docs

# SSH 目標同理
DOCS=$(ssh aether@lab-server 'echo "$AETHER_ROOT"/tools/pyaether/docs/html')
scp -r "aether@lab-server:$DOCS" ./data/docs

./bin/pyaether api build --docs ./data/docs --jobs 4   # 寫入 data/catalog.sqlite
./bin/pyaether api sync-live                           # 補上只在執行期存在的符號（需要 daemon）
```

同一份資料庫裡有三類資料列，可用 `--kind` 分別檢索：

| kind | 列數（以 2026.03 實測） | 來源 |
| --- | --- | --- |
| `function` / `method` / `class` / `attribute` / `module` | 10376 | 安裝包內附的 Sphinx `api_reference` |
| `runtime` | 12582 | **文件未涵蓋**的 `dir(pyAether)` 符號 |
| `doc` / `label` | 5274 | 文件章節標籤（排序時永遠排在 API 符號之後） |

合計 28232 列。**文件並沒有涵蓋所有執行期符號**（例如 `emyInitDb` 只存在於執行
期），所以 `api build` 之後建議再跑一次 `api sync-live`。`sync-live` 可以重複
執行：它會先清掉自己上次寫入的 runtime 資料列再重算，不會重複收錄文件已經描述
過的符號；檢索時也一律優先給出帶完整型別簽章的文件條目。

> **絕對不要把 `data/catalog.sqlite` 提交進專案或對外散布。** 它是從廠商文件
> 擷取出來的介面中介資料（包含說明文字），只應該在你已獲授權的安裝上、於本機
> 產生與使用。`.gitignore` 已排除 `data/`，請維持這個設定。

## 環境變數

| 變數 | 用途 |
| --- | --- |
| `PYAETHER_TRANSPORT` | `docker`（預設）／`ssh`／`local` |
| `PYAETHER_CONTAINER` | Docker 容器名稱（預設 `empyrean-gui`） |
| `PYAETHER_SSH_HOST` | SSH 目標，例如 `user@server` 或 `~/.ssh/config` 的別名（`ssh` 時必填） |
| `PYAETHER_SSH_PORT` | SSH 連接埠（選用） |
| `PYAETHER_SSH_OPTS` | 額外的 ssh 選項，例如 `-o StrictHostKeyChecking=no`（選用） |
| `PYAETHER_PYTHON` | 目標上的解譯器（預設 `python3.9`，經由登入 shell 解析） |
| `PYAETHER_REMOTE_DIR` | session 腳本的部署目錄（預設 `/tmp/pyaether-bridge`） |
| `PYAETHER_LICENSE_SERVER` | 覆寫目標的 `LM_LICENSE_FILE`；未設定則沿用目標自己的值 |
| `PYAETHER_BRIDGE_HOME` | daemon 資料目錄（預設 `~/.cache/pyaether-bridge`） |
| `PYAETHER_DAEMON_SOCK` | daemon 的 unix socket 路徑 |
| `PYAETHER_CATALOG_DB` | 目錄資料庫路徑（預設為專案內的 `data/catalog.sqlite`） |
| `PYAETHER_DOCS_DIR` | `api build` 的預設文件目錄 |
| `PYAETHER_BRIDGE_NO_AUTOSTART` | 設為 `1` 時禁止自動拉起 daemon |

機器專屬的設定也可以放在 `$PYAETHER_BRIDGE_HOME/config.json`（預設
`~/.cache/pyaether-bridge/config.json`），讓本機路徑不會進到專案裡：

```json
{
  "transport": "ssh",
  "ssh_host": "aether@lab-server",
  "ssh_port": "22",
  "python": "python3.9",
  "license_server": "port@host",
  "docs_dir": "/path/to/tools/pyaether/docs/html"
}
```

## 已知限制

- **第三方權利。** 本專案為非官方專案，不隨附任何廠商軟體或文件；API 目錄是由你
  自己已授權的安裝在本機產生的。若華大九天（Empyrean Technology）認為本專案有任何
  內容涉及侵權，請
  [開立 Issue](https://github.com/Matthew-Laplace/pyaether-bridge/issues)，
  我們會盡快修改或移除。
- **一個授權席位。** 常駐 session 在執行期間會一直佔住一個 `PY_AETHER` 席位；
  同時再開另一個獨立的 PyAether 行程可能會取不到授權。
- **OA 存取是序列化的。** 所有請求都會經過 daemon 內同一把鎖，因此耗時較久的
  `exec` 會阻塞其他所有呼叫，直到它完成或逾時。這適合除錯與批次作業，不適合
  高並行的服務情境。
- **目標重啟。** 容器重建或伺服器重開之後，先前保存的 session 會失效；下一次
  請求會自動重建（重新 import pyAether，數秒），舊命名空間裡的變數會遺失。
- **SSH 認證。** 只使用你現有的 SSH 設定／ssh-agent。bridge 強制 `BatchMode`，
  所以需要互動輸入密碼的呼叫會直接失敗而不是卡住。請先自行確認
  `ssh <host>` 可以免密碼登入。
- **`api build` 的文件來源。** 需要你既有安裝裡的 `tools/pyaether/docs/html`
  （或你自己從容器複製出來的副本）。找不到時會明確報錯，不會默默失敗。
- **目錄資料庫是唯讀產物。** 只有 `api build` 會改寫 sqlite 檔；其他指令一律
  唯讀。
- **僅限目標端的 Python。** bridge 依賴 Aether 內附的 Python 3.9 與 OpenAccess
  共享函式庫。宿主端程式碼不做任何 EDA 運算，只負責轉送請求與檢索目錄。
- **`ssh` 傳輸只用樁驗證過。** docker 與 local 兩條路徑已做過端到端實測，ssh 則是
  以忠實重現遠端 shell 語意的樁驗證；尚未對真實遠端伺服器跑過。第一次實際上線
  請當成測試。
- **僅支援 POSIX 宿主。** daemon 透過 unix domain socket 通訊，因此執行 CLI / MCP
  伺服器的宿主必須是 macOS 或 Linux；原生 Windows 不支援。
- **僅存在於執行期的符號簽章較弱。** `runtime` 類條目來自對 SWIG 物件做 `inspect`，
  經常只顯示 `(*args, **kwargs)`。帶完整型別的簽章在文件條目裡，搜尋時會優先給出。

## 測試

```bash
bash tests/smoke_cli.sh                  # CLI 冒煙測試，不需要 Docker/daemon
python3 tests/mcp_probe.py               # MCP 協定 + 4 個工具，共 14 項檢查
python3 tests/transport_probe.py         # 傳輸層：local + 假 ssh 樁
PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py   # 額外驗證真實容器
```

## 文件

- [docs/INSTALL.md](docs/INSTALL.md) — 目標環境準備、自我檢查、疑難排解
- [ARCHITECTURE.md](ARCHITECTURE.md) — 模組劃分、內部介面、傳輸格式
- [README.ja.md](README.ja.md) — 日本語版
- [README.md](README.md) — English version

## 授權

MIT，請見 [LICENSE](LICENSE)。
