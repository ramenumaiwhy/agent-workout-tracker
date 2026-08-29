# 筋トレ記録のMVP設計

## 結論

MVPでは、利用者がAIエージェントへセット実績を報告します。AIエージェントは専用の`SKILL.md`に従って報告を構造化し、ローカルCLI adapterから筋トレ記録moduleを呼びます。保存先はSQLiteです。入力用Web UIは作りません。

Siri、音声認識、iPhone、Tailscale通信はMVPに含めません。これらは将来の音声入力adapterとして、記録moduleの外へ分離します。MVPで音声固有の処理を作らないため、先に記録、訂正、参照、次回メニュー提案の価値を検証できます。

MVPではRemote MCPも作りません。Mac上のAIエージェントはCLI adapterを使います。Mac以外のAIエージェントから使う必要が発生した時点で、Remote MCP adapterを追加します。

実装はPython 3.11以上と標準ライブラリだけを使います。`sqlite3`、`argparse`、`unittest`、HTML、inline SVGで構成します。MVPではruntime用の外部package、ORM、Web framework、migration frameworkを追加しません。

## 利用手順

通常の記録は次の流れです。

```text
利用者: ベンチプレス60キロ10回やった
AIエージェント: ベンチプレス、60kg、10回を記録しました。
```

訂正と取消も同じ会話で行います。

```text
利用者: 今のは9回だった
AIエージェント: 直前のセット実績を、60kg、9回へ訂正しました。

利用者: 今のを取り消して
AIエージェント: 直前のセット実績を取り消しました。
```

トレーニングの開始操作と終了操作はありません。最初のセット実績がトレーニングを自動で開始します。4時間以上の空きがある場合は、次のセット実績を新しいトレーニングへ入れます。

## 全体構造

```text
利用者の報告
      │
      ▼
AIエージェント ───▶ SKILL.md ───▶ CLI adapter ───▶ 筋トレ記録 module ───▶ SQLite
      ▲                                      │
      │                         ┌────────────┴────────────┐
      │                         ▼                         ▼
      └──── トレーニング文脈                 重量グラフ用の推移
```

AIエージェントは自然文を理解します。記録moduleは自然文を理解しません。記録moduleは種目、重量、回数などの確定した値だけを受けます。このseamにより、将来Siriを追加してもSQLite、訂正、集計のimplementationは変わりません。

## Moduleとinterface

外部interfaceは3操作にします。

### `record(command, request_id) -> receipt`

`command`は次の4種類です。

```text
AddSet:
  type = add_set
  exercise
  weight_kg  # 自重種目の外部負荷は0
  reps
  performed_at?
  source_text

CorrectSet:
  type = correct_set
  target_set_id
  changes:
    exercise?
    weight_kg?
    reps?
    performed_at?
  source_text

VoidSet:
  type = void_set
  target_set_id
  source_text

RegisterExercise:
  type = register_exercise
  name
  aliases?
  source_text
```

結果は次の値です。

```text
receipt:
  status = recorded | not_recorded
  set_id?
  training_id?
  normalized_set?
  registered_exercise?
  warnings?
  error?:
    code
    current_set_id?
```

commandはJSON objectを1件使います。`type`で4種類を判別します。`receipt.status`はcommandを適用できたかを表します。`RegisterExercise`の`recorded`は種目登録の完了を意味します。`registered_exercise`には正規化後の正式名と別名を返します。

AIエージェントは、利用者の「今の」を直前の`receipt.set_id`へ解決します。記録moduleは「直前」を推測しません。会話の文脈がない場合はトレーニング文脈を確認し、対象が曖昧なら利用者へ質問します。

`CorrectSet.changes`は1項目以上を必須にします。指定されていない値は、記録moduleが対象のセット実績から引き継ぎます。`performed_at`を変えた場合は、所属するトレーニングも再評価します。

