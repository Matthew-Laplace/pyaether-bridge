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

./bin/pyaether sim backends --probe            # 有哪些模擬器可用
./bin/pyaether sim run rc.cir --backend ngspice  # 開放原始碼的 SPICE
./bin/pyaether sim run tb.scs --backend spectre --mode ax

./bin/pyaether layout gen spec.json -o out.gds    # KLayout：產生版圖
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout compare a.gds b.gds         # 結束碼 0 = 完全相同

./bin/pyaether dsh install                        # 把 skill 裝到 <workspace>/.dsh/skills
./bin/pyaether version
```

## 推薦組合

四個元件就能撐起整條流程，其中只有一個需要付費授權。

| 元件 | 角色 |
| --- | --- |
| **DeepSeek Harness** | 驅動整個迴圈；透過 CLI 連上 bridge，所以沒有任何 session 需要先付出一份 MCP 工具 schema 的成本 |
| **`bin/pyaether`** | 唯一的介面：API 檢索、版圖、原理圖與模擬共用同一套詞彙 |
| **ngspice** | 開源 testbench 模擬（`sim run --backend ngspice`） |
| **KLayout** | 版圖幾何（`layout gen / info / drc / boolean / convert / compare`） |

`api *`、`layout *`、`sim *` 與 `version` 會自己解析目標，既不需要 daemon，也不需要
Aether 席位。`exec` 與 `sch snapshot|build|roundtrip` 則需要：它們在常駐 session 內
執行，而該 session 從第一次 `exec` 把它啟動的那一刻起，就一直佔著一個 `PY_AETHER`
席位。`sch netlist` 不需要 —— 它是把畫布文件轉成 SPICE 文字的純函式（實測：兩個電阻
的分壓器約 0.05 s）。任務需要 Aether 資料庫時再啟動 session；任務只涉及幾何或 SPICE
時就讓它保持關閉。

### 節省 token

以下是在本專案自己的指令上實測的結果：

| 指令 | 預設 | 加上旗標後 |
| --- | --- | --- |
| `layout info --json` | 1885 B | 884 B |
| `status --json` | 2840 B | ≈1700 B（隨 session 狀態而異） |
| `sim run --json`，10 008 點的暫態 | 389 231 B | 952 B（`--summary`） |

1. **要答案，不要取樣點。** `--summary` 把每條波形縮成
   `{n, first, last, min, max}`，用 1/400 的大小回答「它收斂了嗎、收斂到多少」。
   `metadata.plots` 與 `metadata.artifacts` 仍然指出圖表與原始檔的名字，所以取樣點
   只差一次讀檔。
2. **讓精簡輸出成為預設，失敗時再拿 `--debug`。** 執行診斷資訊 —— 暫存目錄、命令列、
   模擬器的 banner —— 在呼叫成功時會被丟掉，失敗時才保留，而那正是值得讀的時候。
3. **先查目錄，不要猜符號。** `api search` 與 `api show` 約 0.12 s 就能讀完離線目錄，
   不需要 session。自己編一個 `pyAether.*` 名字要付出一次往返，還可能回傳一個讀起來
   像沒事的 `nil`。

### 節省往返次數

1. **優先用複合指令。** `layout gen spec.json -o out.gds` 用一次呼叫就建出整份版圖；
   一次建一個圖形就是每個圖形各一次呼叫。
2. **用一次呼叫讀回驗證。** `layout compare a.gds b.gds` 在兩份版圖完全相同時結束碼為
   `0`，而 `sch roundtrip spec.json` 會建立、讀回並比對連接性。兩者都能用一個問題就
   確認一次寫入。（`sch roundtrip` 以及任何會寫入的 `layout` 指令都需要你先授權 ——
   bridge 不會代你詢問。）
3. **看 `ok` 與 `status`，不要看說明文字。** `SUCCESS` / `PARTIAL` / `FAILURE` 才是
   契約；內容非空並不代表成功。
4. **長時間的呼叫放到背景。** 大型的 `layout gen`、`layout deck` 或 `sim run` 應該用
   背景模式，這樣逾時不會讓整次執行白費。
5. **批次處理目標端的工作。** 每個請求都要經過 daemon 的同一把鎖，所以一個冗長的
   `exec` 會擋住其他請求。每個已授權的 view 送一次有界的交易 —— 絕不要每個 instance、
   wire 或 label 各一次呼叫。

## 版圖（KLayout）

版圖的產生、讀取、檢查與轉換都用 **KLayout**，並依其作者支援的無介面用法驅動：
`klayout -b -r <script>`（`-b` 等於 `-zz -nc -rx`，所以沒有 GUI、不讀設定檔、也不會
隱式載入巨集）。這裡不安裝任何 Python 套件 —— 腳本是在 KLayout 自己的解譯器裡執行
—— 而且 KLayout 一律以獨立行程叫用，絕不 import。

```bash
./bin/pyaether layout probe                        # 可用嗎？哪個版本？
./bin/pyaether layout gen spec.json -o out.gds     # box、polygon、path、text、陣列
./bin/pyaether layout info out.gds --layers '{"m1":[1,0]}'
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout boolean and --a 1/0 --b 2/0 --out-layer 4/0 --source out.gds -o and.gds
./bin/pyaether layout convert out.gds out.oas      # KLayout 自帶的串流工具
./bin/pyaether layout compare a.gds b.gds          # 結束碼 0 = 完全相同
./bin/pyaether layout deck rules.drc --source out.gds
```

有三個行為是刻意的：DRC 執行結果若有違規，結束碼為 `3`（不乾淨的版圖不該通過 CI）；
合併幾何後僅相觸的邊界不算間距錯誤；無法評估的規則會讓整次執行失敗，而不是回報零
違規。詳見 [docs/LAYOUT.md](docs/LAYOUT.md)。

## DeepSeek Harness

bridge 是一個 MCP 伺服器，所以 DeepSeek Harness 分兩半接上它：

- **註冊**（`mcp__pyaether__*`）來自一個本機 **bundle**（`dsh-bundle-pyaether-bridge`），
  透過 harness 自己的外掛管理器安裝。bundle 是獨立套件，所以不需要手改任何 profile
  —— 這點很重要，因為桌面應用程式管理的 profile 會拒絕 CLI 組合，並把手寫的 patch
  回滾。
- **skill**（怎麼操作這些工具）則是安裝到工作區裡：

```bash
./bin/pyaether dsh install     # → <workspace>/.dsh/skills/pyaether-bridge/SKILL.md
./bin/pyaether dsh status      # 裝了嗎？同步嗎？哪個 profile 會掃描這個 root？
./bin/pyaether dsh uninstall   # 再把它移除
```

skill 屬於工作區層級，不是全域：filesystem skill provider 出廠時是停用的，之後只掃描
明確列出的 `customSkillDirs`，所以 `$DSH_HOME/skills` 永遠不會被讀取。因此
`dsh status` 會回報某個 profile 是否真的掃描那個根目錄，而當沒有任何 profile 掃描時，
`dsh install` 會印出應該加入的完整設定區塊 —— 裝了卻沒被掃描到的 skill 不會有任何
作用。詳見 [docs/DEEPSEEK-HARNESS.md](docs/DEEPSEEK-HARNESS.md)。

## 模擬器

網表不綁定單一模擬器：同一個檔案可以用開源的 ngspice 檢查，再用 Cadence Spectre/APS
簽核，而兩者都透過同一套結果契約回傳（`ok` / `status` / `data` / `errors` /
`metadata`）。模擬器目標是**獨立於** PyAether 目標選定的，所以 PyAether 住在容器裡時，
ngspice 可以在你的筆電上跑。

| 後端 | 類型 | 模式 |
| --- | --- | --- |
| `ngspice` | 開源 | 分析方式由網表宣告 |
| `spectre` | 商業 | `spectre`、`aps`、`x`、`cx`、`ax`、`mx`、`lx`、`vx` |
| `alps` | 商業（華大九天） | `basic`、`turbo`、`pro` |
| `custom` | 任何模擬器 | 你自己的命令範本 |

```bash
# 開放原始碼引擎只要二進位檔在這裡，就會自動選這台機器;
# 要確定就明確指定：
export PYAETHER_SIM_TARGET=local
./bin/pyaether sim run rc.cir --backend ngspice --json

