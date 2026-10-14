# Defects4J Workspace Scripts

Defects4J の checkout / prompt 生成 / fix / test を `pipeline.py` で実行するための手順です。

## 最初にやること（Dockerのセットアップ）

リポジトリルートで以下を実行します。

```bash
docker compose up -d
```

コンテナに入ります（推奨）。

```bash
docker compose exec -it defects4j bash
```

代替:

```bash
docker exec -it defects4j-container bash
```

以降のコマンドは、コンテナ内 `/workspace` で実行してください。  
終了時は必要に応じて以下を実行します。

```bash
docker compose down
```

## fix プロバイダ（Claude / Codex）のセットアップ

fix ステップは Claude（Claude Agent SDK）と Codex（OpenAI Codex SDK）のどちらかで実行します。
どちらを使うかは環境変数 `FIX_PROVIDER` で切り替え、認証はどちらも API キーで行います。

リポジトリルートで `.env.example` をコピーして `.env` を作り、値を設定します（`.env` は git 管理外）。

```bash
cp .env.example .env
```

| 変数 | 内容 |
| --- | --- |
| `FIX_PROVIDER` | `claude` または `codex`。未設定・不正な値の場合、fix はエラーで止まります |
| `ANTHROPIC_API_KEY` | `FIX_PROVIDER=claude` のときに必要 |
| `OPENAI_API_KEY` | `FIX_PROVIDER=codex` のときに必要 |

`.env` は `docker-compose.yml` の `env_file` でコンテナに渡されます。`.env` を変更したら `docker compose up -d` でコンテナを作り直してください。

モデル名は `--model`（plan モードでは `model`）で指定します。既定値はないため、指定しないとエラーになります。
プロバイダに合ったモデル名を指定してください（例: Claude なら `claude-sonnet-5-5`、Codex なら `gpt-5-mini`）。

### プロバイダごとのアクセス範囲

どちらも作業ディレクトリ（cwd）は修正対象 Java ファイルのあるディレクトリです。

| | Claude | Codex |
| --- | --- | --- |
| 使えるツール | Read / Edit / Write / Glob / Grep（シェルなし） | シェル（ファイルの読み取りもシェル経由）、ファイル編集 |
| cwd 外へのアクセス | フックで拒否 | 拒否できない（Docker 内では OS サンドボックスが動かないため `full_access` で実行）。実行ログから検出して警告 |
| Web 検索 | なし | 無効化 |

Codex の実行ログは `<out_dir>/codex_logs/<project>_<id>.jsonl` に保存されます。
cwd 外（例: 正解パッチがある `/defects4j/framework/projects/*/patches/` や他の out_dir）へのアクセスが検出されたバグは、
`fix_results.txt` に `[警告] cwd 外へのアクセスを検出しました` として対象パスが記録され、SUMMARY の `Outside cwd access (warning)` に件数が出ます。
検出はコマンド文字列とファイル変更のパスから行うため、誤検出・見逃しがあり得ます。

## 個別実行（checkout -> prompt -> fix -> test）

実行順は次の 4 ステップです。

```bash
# 1) checkout
python pipeline.py checkout --in experiment.csv --out-dir Normal

# 2) prompt 生成（fix 前に必須）
python pipeline.py prompts --dir Normal --mode normal

# 3) fix
python pipeline.py fix --dir Normal --model claude-sonnet-5-5

# 4) test
python pipeline.py test --dir Normal
```

主な生成・更新ファイル:

- `Normal/results.csv`
- `Normal/bug-location.md`
- 各対象 Java ファイルの隣に `*_prompt.md`
- `Normal/fix_results.txt`
- `Normal/codex_logs/*.jsonl`（`FIX_PROVIDER=codex` のときのみ）
- `python pipeline.py test` 実行後、`Normal/results.csv` の `test_status` / `error_count` / `failing_tests` が更新
- `prompts` / `fix` 実行後、`Normal/results.csv` にバグごとの所要時間（秒）が記録される（下記）

### results.csv の所要時間カラム