種目名の正規化、値の検証、トレーニングの自動作成、訂正前の値の保持、tool呼び出しの重複防止をimplementationへ隠します。

### `getTrainingContext() -> json`

AIエージェントはこの操作を使います。SQLiteを直接読みません。MVPでは引数を持ちません。

結果には次の値を含めます。

- 現在のトレーニングにある有効なセット実績と`set_id`
- 種目別の直近5回のトレーニング
- 有効なセット実績の`set_id`、実施時刻、種目、重量、回数
- 前回実施日からの日数
- 訂正後に有効なセット実績

AIエージェントは、この事実から次回メニューを作ります。MVPでは推薦moduleを作りません。次回メニューもDBへ保存しません。

「現在のトレーニング」は、現在時刻から4時間以内に有効なセット実績がある、最も新しいトレーニングです。該当するトレーニングがない場合は`null`を返します。種目別の直近5回は、現在のトレーニングの有無に関係なく返します。

### `getProgress(exercise?) -> progress`

重量グラフ用の値を返します。`exercise`を省略した場合は、登録済み種目の正式名一覧を返します。指定した場合は、各トレーニングの実施日、最高重量、その重量の回数を返します。未登録種目には`UNKNOWN_EXERCISE`を返します。グラフ生成側はSQLiteを直接読みません。

## AIエージェント用skill

MVPには`SKILL.md`を1枚使います。これは、AIエージェントが記録moduleを正しく使うためのinterfaceです。新しいmoduleは作りません。正本は`ramenumaiwhy/skills`の`skills/custom/workout/SKILL.md`です。本体repoへcopyを置きません。

skillには次を記載します。

- 完了したセット実績の報告、訂正、取消、次回メニュー、重量グラフの依頼で起動する。
- 予定、目標、設計相談では記録しない。
- 種目、外部負荷、回数が不足又は曖昧な場合は、CLIを呼ぶ前に質問する。追加負荷がない自重種目では外部負荷を0kgとする。
- 利用者が複数のセット実績を1回で報告した場合は、報告順に`AddSet`を複数回呼ぶ。
- 「今の」は会話中の`receipt.set_id`を使う。ない場合はトレーニング文脈を取得し、曖昧なら質問する。
- `UNKNOWN_EXERCISE`では、利用者へ種目登録の確認を行う。明示的な同意後だけ`RegisterExercise`を呼ぶ。
- 種目登録後に元の`AddSet`を行う場合は、新しい依頼IDを使う。最初の依頼は`not_recorded`で完了しているためです。
- 保存結果の種目、重量、回数を完全な形で返す。
- 再試行では同じ依頼IDを使う。

CLIの形は次に固定します。

```text
workout record --request-id <UUID>    # command JSONを標準入力から1件読む
workout context --json
workout progress --json
workout progress --exercise <正式名> --json
workout progress --exercise <正式名> --html <出力先>
```

CLIは標準出力へJSONを1件だけ返します。診断情報は標準エラー出力へ返します。正常な`receipt`を返せた場合は、`recorded`と`not_recorded`のどちらでも終了code 0です。CLI又は保存先が動かず`receipt`を返せない場合だけ0以外にします。

`--html`は同じ`getProgress`の結果からHTMLとinline SVGを作ります。筋トレ記録moduleの操作は増やしません。作成したファイルの絶対pathをJSONで返します。公開URLへの変換は実行環境の責務です。

## AIエージェントの責務

AIエージェントは次を行います。

- 利用者の報告から種目、重量、回数、実施時刻を取り出す。
- 不足又は複数解釈がある場合は、記録前に質問する。
- 確定した値だけを`record`へ渡す。
- `receipt`の完全な値を利用者へ返す。
- トレーニング文脈から次回メニューを提案する。
- 1回の報告に複数のセット実績がある場合は、報告順に複数の`AddSet`へ分ける。

記録moduleは次を行いません。

- 自然文の解釈
- 音声認識
- Siriとの会話
- 次回メニューの生成

