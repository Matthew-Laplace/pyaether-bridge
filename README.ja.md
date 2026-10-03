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
./bin/pyaether version
```

仕様: 人間が読む出力は英語、`--json` は生の JSON を出力します。終了コードは成功が
`0`、実行時エラー（daemon が起動しない、カタログが無い、`exec` が例外を送出した等）
が `1`、引数エラーが `2` です。`exec` は実行したコードの stdout をそのまま出力し、
続けて最後の式の値を出力します。エラーは stderr に出力されます。

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
`pyaether_exec`。実際の `config.toml` への書き込みはユーザー設定の変更にあたるため、
本リポジトリは代行しません。

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
| `PYAETHER_REMOTE_DIR` | セッションスクリプトの配置先（既定 `/tmp/pyaether-bridge`） |
| `PYAETHER_LICENSE_SERVER` | ターゲットの `LM_LICENSE_FILE` を上書き。未設定ならターゲットの値を使用 |
| `PYAETHER_BRIDGE_HOME` | daemon のデータディレクトリ（既定 `~/.cache/pyaether-bridge`） |
| `PYAETHER_DAEMON_SOCK` | daemon の unix socket パス |
| `PYAETHER_CATALOG_DB` | カタログ sqlite のパス（既定はリポジトリ内 `data/catalog.sqlite`） |
| `PYAETHER_DOCS_DIR` | `api build` の既定ドキュメントディレクトリ |
| `PYAETHER_BRIDGE_NO_AUTOSTART` | `1` にすると daemon の自動起動を禁止 |

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

- **ライセンスは 1 席。** 常駐セッションは動作中ずっと `PY_AETHER` の席を保持します。
  別の PyAether プロセスを同時に起動すると、ライセンスを取得できない場合があります。
- **OA アクセスは直列化。** すべてのリクエストは daemon 内の単一ロックを通ります。
  デバッグやバッチ処理には適しますが、高並行のサービスには向きません。
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

## テスト

```bash
bash tests/smoke_cli.sh                  # Docker/daemon 不要の CLI スモークテスト
python3 tests/mcp_probe.py               # MCP プロトコル + 4 ツール、14 項目
python3 tests/transport_probe.py         # 転送層: local + 偽 ssh スタブ
PYAETHER_TEST_DOCKER=1 python3 tests/transport_probe.py   # 実コンテナも検証
```

## ドキュメント

- [docs/INSTALL.md](docs/INSTALL.md) — ターゲットの準備、セルフチェック、トラブルシューティング
- [ARCHITECTURE.md](ARCHITECTURE.md) — モジュール構成、内部インターフェース、通信フォーマット
- [README.md](README.md) — English version
- [README.zh-TW.md](README.zh-TW.md) — 繁體中文版

## ライセンス

MIT — [LICENSE](LICENSE) を参照してください。