| カラム | 内容 |
| --- | --- |
| `prompt_time_sec` | prompt 1 件の生成時間。バグ情報（`defects4j info` / JIRA）の取得から `*_prompt.md` の書き出しまで。修正対象ファイルの特定（`defects4j export`）は含まない |
| `fix_time_sec` | fix セッション 1 回の時間（起動〜応答〜終了）。タイムアウト・エラー時も記録 |
| `prompt_fix_time_sec` | `prompt_time_sec + fix_time_sec`（両方ある場合のみ） |

- 各ステップの開始時に該当カラムを空にし、1 件終わるごとに書き込みます（途中で止めても完了分は残ります）。
- prompt の生成に失敗・スキップした行は空欄になります。
- `prompts_copy`（plan の `prompts.type: copy`）では、コピー時間ではなくコピー元 `results.csv` の `prompt_time_sec` を引き継ぎます。
- 同じ JIRA URL を持つバグが複数ある場合、2 件目以降は取得結果をキャッシュから使うため短くなります。

## 一括実行（plan モード: run --plan）

複数の出力ディレクトリ（variant）をまとめて実行する場合は `run --plan` を使います。

```bash
python pipeline.py run --plan plan.yml
```

特定 variant のみ実行:

```bash
python pipeline.py run --plan plan.yml --only normal-gpt4.1,cot_with_ast-gpt4.1
```

### `plan.yml` の使い方（`workspace/plan.yml` を例に）

- `in_csv`: 入力 CSV（例: `experiment.csv`）
- `defaults.steps`: 実行順（通常は `[checkout, prompts, fix, test]`）
- `defaults.model`: fix で使うモデル名（`FIX_PROVIDER` に合ったもの。未指定だとエラー）
- `variants[].out_dir`: 実行対象ディレクトリ
- `variants[].prompts.type`:
  - `generate`: prompt を新規生成
  - `copy`: 既存 prompt をコピー

現行の `workspace/plan.yml` は `prompts.type: copy` が中心です。  
そのため、`from_dir` 側に対応する prompt ファイルが事前に存在している必要があります。

各 variant のログは `out_dir/run_all_results.txt` に保存されます。

## experiment.csv のカラム仕様

`experiment.csv` の必須カラムは次の 2 つです。

- `project`
- `id`

最小例:

```csv
project,id
Lang,1
Math,7
```

ルール:

- ヘッダは `project,id` を使用
- `checkout` で参照されるのはこの 2 カラム
- 空の `project` / `id` 行はスキップされる

## 補足コマンド

### prompts_copy

用途: 既存 prompt を別 checkout ディレクトリへコピーして再利用します。

```bash
python pipeline.py prompts_copy --from-dir Normal --to-dir CoT-with-AST
```

前提:

- `--to-dir/results.csv` の `project,id` を基準に処理
- `--from-dir` 側に対応する `*_prompt.md` が存在すること

## トラブルシューティング

### `defects4j` が見つからない

```bash
which defects4j
echo $PATH
```

### `results.csv` が無いと言われる

`python pipeline.py checkout ...` が完了しているか確認してください。  
`test` / `fix` / `prompts` は `out_dir/results.csv` を前提に動作します。

## prompt テンプレートの編集とモード追加

### prompt テンプレートの編集場所

prompt テンプレートは `workspace/defects4j_tools/prompts/` 配下にあります。

- `workspace/defects4j_tools/prompts/normal.txt`
- `workspace/defects4j_tools/prompts/ast.txt`
- `workspace/defects4j_tools/prompts/cfg_pdg.txt`

既存モードの文面を調整したい場合は、上記 `.txt` を直接編集してください。

### prompt 生成モードを増やす方法

新しいモード（例: `MY_MODE`）を追加する場合は、次を実施します。

1. `workspace/defects4j_tools/prompts/` に新しいテンプレートファイル（例: `my_mode.txt`）を追加
2. `workspace/defects4j_tools/generate_prompts.py` の `PROMPT_FILES` にモード名とファイル名の対応を追加
3. `workspace/defects4j_tools/generate_prompts.py` の `parse_args()` の `choices` に新モードを追加
4. `workspace/pipeline.py` の `prompts` サブコマンド引数 `--mode` の `choices` に新モードを追加
5. `workspace/pipeline.py` 内の plan 検証（`prompts.mode` の許可値チェック）にも新モードを追加

これで次のように実行できます。

```bash
python pipeline.py prompts --dir Normal --mode MY_MODE
```