# 其他任何工具，包括自研或廠商模擬器
export PYAETHER_SIM_CMD='Xyce -l {log} -r {raw}.raw {netlist}'
export PYAETHER_SIM_ENV='SPICE_ASCIIRAWFILE=1'   # 當工具預設輸出二進位時
./bin/pyaether sim run rc.cir --backend custom
```

`ok` 才是執行契約：`data` 非空絕不被當成成功的證明；失敗會被分類（網表讀取錯誤／
授權錯誤／收斂失敗／檔案不存在／行程崩潰）；而嚴格的存取器拒絕無中生有地編出數字。
詳見 [docs/SIMULATORS.md](docs/SIMULATORS.md)。

## 同一台機器、多個目標（profile）

同一份 checkout 不需要改任何東西就能連上多個安裝：profile 是使用者設定檔裡一組具名
的設定，而 `.pyaether-profile` 檔會把某個目錄綁定到其中一個。

```bash
# ~/.cache/pyaether-bridge/config.json
# {
#   "default_profile": "container",
#   "profiles": {
#     "container": {"transport": "docker", "container": "empyrean-gui"},
#     "lab":       {"transport": "ssh", "ssh_host": "aether@lab-server",
#                   "sim_backend": "alps", "sim_target": "ssh"}
#   }
# }

