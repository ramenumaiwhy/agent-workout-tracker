# agent-workout-tracker

AIエージェントへ報告した筋トレのセット実績を、SQLiteへ1回だけ保存するローカルCLIです。同じ記録から、AIエージェント用のトレーニング文脈と人向けの重量グラフを作ります。

MVPの入力元はAIエージェントです。Siri、音声認識、入力用Web UI、Remote MCPは含みません。

## 必要な環境

- Python 3.11以上
- [uv](https://docs.astral.sh/uv/)（以下の導入手順で使用）

## インストール

```sh
uv tool install git+https://github.com/ramenumaiwhy/agent-workout-tracker
workout --help
```

`workout` が見つからない場合は、`uv tool update-shell` を実行してシェルを開き直してください。

同じ版をインストールするには、Git URLの末尾に `@<コミットSHA>` を付けます。ローカルの変更を試すには、リポジトリのルートで `uv run --locked workout --help` を実行します。

## 試しに記録する

次の例はmacOSのシェルで実行できます。サンプルのセット実績は一時ディレクトリへ保存します。

```sh
export WORKOUT_DB_PATH="$(mktemp -d)/workout.sqlite3"
```

種目を登録します。

```sh
printf '%s\n' '{"type":"register_exercise","name":"ベンチプレス","aliases":["ベンチ"],"source_text":"ベンチプレスとして登録することに同意"}' \
  | workout record --request-id "$(uuidgen)"
```

種目登録後にセット実績を記録します。

```sh
printf '%s\n' '{"type":"add_set","exercise":"ベンチプレス","weight_kg":60,"reps":10,"source_text":"ベンチプレス60キロ10回"}' \
  | workout record --request-id "$(uuidgen)"
```

トレーニング文脈と重量推移を取得します。

```sh
workout context --json
workout progress --json
workout progress --exercise ベンチプレス --json
workout progress --exercise ベンチプレス --html "$(dirname "$WORKOUT_DB_PATH")/progress.html"
unset WORKOUT_DB_PATH
```

## AIエージェントとの接続

利用するAIエージェントに、上の例と [CLI仕様](docs/design.md#moduleとinterface) を渡してください。AIエージェントは報告を構造化して `workout record` の標準入力にJSONを1件渡し、標準出力の `status` を確認します。`not_recorded` も終了コードは0です。訂正・取消には対象の `set_id` を指定します。次回メニューの提案には `workout context --json` を使います。

種目登録は利用者の同意後に行います。操作ごとに新しいUUIDを作り、結果が不明な操作の再試行には同じ依頼IDと同じJSONを使います。実施時刻の指定を省略すると報告時刻を使います。指定する場合はUTCオフセット付きのISO 8601形式を使います。

## 保存先

SQLiteの既定の保存先は `~/.local/share/workout-tracker/workout.sqlite3` です。バックアップは同じディレクトリの `workout.backup.sqlite3` に作ります。DBとバックアップには報告の原文が含まれます。HTMLには種目名と実施日、重量、回数が含まれます。`WORKOUT_DB_PATH` や `--html` で場所を指定する場合も、DB、SQLiteの付随ファイル、バックアップ、HTMLをGitの作業ツリーの外に保管してください。

macOSやLinuxで以前の版から更新した場合は、`ls -l` で既存のDBとバックアップが `-rw-------` か確認してください。必要なら、CLIやほかのSQLite接続を閉じてから、自分が所有する各ファイルに `chmod 600 <ファイルのパス>` を実行します。

バックアップの由来を確認できない場合は `BACKUP_FAILED` が出ます。以前の版で作成したバックアップが該当する場合は、元のDBを確認してから別の場所に保管すると、次回の起動時に新しいバックアップが作られます。

バックアップをCLIで確認するときは、作業ツリー外に作ったコピーを `WORKOUT_DB_PATH` に指定してください。復元にもコピーを使うと、元のバックアップの照合情報を保てます。

## 開発

実行時依存はPython標準ライブラリだけです。

```sh
uv run --locked python -m unittest discover -s tests -v
```

筋トレ記録の設計は [`docs/design.md`](docs/design.md)、将来の音声入力は [`docs/voice-input.md`](docs/voice-input.md) を参照してください。

## License

[MIT License](LICENSE)