自然文の理解をAIエージェントへ置くことで、限定文法のparserを作りません。保存の正しさは、記録moduleの構造化interfaceと制約で守ります。

## 不変条件とエラー

- `request_id`は一意です。同じtool呼び出しの再送ではセット実績を増やしません。
- 依頼IDはAIエージェントがcommandごとにUUIDで作ります。
- 結果が不明な失敗又は`STORAGE_UNAVAILABLE`の再試行では、同じ依頼IDと同じcommandを使います。
- `record_requests`へ結果が保存済みの場合、同じ依頼IDと同じcommandの再送には初回と同じ`receipt`を返します。
- 同じ依頼IDでcommandが異なる場合は`IDEMPOTENCY_CONFLICT`を返します。
- 外部負荷は0kg以上、500kg以下です。0kgは追加負荷がない自重種目を表します。
- 回数は1回以上、100回以下です。
- `AddSet`は1件のセット実績だけを保存します。
- 元の報告は`source_text`として変更せずに残します。
- 訂正前のセット実績は削除しません。無効として残します。
- `CorrectSet`と`VoidSet`の対象は有効なセット実績だけです。無効な対象には`TARGET_NOT_ACTIVE`を返します。有効な後継がある場合は`error.current_set_id`へ返します。
- 1件の有効なセット実績から作れる有効な後継は1件だけです。訂正と取消は同じtransactionで対象の有効性を確認して適用します。
- 参照とグラフは有効なセット実績だけを使います。
- 不完全な構造化入力は保存しません。
- 保存に失敗した場合は成功を返しません。
- `performed_at`を省略した場合は、記録moduleが報告時刻を使います。
- 実施時刻が既存の有効なセット実績から4時間以内なら、最も近いセット実績と同じトレーニングへ入れます。該当しない場合は新しいトレーニングを作ります。
- 未登録の種目は暗黙に追加しません。利用者の同意後に`RegisterExercise`で種目登録します。
- 種目の検索keyはUnicode NFKC、前後の空白削除、連続空白の1文字化、Unicode case foldingで正規化します。正式名の表示文字列は変更しません。
- 正規化後の正式名と別名は全種目で一意です。既存の正式名又は別名と衝突する種目登録は`EXERCISE_CONFLICT`を返します。
- 数値はJSONの有限なnumberだけを受けます。boolean、`NaN`、無限大は拒否します。重量は小数第3位までを受け、SQLiteの`REAL`へ保存します。
- 指定する`performed_at`はUTC offset付きISO 8601だけを受けます。省略時の`reported_at`と`performed_at`は、実行中のMacのlocal timezoneとoffsetを使います。

主なエラーは次のとおりです。

```text
UNKNOWN_EXERCISE
MISSING_FIELD
INVALID_VALUE
EXERCISE_CONFLICT
TARGET_NOT_FOUND
TARGET_NOT_ACTIVE
IDEMPOTENCY_CONFLICT
STORAGE_UNAVAILABLE
```

同じ内容の近接重複は検出しません。同じ重量と回数の連続セットは正しい記録になり得ます。二重実行は依頼IDで防ぎます。

## データモデル

MVPは5つのtableを使います。

### `exercises`

種目の正式名と正規化した検索keyを保存します。検索keyには一意制約を付けます。初期状態は空にします。利用者が最初に報告した時点で確認し、使う種目だけを種目登録します。

### `exercise_aliases`

「ベンチ」と「ベンチプレス」などの別名と正規化した検索keyを保存します。正式名を含む全検索keyの衝突は、種目登録のtransaction内で検査します。

### `trainings`

セット実績のまとまりを保存します。開始操作と終了操作はありません。

### `set_performances`

次の値を保存します。

```text
id
training_id
exercise_id
performed_at
weight_kg
reps
status
supersedes_id
request_id
source_text
reported_at
```

