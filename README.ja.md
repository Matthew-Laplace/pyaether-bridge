# pyaether-bridge

[English](README.md) | **日本語** | [繁體中文](README.zh-TW.md)

Empyrean Aether / PyAether をコマンドラインツールと MCP サーバーに変換します。
常駐 daemon がターゲット上で pyAether セッションを 1 つ維持し、オフラインの API
カタログが約 28,000 個のシンボルを検索可能にします。Python 3.9 標準ライブラリ
のみを使用し、サードパーティ依存はありません。

**PyAether の設置場所に応じて転送方式を選べます**（CLI と MCP サーバーは共通で、
環境変数 1 つで切り替わります）:

| PyAether の設置場所 | `PYAETHER_TRANSPORT` | 接続方法 |
| --- | --- | --- |
| Docker コンテナ内 | `docker`（既定） | `docker exec` |
| 社内・研究室のサーバー上 | `ssh` | `ssh`（既存の設定と鍵をそのまま使用） |
| bridge と同じ Linux マシン上 | `local` | ローカルプロセスとして実行 |

> **非公式プロジェクト。** Empyrean Technology との提携・承認・サポート関係は
> ありません。本リポジトリはベンダーのソフトウェア・バイナリ・ドキュメントを
> 一切含まず、配布もしません。**正規ライセンス**の Aether インストールと
> ライセンスが別途必要です。ターゲットの準備は
> [docs/INSTALL.md](docs/INSTALL.md) を参照してください。
>
> 華大九天（Empyrean Technology）が本プロジェクトについて権利侵害があると
> お考えの場合は、[Issue を作成](https://github.com/Matthew-Laplace/pyaether-bridge/issues)
> してご連絡ください。速やかに修正または削除いたします。

```bash
git clone https://github.com/Matthew-Laplace/pyaether-bridge.git
cd pyaether-bridge
./bin/pyaether version
```

## アーキテクチャ

```
ホスト側プロセス             ホスト側 daemon             ターゲット
----------------             ---------------             ----------
bin/pyaether  ┐                                         /tmp/pyaether-bridge/
bin/pyaether-mcp ├─ unix socket ─→ daemon ── transport ──→ session_bridge.py
MCP クライアント ┘  (NDJSON)      (単一書き手ロック)      常駐 pyAether セッション
                                               │         (pyAether の import は 1 回)
                      転送方式: docker exec / ssh / local bash
                                                                    │
オフライン API カタログ data/catalog.sqlite ←── api build ←── Sphinx docs/html ┘
```

- CLI と MCP サーバーは unix socket 経由で daemon とだけ通信し、Docker や SSH を
  直接触りません。
- daemon はリクエストをターゲット上の常駐 Python セッションへ転送します。この
  セッションは pyAether を**一度だけ** import し、以降のコードは同じ名前空間で
  実行されるため、変数は `exec` をまたいで保持されます。
- 3 つの転送方式はいずれも**ログインシェル**（`bash -lc`）で起動します。Aether の
  `AETHER_*`、`LD_LIBRARY_PATH`、`PYTHONPATH` はイメージまたはサーバーの
  profile が提供するためです。
- OpenAccess の書き込みは本質的に単一プロセスなので、daemon はすべてのリクエストを
  1 つのロックで直列化します。
- bridge は認証情報を読み取りも保存もしません。SSH 認証は `ssh` 自身に委ね
  （鍵 + ssh-agent を推奨）、`BatchMode` を強制するため、パスワード入力待ちで
  バックグラウンド呼び出しが止まることはなく、即座に失敗します。

## インストール

インストール作業は不要です。pip も使わず、システムディレクトリにも書き込みません。
クローン後、リポジトリ内の実行ファイルを直接呼び出してください（以下のコマンドは
すべてリポジトリのルートをカレントディレクトリと仮定しています）:

```bash
./bin/pyaether version
```

必要なもの:

- ホスト側の `python3`（3.9.6 で検証済み）。
- ターゲットに Aether（同梱の Python 3.9 と PyAether を含む）がインストールされ、
  ライセンスを取得できること。
- 選択した転送方式に応じた接続手段（Docker CLI、またはサーバーへの SSH アクセス）。

## PyAether への接続

**Docker（既定）**

```bash
./bin/pyaether status                      # transport=docker、コンテナ empyrean-gui
export PYAETHER_CONTAINER=my-aether        # 自分のコンテナ名に変更
```

**サーバーへ SSH**

```bash
export PYAETHER_TRANSPORT=ssh
export PYAETHER_SSH_HOST=aether@lab-server # ~/.ssh/config の別名も使用可
# 任意: PYAETHER_SSH_PORT=2222、PYAETHER_SSH_OPTS="-o StrictHostKeyChecking=no"
./bin/pyaether status
```

サーバー上の `PYAETHER_REMOTE_DIR`（既定は `/tmp/pyaether-bridge`）へ
`session_bridge.py` を配置し、そこで常駐セッションを起動します。ホスト側には
何もインストールする必要がありません。インタプリタ名が `python3.9` でない場合は
`PYAETHER_PYTHON` を設定してください。

**ローカル（Aether と同じマシンで bridge を動かす）**

```bash
export PYAETHER_TRANSPORT=local
./bin/pyaether exec -c 'import pyAether; print(pyAether.__file__)'
```

これらの設定は、シェルごとに export する代わりに
`~/.cache/pyaether-bridge/config.json` に書くこともできます
（[環境変数](#環境変数)を参照）。

## CLI の使い方

```bash
./bin/pyaether status                 # ターゲット + daemon + セッションの状態
./bin/pyaether status --json          # 生の JSON

./bin/pyaether daemon start           # ホスト側 daemon を起動（セッションは遅延起動）
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

./bin/pyaether sim backends --probe            # 利用できるシミュレータを表示
./bin/pyaether sim run rc.cir --backend ngspice  # オープンソースの SPICE
./bin/pyaether sim run tb.scs --backend spectre --mode ax

./bin/pyaether layout gen spec.json -o out.gds    # KLayout: レイアウトを生成
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout compare a.gds b.gds         # 終了コード 0 = 同一

./bin/pyaether dsh install                        # skill を <workspace>/.dsh/skills へ
./bin/pyaether version
```

## 推奨スタック

4 つの要素で 1 つのフローが完結します。ライセンスが必要なのはそのうち 1 つだけです。

| 要素 | 役割 |
| --- | --- |
| **DeepSeek Harness** | ループを回す。bridge へは CLI 経由で到達するため、MCP ツールスキーマを事前に読み込むセッションが発生しない |
| **`bin/pyaether`** | 唯一のインターフェース。API 検索、レイアウト、回路図、シミュレーションを 1 つの語彙で扱う |
| **ngspice** | オープンソースのテストベンチシミュレーション（`sim run --backend ngspice`） |
| **KLayout** | レイアウト形状（`layout gen / info / drc / boolean / convert / compare`） |

`api *`、`layout *`、`sim *`、`version` はターゲットを自前で解決し、daemon も Aether の
席も必要としません。`exec` と `sch snapshot|build|roundtrip` は必要とします。これらは
常駐セッション内で実行され、最初の `exec` がセッションを起動した時点から `PY_AETHER` の
席を 1 つ保持します。`sch netlist` は必要としません。これはキャンバス文書から SPICE
テキストへの純粋な関数です（実測: 抵抗 2 本の分圧回路で約 0.05 秒）。Aether の
データベースが必要なタスクではセッションを起動し、形状や SPICE のタスクでは起動しない
でください。

### トークンの消費

本リポジトリのコマンドでの実測値:

| コマンド | 既定 | フラグ使用時 |
| --- | --- | --- |
| `layout info --json` | 1885 B | 884 B |
| `status --json` | 2840 B | ≈1700 B（セッション状態により変動） |
| `sim run --json`、10008 点の過渡解析 | 389 231 B | 952 B（`--summary`） |

1. **サンプルではなく答えを要求する。** `--summary` は各波形を
   `{n, first, last, min, max}` に畳み込み、「収束したか、いくつになったか」という
   問いに 1/400 のサイズで答えます。`metadata.plots` と `metadata.artifacts` は
   プロット名と raw ファイル名を保持するため、サンプルはファイルを 1 回読むだけで
   取り出せます。
2. **既定は簡潔な出力にし、失敗時に `--debug` を使う。** 実行の診断情報
   （スクラッチディレクトリ、コマンドライン、シミュレータのバナー）は、呼び出しが
   成功したときは捨てられ、失敗したときは保持されます。読む価値があるのは後者です。
3. **シンボルを推測する前にカタログを検索する。** `api search` と `api show` は
   オフラインカタログを約 0.12 秒で読み、セッションを必要としません。でっち上げた
   `pyAether.*` 名は往復のコストがかかり、無操作に見える `nil` を返すことがあります。

### 往復回数の節約

1. **複合動詞を優先する。** `layout gen spec.json -o out.gds` はレイアウト全体を 1 回の
   呼び出しで構築します。形状を 1 つずつ作ると 1 つにつき 1 回の呼び出しになります。
2. **1 回の呼び出しで読み戻す。** `layout compare a.gds b.gds` はレイアウトが同一なら
   終了コード 0 を返し、`sch roundtrip spec.json` は構築・読み戻し・接続性の差分まで
   行います。どちらも 1 つの問いで書き込みを確定できます（`sch roundtrip` と、書き込みを
   行うすべての `layout` コマンドは、先にあなたの承認が必要です。bridge が代わりに
   確認することはありません）。
3. **文章ではなく `ok` と `status` を読む。** 契約は `SUCCESS` / `PARTIAL` / `FAILURE`
   です。本文が空でないことは成功の証明ではありません。
4. **長い処理はバックグラウンドに回す。** 大きな `layout gen`、`layout deck`、
   `sim run` はバックグラウンドモードが適切です。タイムアウトで実行結果を失わずに
   済みます。
5. **ターゲット側の処理はまとめる。** すべてのリクエストは daemon の単一ロックを
   通るため、長い `exec` は他のすべてをブロックします。承認された view ごとに
   まとまった 1 つのトランザクションを送ってください。インスタンス、配線、ラベルごとに
   1 回ずつ呼び出してはいけません。

## レイアウト（KLayout）

レイアウトの生成・読み込み・検査・変換は **KLayout** で行い、その作者がサポートする
ヘッドレス利用の方法で駆動します: `klayout -b -r <script>`（`-b` は `-zz -nc -rx` と
等価で、GUI なし、設定ファイルなし、暗黙のマクロなしで実行します）。Python パッケージは
インストールしません -- スクリプトは KLayout 自身のインタプリタ内で実行されます --
また KLayout は別プロセスとして起動し、import はしません。

```bash
./bin/pyaether layout probe                        # 利用可能か? どのバージョンか?
./bin/pyaether layout gen spec.json -o out.gds     # box、polygon、path、text、配列
./bin/pyaether layout info out.gds --layers '{"m1":[1,0]}'
./bin/pyaether layout drc out.gds --rules rules.json --layers '{"m1":[1,0]}'
./bin/pyaether layout boolean and --a 1/0 --b 2/0 --out-layer 4/0 --source out.gds -o and.gds
./bin/pyaether layout convert out.gds out.oas      # KLayout 付属のストリームツール
./bin/pyaether layout compare a.gds b.gds          # 終了コード 0 = 同一
./bin/pyaether layout deck rules.drc --source out.gds
```

3 つの挙動は意図的なものです。違反のある DRC 実行は終了コード `3` を返し（汚れた
レイアウトを CI に通してはいけません）、マージ後の形状から生じる接触エッジはスペース
違反として数えず、評価できないルールは違反ゼロを報告するのではなく実行を失敗させます。
[docs/LAYOUT.md](docs/LAYOUT.md) を参照してください。

## DeepSeek Harness

bridge は MCP サーバーなので、DeepSeek Harness は 2 つの半分から到達します:

- **登録**（`mcp__pyaether__*`）はローカルの **bundle**
  （`dsh-bundle-pyaether-bridge`）から提供され、harness 自身のプラグイン
  マネージャーでインストールします。bundle は独立したパッケージなので、profile を
  手作業で編集する必要がありません -- デスクトップアプリが管理する profile は CLI
  からの合成を拒否し、手書きのパッチをロールバックするため、これは重要です。
- **skill**（ツールの操作方法）はワークスペースへインストールします:

```bash
./bin/pyaether dsh install     # → <workspace>/.dsh/skills/pyaether-bridge/SKILL.md
./bin/pyaether dsh status      # 導入済みか? 同期しているか? その root を走査する profile はあるか?
./bin/pyaether dsh uninstall   # もう一度削除
```

skill はグローバルではなくワークスペース単位です。ファイルシステム skill プロバイダは
無効な状態で同梱され、明示的な `customSkillDirs` リストを走査するため、
`$DSH_HOME/skills` は決して読まれません。したがって `dsh status` は、profile がその
ルートを実際に走査しているかどうかを報告し、どの profile も走査していない場合は
`dsh install` が追加すべきブロックをそのまま出力します -- インストール済みでも走査
されていない skill は何も変えません。
[docs/DEEPSEEK-HARNESS.md](docs/DEEPSEEK-HARNESS.md) を参照してください。

なお本リポジトリの現状では、bridge は CLI 経由でも到達できます: セッションは
`bin/pyaether` を呼び出すだけです。MCP 経由の登録は
[後述](#mcp-サーバーの登録)のとおりです。

## シミュレータ

ネットリストは特定のシミュレータに縛られません。同じファイルをオープンソースの
ngspice で確認し、Cadence Spectre/APS でサインオフでき、どちらも同じ結果契約
（`ok` / `status` / `data` / `errors` / `metadata`）で返ってきます。シミュレータの
ターゲットは PyAether のターゲットとは**独立**に選択できるため、PyAether が
コンテナ内にあっても ngspice をノート PC で実行できます。

| バックエンド | 種別 | モード |
| --- | --- | --- |
| `ngspice` | オープンソース | ネットリストで宣言された解析 |
| `spectre` | 商用 | `spectre`、`aps`、`x`、`cx`、`ax`、`mx`、`lx`、`vx` |
| `alps` | 商用（Empyrean） | `basic`、`turbo`、`pro` |
| `custom` | 任意のシミュレータ | 独自のコマンドテンプレート |

```bash
# オープンソースのエンジンは、バイナリがここにあれば自動的にこのマシンを選びます;
# 確実にしたいなら明示的に指定してください:
export PYAETHER_SIM_TARGET=local
./bin/pyaether sim run rc.cir --backend ngspice --json

# 社内製やベンダ製のシミュレータを含む、その他のツール
export PYAETHER_SIM_CMD='Xyce -l {log} -r {raw}.raw {netlist}'
export PYAETHER_SIM_ENV='SPICE_ASCIIRAWFILE=1'   # ツールが既定でバイナリ出力する場合
./bin/pyaether sim run rc.cir --backend custom
```

`ok` は実行の契約です。空でない `data` を成功の証明として扱うことはなく、失敗は分類
され（ネットリスト読み込みエラー / ライセンスエラー / 収束失敗 / ファイル欠如 /
クラッシュ）、厳格なアクセサは数値をでっち上げることを拒否します。
[docs/SIMULATORS.md](docs/SIMULATORS.md) を参照してください。

## 1 台のマシンで複数のターゲット（profile）

同じチェックアウトから、何も編集せずに複数のインストール先を扱えます。profile は
ユーザー設定ファイル内の名前付き設定グループで、``.pyaether-profile`` ファイルが
ディレクトリをそのうちの 1 つに紐付けます。

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

./bin/pyaether profile list          # 何が定義され、どれが有効か
./bin/pyaether profile show          # 有効な profile と解決済みの全設定
./bin/pyaether profile bind lab      # このディレクトリを "lab" に結び付ける
./bin/pyaether profile clear         # 結び付きを解除
PYAETHER_PROFILE=lab ./bin/pyaether status   # 1 回のコマンドだけ指定することもできます
```

すべての設定の解決順序は**環境変数 > 有効な profile > トップレベル設定 > 既定値**で、
各 profile は専用の daemon ソケット
（`<data dir>/profiles/<name>/daemon.sock`）を持つため、2 つの profile が誤って
セッションを共有することはありません。

**2 つの profile の間で共有されるものはありません。** それぞれが専用の起動ロック、
pid、ログを持ち、ターゲット側のパスも名前空間が分かれます。`remote_dir`、
`sim_workdir`、`layout_workdir`、`sch_workdir` は、明示的に設定しない限り
`-<profile>` 接尾辞が付きます。そうしなければ、同じホストを指す 2 つの profile が、
配置したセッションスクリプトとステージングしたネットリストを上書きし合います。
profile が有効でない場合、パスは従来とまったく同じです。

**バージョン依存の成果物は推測せず固定します。** オフラインのシンボルカタログは
1 つのインストールのドキュメントから構築されるため、profile に
`"aether_version": "2026.03"` を宣言すると、カタログがそのリリースに紐付きます
（`<data dir>/profiles/<name>/catalog-<version>.sqlite`）。同様に `api build` は、
複数の Aether ツリーがインストールされている場合に推測することを拒否します。
探索に任せるのではなく、profile で `docs_dir` を固定する（または `--docs` を渡す）
ようにしてください。profile ブロック内に書いた `docs_dir` が有効になります -- 以前は
無視されていました。

**profile はターゲットが何であるかを表明できます。** `expected_hostname`、
`expected_container`、`expected_image`、`expected_aether_version`、
`expected_license_server` を設定し、次を実行します:

```bash
./bin/pyaether profile verify     # 項目ごとに match / mismatch / unverifiable
```

稼働中のセッションへの最初の書き込みは、不一致の場合は続行を拒否します。また、
ターゲットが報告できない*宣言済み*の値は、合格ではなく失敗として数えます。
検証できない主張こそが、誤ったサーバーをすり抜けさせる手口だからです。意図的に
別のターゲットへ渡る場合は `PYAETHER_ALLOW_IDENTITY_MISMATCH=1` を設定してください。

**継承されたターゲットは拒否できます。** `PYAETHER_REQUIRE_EXPLICIT_PROFILE=1` を
設定すると、ターゲットを操作するすべてのコマンド（`exec`、`sim run`、
`layout gen|drc|boolean|convert|deck`、`sch build|roundtrip`、`api sync-live`）は、
*この*プロセスで profile が指定されている（`PYAETHER_PROFILE=...`）ことを要求します。
`.pyaether-profile` のバインディングや `default_profile` はターゲットを選択しますが、
そのコマンドにとって確認にはならないため、どちらもガードを満たしません。読み取り専用
コマンドは影響を受けません。

**古い daemon が誤ったターゲットに応答することはありません。** daemon のすべての
応答は、それが起動されたターゲットのフィンガープリントを含みます。有効な profile の
ターゲットが変わったのにその daemon が動き続けている場合、セッション呼び出しは
古いターゲットで黙って動作するのではなく、対処方法を示すメッセージとともに失敗します:

```bash
./bin/pyaether daemon restart     # 新しいターゲットを反映
```

規約: 人間が読む出力は英語、`--json` は生の JSON を出力します。終了コードは成功が
`0`、実行時エラー（daemon が起動しない、カタログが無い、`exec` が例外を送出した等）
が `1`、引数エラーが `2` です。`exec` は実行したコードの stdout を先に出力し、続けて
最後の式の値を出力します。エラーは stderr に出力されます。

`--json` は 1 行のコンパクトな出力を書き、bridge が何を見つけたかではなく bridge が
どのように動作したかを示す実行の診断情報（`metadata.target`、`work_dir`、`command`、
`timings` など）を省きます。これらのフィールドは呼び出しが成功しなかった場合には
戻ってきて、`--debug` は常にインデント付きで出力します。どちらの場合もエンベロープと
そのキーは同じなので、パーサーから見た違いはありません。

`sim run` は信号ごとに 1 つの配列を返すため、1 万点の過渡解析は 400 キロバイトの
1 行になります。`--summary` は各トレースを `{n, first, last, min, max}` に置き換えます。
要約より短いトレースはサンプルを保持し、`metadata.plots` と `metadata.artifacts` は
プロット名と raw ファイル名を保持するため、失われるものは何もありません -- 場所が
変わるだけです。

`PYAETHER_BRIDGE_NO_AUTOSTART=1` を設定すると、`status` は現在の状態を報告するだけで
何も起動しません。
## MCP サーバーの登録

`bin/pyaether-mcp` は stdio 型の MCP サーバー（NDJSON JSON-RPC）で、Codex から
直接起動できます:

```toml
# ~/.codex/config.toml — 自分の設定を変更すると決めてから追記してください
[mcp_servers.pyaether]
command = "/usr/bin/python3"
args = ["/absolute/path/to/pyaether-bridge/bin/pyaether-mcp"]
```

公開するツール: `pyaether_status`、`pyaether_api_search`、`pyaether_api_help`、
`pyaether_exec`、`pyaether_sim_run`。実際の `config.toml` への書き込みはユーザー設定の
変更にあたるため、本リポジトリは代行しません。

## オフライン API カタログ

カタログは、**あなた自身の** Aether インストールに同梱されている Sphinx
ドキュメント（`objects.inv` と生成済み API ページ）から構築します。ドキュメントは
`$AETHER_ROOT/tools/pyaether/docs/html` にあり、`api build` は
`$PYAETHER_DOCS_DIR`、ユーザー設定の `docs_dir`、`$AETHER_ROOT`、一般的な
インストール先（`/opt/empyrean/*`、`~/empyrean/*`）の順に探します。

Aether がコンテナ内にある場合は、先にドキュメントを取り出します（`data/` は
git 管理外です）:

```bash
CTR=empyrean-gui
DOCS=$(docker exec "$CTR" bash -lc 'ls -d "$AETHER_ROOT"/tools/pyaether/docs/html')
docker cp "$CTR:$DOCS" ./data/docs

# SSH の場合も同様
DOCS=$(ssh aether@lab-server 'echo "$AETHER_ROOT"/tools/pyaether/docs/html')
scp -r "aether@lab-server:$DOCS" ./data/docs

./bin/pyaether api build --docs ./data/docs --jobs 4   # data/catalog.sqlite を書き出す
./bin/pyaether api sync-live                           # 実行時のみのシンボルを追加（daemon が必要）
```

1 つのデータベースに 3 種類の行が入ります。`--kind` で絞り込めます:

| kind | 行数（2026.03 での実測） | 由来 |
| --- | --- | --- |
| `function` / `method` / `class` / `attribute` / `module` | 10376 | インストーラ同梱の Sphinx `api_reference` |
| `runtime` | 12582 | **ドキュメントが扱っていない** `dir(pyAether)` のシンボル |
| `doc` / `label` | 5274 | ドキュメントのセクションラベル（常に API シンボルより下位に順位付け） |

合計 28232 行。**ドキュメントは実行時の全シンボルを網羅していません**（例えば
`emyInitDb` は実行時にしか存在しません）。そのため `api build` の後に
`api sync-live` を実行してください。`sync-live` は繰り返し実行可能で、前回の
runtime 行を消してから再計算するため、ドキュメントが既に記述しているシンボルを
重複させません。検索では常に、型付きシグネチャを持つドキュメント側の項目が優先されます。

> **`data/catalog.sqlite` をコミットしたり再配布したりしないでください。** これは
> ベンダーのドキュメントから抽出したインターフェースのメタデータ（説明文を含む）で
> あり、ライセンスを受けたインストールに対して、ローカルでのみ生成・使用すべきものです。
> `.gitignore` で `data/` を除外しています。この設定は維持してください。

## 環境変数

| 変数 | 用途 |
| --- | --- |
| `PYAETHER_TRANSPORT` | `docker`（既定）/ `ssh` / `local` |
| `PYAETHER_CONTAINER` | Docker コンテナ名（既定 `empyrean-gui`） |
| `PYAETHER_SSH_HOST` | SSH ターゲット。例 `user@server`、`~/.ssh/config` の別名（`ssh` では必須） |
| `PYAETHER_SSH_PORT` | SSH ポート（任意） |
| `PYAETHER_SSH_OPTS` | 追加の ssh オプション。例 `-o StrictHostKeyChecking=no`（任意） |
| `PYAETHER_PYTHON` | ターゲット上のインタプリタ（既定 `python3.9`、ログインシェル経由で解決） |
| `PYAETHER_REMOTE_DIR` | セッションスクリプトの配置先（既定 `/tmp/pyaether-bridge`、profile 有効時は `/tmp/pyaether-bridge-<profile>`） |
| `PYAETHER_LICENSE_SERVER` | ターゲットの `LM_LICENSE_FILE` を上書き。未設定ならターゲットの値を使用 |
| `PYAETHER_BRIDGE_HOME` | daemon のデータディレクトリ（既定 `~/.cache/pyaether-bridge`） |
| `PYAETHER_DAEMON_SOCK` | daemon の unix socket パス |
| `PYAETHER_CATALOG_DB` | カタログ sqlite のパス（既定はリポジトリ内 `data/catalog.sqlite`、`aether_version` 設定時は `profiles/<name>/catalog-<version>.sqlite`） |
| `PYAETHER_AETHER_VERSION` | この profile が対象とする Aether リリース。シンボルカタログをそのリリースに紐付ける |
| `PYAETHER_DOCS_DIR` | `api build` のドキュメントディレクトリ。profile ごとに設定可能 |
| `PYAETHER_BRIDGE_NO_AUTOSTART` | `1` にすると daemon の自動起動を禁止 |
| `PYAETHER_REQUIRE_EXPLICIT_PROFILE` | `1` にすると、`PYAETHER_PROFILE` がこのプロセスで profile を指定していない限り、ターゲットを操作するコマンドを拒否 |
| `PYAETHER_EXPECTED_HOSTNAME` / `_CONTAINER` / `_IMAGE` / `_AETHER_VERSION` / `_LICENSE_SERVER` | ターゲットが何であると主張するか。`profile verify` と最初の書き込み前に検査 |
| `PYAETHER_ALLOW_IDENTITY_MISMATCH` | `1` にすると、識別検査に失敗したターゲットへの書き込みを許可 |
| `PYAETHER_ALLOW_STALE_DAEMON` | `1` にすると、別のターゲット向けに起動された稼働中の daemon を使用 |
| `PYAETHER_SIM_TARGET` | シミュレータの実行場所: `docker` / `ssh` / `local`（既定は bridge の転送方式） |
| `PYAETHER_SIM_BACKEND` | 既定のシミュレータバックエンド（既定 `ngspice`） |
| `PYAETHER_SIM_WORKDIR` | ターゲット上のシミュレータ作業ディレクトリ（既定 `/tmp/pyaether-sim`、profile 有効時は `-<profile>` 付き） |
| `PYAETHER_SIM_TIMEOUT` | 既定のシミュレーションタイムアウト秒（既定 600） |
| `PYAETHER_SIM_SSH_HOST` / `_PORT` / `_OPTS` | シミュレータの SSH ターゲット（bridge のターゲットと異なる場合） |
| `PYAETHER_SIM_CONTAINER` | シミュレータのコンテナ（bridge のコンテナと異なる場合） |
| `PYAETHER_SIM_CMD` | `custom` バックエンド用のコマンドテンプレート（`{netlist}` `{workdir}` `{log}` `{raw}` `{mode}`） |
| `PYAETHER_SIM_ENV` | 任意のシミュレータコマンドに追加する環境変数。例 `SPICE_ASCIIRAWFILE=1`（`KEY=VALUE` をカンマ区切り） |
| `PYAETHER_NGSPICE_BIN` | ngspice のバイナリ名またはパス（既定 `ngspice`） |
| `PYAETHER_SPECTRE_BIN` | Spectre のバイナリ名またはパス（既定 `spectre`） |
| `PYAETHER_ALPS_BIN` | Empyrean ALPS のバイナリ（既定 `alps`。有効なライセンスが別途必要） |
| `PYAETHER_ALPS_THREADS` | ALPS に `-mt` として渡すスレッド数（任意） |
| `PYAETHER_PROFILE` | このコマンドで有効な profile（バインディングファイルを上書き） |
| `PYAETHER_KLAYOUT_BIN` | KLayout の実行ファイル（既定 `klayout`） |
| `PYAETHER_KLAYOUT_TARGET` | KLayout の実行場所: `docker` / `ssh` / `local`（既定は KLayout が本機にインストールされていれば本機） |
| `PYAETHER_KLAYOUT_BUDDY_DIR` | KLayout の stream ツールを格納するディレクトリ（既定は KLayout バイナリから推定） |
| `PYAETHER_LAYOUT_WORKDIR` | ターゲット上のレイアウト実行ディレクトリのルート（既定 `/tmp/pyaether-layout`、profile 有効時は `-<profile>` 付き） |
| `PYAETHER_LAYOUT_TIMEOUT` | レイアウトの既定タイムアウト秒（既定 600） |
| `PYAETHER_SCH_WORKDIR` | ターゲット上の回路図ステージングディレクトリ（既定 `/tmp/pyaether-sch`、profile 有効時は `-<profile>` 付き） |
| `PYAETHER_SCH_LIBDEFS` / `PYAETHER_SCH_AETHER_ROOT` | ターゲットのライブラリ定義と Aether ツリーを探す場所 |

マシン固有の設定は `$PYAETHER_BRIDGE_HOME/config.json`（既定
`~/.cache/pyaether-bridge/config.json`）にも書けます。ローカルパスをリポジトリに
入れずに済みます:

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

## 既知の制限

- **第三者の権利について。** 本プロジェクトは非公式であり、ベンダーのソフトウェアや
  ドキュメントを同梱していません。API カタログは、あなた自身がライセンスを受けた
  インストールからローカルで生成するものです。華大九天（Empyrean Technology）が
  本リポジトリの内容について権利侵害があるとお考えの場合は
  [Issue を作成](https://github.com/Matthew-Laplace/pyaether-bridge/issues)
  してください。速やかに修正または削除いたします。
- **ライセンスは 1 席。** 常駐セッションは動作中ずっと `PY_AETHER` の席を保持します。
  別の PyAether プロセスを同時に起動すると、ライセンスを取得できない場合があります。
- **OA アクセスは直列化。** すべてのリクエストは daemon 内の単一ロックを通るため、
  長時間かかる `exec` は完了またはタイムアウトするまで他のすべての呼び出しを
  ブロックします。デバッグやバッチ処理には適しますが、高並行のサービスには
  向きません。
- **ターゲットの再起動。** コンテナの再作成やサーバーの再起動後は、保存された
  セッションが失効します。次のリクエストで自動的に再構築され（pyAether の再 import、
  数秒）、以前の名前空間の変数は失われます。
- **SSH 認証。** 既存の SSH 設定 / ssh-agent のみを使用します。bridge は
  `BatchMode` を強制するため、対話的なパスワード入力が必要な呼び出しはハングせず
  失敗します。事前に `ssh <host>` がパスワードなしで通ることを確認してください。
- **`api build` のドキュメント入手元。** 既にお持ちのインストールに含まれる
  `tools/pyaether/docs/html`（またはコンテナから取り出したコピー）が必要です。
  見つからない場合は黙って失敗せず、明示的にエラーを出します。
- **カタログは読み取り専用の成果物。** sqlite ファイルを書き換えるのは `api build`
  だけで、他のコマンドは読み取り専用です。
- **ターゲット側の Python のみ。** bridge は Aether 同梱の Python 3.9 と
  OpenAccess 共有ライブラリに依存します。ホスト側のコードは EDA 計算を一切行わず、
  リクエストの転送とカタログ検索だけを行います。
- **`ssh` 転送はスタブでしか検証していません。** docker と local は実機で
  エンドツーエンド検証済みで、ssh は遠隔シェルの意味論を忠実に再現したスタブで
  検証していますが、実際の遠隔サーバーに対しては未検証です。最初の実運用は
  テストとして扱ってください。
- **POSIX ホスト限定。** daemon は unix ドメインソケットで通信するため、
  CLI / MCP サーバーを動かすホストは macOS か Linux である必要があります。
  ネイティブ Windows は未対応です。
- **実行時のみのシンボルはシグネチャが弱い。** `runtime` 種別の項目は SWIG
  オブジェクトに対する `inspect` の結果で、`(*args, **kwargs)` のように表示される
  ことがよくあります。型の揃ったシグネチャはドキュメント項目側にあり、検索は
  そちらを優先します。
- **設計上、任意のコードを実行します。** `pyaether exec` と MCP の
  `pyaether_exec` ツールは、ターゲット側セッションであなたの権限のまま任意の
  Python を実行します（それが本ツールの目的です）。daemon の unix ソケットは
  モード `0600` で同じユーザーしか接続できませんが、信頼できない相手にこの
  bridge を渡さないでください。
- **バージョンとレイアウトの前提。** ターゲットには小文字のモジュール名
  `pyAether` が必要で、`api build` は `tools/pyaether/docs/html` にドキュメントが
  あることを前提にします。開発と検証は Aether 2026.03 で行っており、他の
  リリースは未検証です。

## テスト

```bash
bash tests/smoke_cli.sh                  # Docker/daemon 不要の CLI スモークテスト
python3 tests/mcp_probe.py               # MCP プロトコル + 全ツール
python3 tests/transport_probe.py         # 転送層: local + 偽 ssh スタブ
PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py   # 実コンテナも検証
python3 tests/simulator_probe.py         # 実際の ngspice 実行 + 結果の契約
python3 tests/profile_probe.py           # profile 解決と daemon 分離
python3 tests/layout_probe.py            # 既知の図形に対する実際の KLayout 実行
python3 tests/dsh_probe.py               # DeepSeek Harness の導入/合成サイクル
PYAETHER_ALPS_PROBE=1 python3 tests/alps_live_probe.py    # ベンダ製シミュレータ、ライセンスが必要
```

## ドキュメント

- [docs/INSTALL.md](docs/INSTALL.md) — ターゲットの準備、セルフチェック、トラブルシューティング
- [docs/SIMULATORS.md](docs/SIMULATORS.md) — シミュレータバックエンド、結果契約、新しいバックエンドの追加
- [docs/LAYOUT.md](docs/LAYOUT.md) — KLayout の操作、spec とルールの形式、ライセンスの境界
- [docs/DEEPSEEK-HARNESS.md](docs/DEEPSEEK-HARNESS.md) — bridge を DSH プラグインとしてインストールする
- [ARCHITECTURE.md](ARCHITECTURE.md) — モジュール構成、内部インターフェース、通信フォーマット
- [README.md](README.md) — English version
- [README.zh-TW.md](README.zh-TW.md) — 繁體中文版

## ライセンス

MIT — [LICENSE](LICENSE) を参照してください。