./bin/pyaether profile list          # 定義了什麼，以及哪個是使用中的
./bin/pyaether profile show          # 使用中的 profile 與所有解析後的設定
./bin/pyaether profile bind lab      # 把這個目錄綁定到 "lab"
./bin/pyaether profile clear         # 解除綁定
PYAETHER_PROFILE=lab ./bin/pyaether status   # 也可以只為單一指令指定
```

每個設定的解析順序都是**環境變數 > 作用中的 profile > 頂層設定 > 預設值**，而且每個
profile 有自己的 daemon socket（`<data dir>/profiles/<name>/daemon.sock`），所以兩個
profile 不會意外共用同一個 session。

**兩個 profile 之間不共用任何東西。** 每個 profile 也有自己的啟動鎖、pid 與日誌，
目標端的路徑同樣加上命名空間：除非你明確設定，否則 `remote_dir`、`sim_workdir`、
`layout_workdir`、`sch_workdir` 都會加上 `-<profile>` 後綴。不然兩個指向同一台主機的
profile 會互相覆蓋對方部署的 session 腳本與暫存的網表。沒有作用中的 profile 時，路徑
完全和以前一樣。

**與版本相關的產物要釘住，不要用猜的。** 離線符號目錄是從某一份安裝的文件建立的，
所以在 profile 裡宣告 `"aether_version": "2026.03"` 會把目錄綁到那個版本
（`<data dir>/profiles/<name>/catalog-<version>.sqlite`）。安裝了多份 Aether 時，
`api build` 同樣拒絕用猜的：請在 profile 裡釘住 `docs_dir`（或傳 `--docs`），不要讓
自動探索自己挑一個。寫在 profile 區塊裡的 `docs_dir` 會被採用 —— 以前它會被忽略。

**profile 可以斷言目標是什麼。** 設定 `expected_hostname`、`expected_container`、
`expected_image`、`expected_aether_version` 或 `expected_license_server`，然後：

```bash
./bin/pyaether profile verify     # 逐欄位 match / mismatch / unverifiable
```

對 live session 的第一次寫入在身分不符時會拒絕進行，而目標無法回報的*已宣告*值算失敗
而不是通過：無法驗證的斷言正是錯誤伺服器溜進來的方式。刻意跨目標時，設定
`PYAETHER_ALLOW_IDENTITY_MISMATCH=1`。

**可以拒絕繼承來的目標。** 設定 `PYAETHER_REQUIRE_EXPLICIT_PROFILE=1` 後，每個會對
目標動手的指令（`exec`、`sim run`、`layout gen|drc|boolean|convert|deck`、
`sch build|roundtrip`、`api sync-live`）都要求 profile 在*這個*行程裡被指名
（`PYAETHER_PROFILE=...`）。`.pyaether-profile` 綁定或 `default_profile` 只是「選出」
目標，對該指令而言不算「確認」，所以兩者都無法滿足這道守門。唯讀指令不受影響。

**過期的 daemon 不會服務錯的目標。** 每個 daemon 回覆都帶著它是為哪個目標啟動的
指紋。如果作用中 profile 的目標在該 daemon 持續執行期間改變，session 呼叫會以可據以
處理的訊息失敗，而不是默默對舊目標動手：

```bash
./bin/pyaether daemon restart     # 套用新的目標
```

慣例：人類可讀輸出為英文，`--json` 輸出原始 JSON。結束碼 `0` 表示成功、`1` 表示
執行期失敗（daemon 起不來、目錄資料庫不存在、`exec` 拋出例外等）、`2` 表示參數
錯誤。`exec` 會先原樣印出被執行內容的 stdout，再印最後一個運算式的值；錯誤訊息
走 stderr。

`--json` 只寫一行緊湊輸出，並省略執行診斷資訊（`metadata.target`、`work_dir`、`command`、
`timings` 等），那些欄位描述的是 bridge 怎麼跑，而不是它找到了什麼。呼叫沒有成功時
這些欄位會回來，而 `--debug` 一律印出它們（縮排顯示）。兩種情況下 envelope 與它的鍵
都相同，所以解析器看不出差別。

`sim run` 每個訊號回傳一個陣列，所以一萬個點的暫態就是一行四百 KB 的輸出。
`--summary` 把每條 trace 換成 `{n, first, last, min, max}`。長度不超過自己摘要的
trace 會保留取樣點，而 `metadata.plots` 與 `metadata.artifacts` 仍然指出圖表與原始檔
的名字，所以什麼都沒少 —— 只是換了位置。

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

提供五個工具：`pyaether_status`、`pyaether_api_search`、`pyaether_api_help`、
`pyaether_exec`、`pyaether_sim_run`。寫入真正的 `config.toml` 屬於使用者設定變更，
本專案不會代為修改。

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
| `PYAETHER_REMOTE_DIR` | session 腳本的部署目錄（預設 `/tmp/pyaether-bridge`，有作用中的 profile 時為 `/tmp/pyaether-bridge-<profile>`） |
| `PYAETHER_LICENSE_SERVER` | 覆寫目標的 `LM_LICENSE_FILE`；未設定則沿用目標自己的值 |
| `PYAETHER_BRIDGE_HOME` | daemon 資料目錄（預設 `~/.cache/pyaether-bridge`） |
| `PYAETHER_DAEMON_SOCK` | daemon 的 unix socket 路徑 |
| `PYAETHER_CATALOG_DB` | 目錄資料庫路徑（預設為專案內的 `data/catalog.sqlite`；設定 `aether_version` 時為 `profiles/<name>/catalog-<version>.sqlite`） |
| `PYAETHER_AETHER_VERSION` | 這個 profile 目標的 Aether 版本；把符號目錄綁到該版本 |
| `PYAETHER_DOCS_DIR` | `api build` 的文件目錄；可以逐 profile 設定 |
| `PYAETHER_BRIDGE_NO_AUTOSTART` | 設為 `1` 時禁止自動拉起 daemon |
| `PYAETHER_REQUIRE_EXPLICIT_PROFILE` | 設為 `1` 時，除非 `PYAETHER_PROFILE` 在這個行程裡指名 profile，否則拒絕會對目標動手的指令 |
| `PYAETHER_EXPECTED_HOSTNAME` / `_CONTAINER` / `_IMAGE` / `_AETHER_VERSION` / `_LICENSE_SERVER` | 斷言目標是什麼；由 `profile verify` 以及第一次寫入前檢查 |
| `PYAETHER_ALLOW_IDENTITY_MISMATCH` | 設為 `1` 時允許寫入身分檢查不通過的目標 |
| `PYAETHER_ALLOW_STALE_DAEMON` | 設為 `1` 時允許使用為其他目標啟動的執行中 daemon |
| `PYAETHER_SIM_TARGET` | 模擬器在哪裡執行：`docker`／`ssh`／`local`（預設同 bridge 的傳輸方式） |
| `PYAETHER_SIM_BACKEND` | 預設模擬器後端（預設 `ngspice`） |
| `PYAETHER_SIM_WORKDIR` | 目標上的模擬工作目錄（預設 `/tmp/pyaether-sim`，有作用中的 profile 時加 `-<profile>`） |
| `PYAETHER_SIM_TIMEOUT` | 預設模擬逾時秒數（預設 600） |
| `PYAETHER_SIM_SSH_HOST` / `_PORT` / `_OPTS` | 模擬器的 SSH 目標，當它與 bridge 目標不同時使用 |
| `PYAETHER_SIM_CONTAINER` | 模擬器容器，當它與 bridge 容器不同時使用 |
| `PYAETHER_SIM_CMD` | `custom` 後端的命令範本（`{netlist}` `{workdir}` `{log}` `{raw}` `{mode}`） |
| `PYAETHER_SIM_ENV` | 任何模擬器命令的額外環境變數，例如 `SPICE_ASCIIRAWFILE=1`（以逗號分隔的 `KEY=VALUE`） |
| `PYAETHER_NGSPICE_BIN` | ngspice 執行檔名稱或路徑（預設 `ngspice`） |
| `PYAETHER_SPECTRE_BIN` | Spectre 執行檔名稱或路徑（預設 `spectre`） |
| `PYAETHER_ALPS_BIN` | 華大九天 ALPS 執行檔（預設 `alps`；需要你自己有效的授權） |
| `PYAETHER_ALPS_THREADS` | 傳給 ALPS 的執行緒數，以 `-mt` 指定（選用） |
| `PYAETHER_PROFILE` | 這個指令的作用中 profile（覆寫綁定檔） |
| `PYAETHER_KLAYOUT_BIN` | KLayout 執行檔（預設 `klayout`） |
| `PYAETHER_KLAYOUT_TARGET` | KLayout 在哪裡執行：`docker`／`ssh`／`local`（預設：本機已安裝 KLayout 時就在本機） |
| `PYAETHER_KLAYOUT_BUDDY_DIR` | 存放 KLayout stream tools 的目錄（預設：由 KLayout 執行檔推斷） |
| `PYAETHER_LAYOUT_WORKDIR` | 目標上的版圖執行目錄根（預設 `/tmp/pyaether-layout`，有作用中的 profile 時加 `-<profile>`） |
| `PYAETHER_LAYOUT_TIMEOUT` | 預設版圖逾時秒數（預設 600） |
| `PYAETHER_SCH_WORKDIR` | 目標上的原理圖暫存目錄（預設 `/tmp/pyaether-sch`，有作用中的 profile 時加 `-<profile>`） |
| `PYAETHER_SCH_LIBDEFS` / `PYAETHER_SCH_AETHER_ROOT` | 目標的函式庫定義與 Aether 樹的位置 |

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
- **設計上會執行任意程式碼。** `pyaether exec` 與 MCP 的 `pyaether_exec` 工具會以
  你自己的權限在目標 session 中執行任意 Python —— 這正是本工具的用途。daemon 的
  unix socket 權限為 `0600`，只有你自己的使用者能連上；但請不要把這個 bridge
  交給不受信任的呼叫方。
- **版本與目錄佈局假設。** 目標必須提供小寫模組名 `pyAether`，且 `api build`
  預期文件位於 `tools/pyaether/docs/html`。開發與驗證使用 Aether 2026.03，
  其他版本未經測試。

## 測試

```bash
bash tests/smoke_cli.sh                  # CLI 冒煙測試，不需要 Docker/daemon
python3 tests/mcp_probe.py               # MCP 協定 + 全部工具
python3 tests/transport_probe.py         # 傳輸層：local + 假 ssh 樁
PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py   # 額外驗證真實容器
python3 tests/simulator_probe.py         # 真實 ngspice 執行 + 結果契約
python3 tests/profile_probe.py           # profile 解析與 daemon 隔離
python3 tests/layout_probe.py            # 對已知幾何實際跑 KLayout
python3 tests/dsh_probe.py               # DeepSeek Harness 安裝/組合循環
PYAETHER_ALPS_PROBE=1 python3 tests/alps_live_probe.py    # 廠商模擬器，需要授權
```

## 文件

- [docs/INSTALL.md](docs/INSTALL.md) — 目標環境準備、自我檢查、疑難排解
- [docs/SIMULATORS.md](docs/SIMULATORS.md) — 模擬器後端、結果契約、如何新增一個
- [docs/LAYOUT.md](docs/LAYOUT.md) — KLayout 操作、spec 與規則格式、授權邊界
- [docs/DEEPSEEK-HARNESS.md](docs/DEEPSEEK-HARNESS.md) — 把 bridge 裝成 DSH 外掛
- [ARCHITECTURE.md](ARCHITECTURE.md) — 模組劃分、內部介面、傳輸格式
- [README.ja.md](README.ja.md) — 日本語版
- [README.zh-TW.md](README.zh-TW.md) — 繁體中文版

## 授權

MIT，請見 [LICENSE](LICENSE)。