`reported_at`は記録moduleが報告を受けた時刻です。時刻はoffset付きISO 8601で保存します。外部キー、`CHECK`制約、必要なindexを設定します。`request_id`は`record_requests`への外部キーです。一意制約は`record_requests`だけが所有します。

### `record_requests`

依頼ID、正規化済みcommand JSON、commandのfingerprint、`receipt`、受付時刻を保存します。取消を含む元の報告は、このcommand JSONの`source_text`から確認できます。commandの処理と`receipt`の保存は1つのtransactionで行います。同じ依頼IDの再送で初回と同じ結果を返し、内容が異なる再送を拒否します。`request_id`の一意制約はこのtableで管理します。保存先へ到達できずtransactionを開始できない場合は結果を残せないため、同じ依頼IDで再試行できます。

commandのfingerprintは、入力JSONを検証した後、key順を固定した空白なしのUTF-8 JSONからSHA-256で作ります。同じ依頼IDで同じ正規化済みcommandを受けた場合だけ、保存済みの`receipt`を返します。

全SQLite接続で`PRAGMA foreign_keys = ON`を設定し、有効になったことを確認します。書き込みは`BEGIN IMMEDIATE`を使い、commandの適用と`record_requests`への`receipt`保存を1つのtransactionにします。接続には5秒のbusy timeoutを設定します。schema作成は起動時に冪等に行います。旧DBの重量制約は、起動時に外部負荷0kgを許可する制約へ移行します。migration frameworkは作りません。

## 重量グラフ

記録用の入力機能は付けません。`getProgress()`が返す読み取り専用の種目一覧から1つを選び、トレーニングごとの最高重量を折れ線で表示します。各点には回数を表示します。

HTMLとinline SVGを使います。Chartライブラリは使いません。グラフは`getProgress`の結果から要求時に作ります。別の更新処理は不要です。

e1RM、総volume、PR判定はMVPへ入れません。

## 保存失敗への対応

CLI adapter又はSQLiteで失敗した場合、AIエージェントは「記録できませんでした」と返します。会話に利用者の報告が残るため、iPhone側の退避処理は作りません。利用者は障害解消後に同じ報告を再実行できます。

CLI adapterは起動時にbackupファイルの更新時刻を確認します。既存DBはschema移行前にbackupします。ファイルがない場合又は24時間以上経過している場合は、`sqlite3.Connection.backup`で1回保存します。backupは一時ファイルへ作り、完了後に固定名へ置き換えます。backup失敗は現在の記録を止めません。`receipt.warnings`と標準エラー出力へ警告を返します。固定名の更新時刻は成功時だけ変わるため、失敗時は次回のCLI起動で再試行します。常駐処理とbackup管理tableは追加しません。セット実績は後から正確に再生成できないためです。

## 既存の食事記録から使う考え方

- SQLiteを事実の保存先にする。
- 利用者の元の報告を保存する。
- 実施時刻と報告時刻を分ける。
- 記録後に結果を検証する。
- 人向け表示は保存データから作る。
- AIエージェントへDB schemaを公開しない。

筋トレ記録は食事記録と別DBにします。既定の保存先は`~/.local/share/workout-tracker/workout.sqlite3`です。試験と移行時だけ`WORKOUT_DB_PATH`で上書きできます。backupは同じdirectoryの`workout.backup.sqlite3`へ保存します。食事記録と障害及び変更の影響を分けます。

## MVPに含める範囲

- 1利用者
- Mac上のAIエージェント
- AIエージェント用`SKILL.md`
- ローカルCLI adapter
- kgと回数
- 追加負荷がない自重種目
- 通常記録
- 対象IDを指定した訂正
- 対象IDを指定した取消
- 利用者の確認後の種目登録
- `request_id`による二重実行防止
- 元の報告の保存
- トレーニングの自動 grouping
- トレーニング文脈のJSON
- AIエージェントによる次回メニュー提案
- 種目別の重量推移グラフ
- SQLiteの日次バックアップ

