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

## Copilot のセットアップ

Docker セットアップ後に Copilot を使う場合は、以下を実行します。

```bash
copilot
```

起動後に `/login` を入力してログインします。  
ブラウザで信頼/許可のボタンを押し、認証完了後に閉じてください。

## 個別実行（checkout -> prompt -> fix -> test）

実行順は次の 4 ステップです。

```bash
# 1) checkout
python pipeline.py checkout --in experiment.csv --out-dir Normal

# 2) prompt 生成（fix 前に必須）
python pipeline.py prompts --dir Normal --mode normal

# 3) fix
python pipeline.py fix --dir Normal --model gpt-5-mini

# 4) test
python pipeline.py test --dir Normal
```

主な生成・更新ファイル:

- `Normal/results.csv`
- `Normal/bug-location.md`
- 各対象 Java ファイルの隣に `*_prompt.md`
- `Normal/fix_results.txt`
- `python pipeline.py test` 実行後、`Normal/results.csv` の `test_status` / `error_count` / `failing_tests` が更新
- `prompts` / `fix` 実行後、`Normal/results.csv` にバグごとの所要時間（秒）が記録される（下記）

### results.csv の所要時間カラム

| カラム | 内容 |
| --- | --- |
| `prompt_time_sec` | prompt 1 件の生成時間。バグ情報（`defects4j info` / JIRA）の取得から `*_prompt.md` の書き出しまで。修正対象ファイルの特定（`defects4j export`）は含まない |
| `fix_time_sec` | Copilot セッション 1 回の時間（起動〜応答〜終了）。タイムアウト・エラー時も記録 |
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
- `defaults.model`: fix で使うモデル名
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
