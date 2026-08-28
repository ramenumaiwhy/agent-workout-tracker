# agent-workout-tracker

AIエージェントへ報告した筋トレのセット実績を、SQLiteへ1回だけ保存するローカルCLIです。同じ記録から、AIエージェント用のトレーニング文脈と人向けの重量グラフを作ります。

MVPの入力元はAIエージェントです。Siri、音声認識、入力用Web UI、Remote MCPは含みません。

## 必要な環境

- Python 3.11以上
- [uv](https://docs.astral.sh/uv/)を推奨

## インストール

GitHub公開後は次のコマンドでインストールできます。

```sh
uv tool install git+https://github.com/ramenumaiwhy/agent-workout-tracker
```

開発中のローカルcheckoutを試す場合は、repoのルートで次を実行します。

```sh
uv tool install .
workout --help
```

## AIエージェントとの接続

AIエージェントは、自然文の報告を構造化し、`workout`を呼びます。SQLiteを直接読みません。対応する`$workout`スキルは、公開後に[ramenumaiwhy/skills](https://github.com/ramenumaiwhy/skills)から配布します。

初めて使う種目は、利用者の同意後に種目登録します。

```sh
printf '%s\n' '{"type":"register_exercise","name":"ベンチプレス","aliases":["ベンチ"],"source_text":"ベンチプレスとして登録することに同意"}' \
  | workout record --request-id "$(uuidgen)"
```

種目登録後にセット実績を記録します。依頼IDは操作ごとに新しいUUIDを使います。同じ操作を再試行する場合だけ同じ依頼IDを使います。

```sh
printf '%s\n' '{"type":"add_set","exercise":"ベンチプレス","weight_kg":60,"reps":10,"source_text":"ベンチプレス60キロ10回"}' \
  | workout record --request-id "$(uuidgen)"
```

トレーニング文脈と重量推移を取得します。

```sh
workout context --json
workout progress --json
workout progress --exercise ベンチプレス --json
workout progress --exercise ベンチプレス --html ./progress.html
```

既定の保存先は`~/.local/share/workout-tracker/workout.sqlite3`です。試験時だけ`WORKOUT_DB_PATH`で変更できます。

## 開発

runtime dependencyはPython標準ライブラリだけです。

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

筋トレ記録の設計は[`docs/design.md`](docs/design.md)を参照してください。将来の音声入力は[`docs/voice-input.md`](docs/voice-input.md)へ分離しています。食事、身体、ジム及び筋トレを組み合わせる上位の分析設計は[`docs/body-change-system.md`](docs/body-change-system.md)にあります。

## License

[MIT License](LICENSE)