## MVPに含めない範囲

- Siriショートカット
- 音声認識
- 音声による復唱と確認
- iPhoneからの記録
- Tailscale経由の入力
- 入力用Web UI
- native iOS app
- Remote MCP
- Cloud保存
- オフライン同期
- 複数利用者
- module側の複数セット用batch command
- rest timer
- plate計算
- RPEとRIR
- Apple Health連携
- 動画解析
- 自動periodization module
- Chartライブラリ

## 検証条件

実装完了は次の条件で判定します。

- AIエージェントへの1回の報告で、1件のセット実績を記録できる。
- 追加負荷がない自重種目を、外部負荷0kgのセット実績として記録できる。
- 代表的な報告10件から、AIエージェントが正しいcommand列を作れる。複数セット、過去日付、曖昧な報告、未登録種目を含める。
- 代表的な報告10件を`SKILL.md`の例にも使う。
- 不足又は曖昧な報告では、AIエージェントが確認してから記録する。
- 記録結果として種目、重量、回数を返す。
- 同じ`request_id`を3回送っても1件だけ保存する。
- 同じ依頼IDで異なるcommandを送ると`IDEMPOTENCY_CONFLICT`になる。
- 結果不明の失敗後に同じ依頼IDで再試行すると、初回と同じ`receipt`を得る。
- 同じ重量と回数の連続セットを別のセット実績として保存できる。
- 「60kgを10回、8回、7回」という1回の報告を、3件のセット実績として報告順に保存できる。
- 訂正では対象の`set_id`を指定する。
- 別の会話でも、トレーニング文脈の`set_id`を使って訂正できる。
- 訂正済み又は取消済みのセット実績を訂正・取消の対象にできない。有効な後継がある場合は、その`set_id`を取得できる。
- 訂正後は元のセット実績が無効になる。
- 取消後は対象のセット実績が集計から外れる。
- トレーニング文脈は有効な値だけを集計する。
- AIエージェントはトレーニング文脈だけを使って次回メニューを1つ提案できる。
- 重量グラフは最新の有効なセット実績を表示する。
- 引数なしの`getProgress()`から、登録済み種目の正式名一覧を取得できる。
- 未登録種目の`getProgress()`は`UNKNOWN_EXERCISE`を返す。
- 保存失敗時に成功とは返さない。
- SQLiteのschemaをAIエージェントへ公開しない。
- 未登録種目は確認なしに追加されない。確認後は種目登録して記録を再実行できる。
- `performed_at`省略時は報告時刻を使う。過去日付の報告は4時間規則で正しいトレーニングへ入る。
- CLI起動時に前回バックアップから24時間以上経過していれば、日次バックアップを作る。
- バックアップ失敗時も現在の記録は成功し、警告を返す。成功時だけ前回バックアップ時刻を更新する。

将来Remote MCP又はSiriを追加する場合も、同じ記録moduleのinterface試験を使います。追加するものは入力adapterだけです。

## Siriを追加する条件

エージェント経由の記録を使った後、次の条件が確認できた場合に音声入力adapterを設計します。

- ジムでエージェント画面を開く操作が継続の妨げになる。
- セット実績の項目と訂正方法が安定している。
- 音声認識の聞き間違い対策へ追加工数を使う価値がある。

音声入力adapterは、Siriショートカット、音声認識、復唱、確認継続、近接重複の確認、iPhone側の退避を担当します。筋トレ記録moduleへ渡す値は、MVPと同じ構造化`command`です。

## Remote MCPを追加する条件

次の条件が発生した場合にRemote MCPを検討します。

- Mac以外のAIエージェントが直接参照する。
- Macを起動していない時間にも記録又は参照したい。
- 複数端末から同時に記録する。

必要になった時点で保存先、通信、認証を選びます。記録moduleのinterfaceとトレーニング文脈の形は維持します。
