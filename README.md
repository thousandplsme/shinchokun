# 🤖 しんちょくん — Discord ゲーミフィケーション進捗管理Bot

卒業制作チーム（5人）の進捗管理を **ゲーミフィケーション** で自動化する Discord Bot です。
クエスト達成でEXPを獲得し、称号（ロール）が成長していく仕組みで、チームのモチベーションを向上させます。

## ✨ 機能一覧

| 機能 | 説明 |
|---|---|
| 🗡️ **クエスト自動配信** | 毎週月曜 09:00 にGoogle Sheetsからタスクを読み込み、各メンバーへDMで配信 |
| ⭐ **EXP & 称号システム** | クエスト完了でEXP獲得 → レベルアップ → 称号変更 → Discordロール自動付与 |
| 🆘 **SOS & メンター機能** | コマンドチャンネルからいつでも相談。メンターにDM＋リーダーにCc自動共有 |
| 📅 **ミーティング** | 個別打診（相手選択→日時提案）& 日程調整（調整さん方式、全員投票→サーバーイベント自動作成） |
| 💌 **ありがとうを送る** | メンバーに感謝メッセージを送信 → 双方にEXP付与 |
| 📝 **タスク申請** | メンバーがリーダーにタスク追加を申請 → 承認でSheetsに自動追記 |
| 📊 **週間サマリー** | 毎週日曜 21:00 にEXPランキング・クエスト消化率・称号一覧を自動投稿 |
| 🎯 **今後の目標** | プロジェクトの締切と残り日数を一覧表示 |
| 🏆 **ランキング** | チーム内のEXPランキングをリアルタイム表示 |

## 🎮 利用方法

メンバーは **コマンドチャンネル** に常設されたボタンから全機能を利用します。
スラッシュコマンドの入力は不要です。

```
📌 しんちょくんコマンドチャンネル
┌──────────────────────────────────────────────────┐
│  🤖 しんちょくん コマンドセンター                  │
│                                                  │
│  [🆘 SOS] [📅 ミーティング]                       │
│  [⚔️ クエスト状況] [🎯 今後の目標]                │
│  [💌 ありがとうを送る] [🏆 ランキング]           　│
│  [📝 タスク申請]                                 │
└──────────────────────────────────────────────────┘
```

各ボタンを押すと **自分だけに見える（ephemeral）** メッセージで応答するため、チャンネルが汚れません。

### 📅 ミーティング機能

ミーティングボタンを押すと2つのモードを選択できます：

| モード | 用途 | フロー |
|--------|------|--------|
| **個別打診** | 少人数向け | 相手を選択 → 日時＋議題を入力 → DMで参加/別日/辞退 |
| **日程調整** | 全体向け | 候補日時を入力 → 全員にDM投票 → 48h自動集計 → サーバーイベント作成 |

### 📝 タスク申請機能

メンバーがリーダーにタスク追加を申請するフローです：

1. 「📝 タスク申請」→ 今週/来週を選択
2. タスク名・詳細・希望EXPを入力
3. リーダーのDMに承認/却下ボタン付きで通知
4. 承認 → Google Sheets「週次タスク」に自動追記 + 申請者に通知

## 🎮 ゲーミフィケーション要素

### 称号システム

| 必要レベル | 称号           | 累計EXP目安 |
|-----------|----------------|------------|
| Lv.1〜    | 見習い [職種]   | 0          |
| Lv.5〜    | 駆け出し [職種] | 250        |
| Lv.10〜   | [職種] C級     | 1,000      |
| Lv.20〜   | [職種] B級     | 4,000      |
| Lv.30〜   | [職種] A級     | 9,000      |
| Lv.50〜   | マスター [職種] | 25,000     |

### EXP獲得ルール

| アクション | 獲得EXP |
|-------------|---------| 
| クエスト完了 | 可変（スプレッドシートで設定） |
| SOS送信（困りごとの共有） | +20 |
| ピアボーナス送信 | +15（送信者） |
| ピアボーナス受信 | +15（受信者） |
| 3週連続クエスト達成ボーナス | +100 |

> ピアボーナスは1日3回まで送信できます（スパム防止）。

## 📦 セットアップ

### 前提条件
- Python 3.10 以上（推奨: 3.13.3）
- Discord Bot トークン（[Developer Portal](https://discord.com/developers/applications) で取得）
- Google Cloud Platform サービスアカウント（[セットアップ手順](docs/google_sheets_setup.md)）

### 1. クローン & インストール

```bash
git clone https://github.com/わたしのユーザー名/shinchokun.git
cd shinchokun
python -m venv .venv

# Windows
.\.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Google Sheets API の設定

[docs/google_sheets_setup.md](docs/google_sheets_setup.md) に従い、以下を完了してください：
1. GCPプロジェクト作成 & Sheets API 有効化
2. サービスアカウント作成 & JSONキーダウンロード
3. スプレッドシート作成（4シート + 任意2シート）& サービスアカウントに共有
4. `credentials/` にJSONを配置

### 3. 環境変数の設定

`.env.example` を `.env` にコピーして、各値を設定してください。

```bash
cp .env.example .env
```

| 変数名 | 説明 | 必須 |
|---|---|---|
| `DISCORD_TOKEN` | Botのトークン | ✅ |
| `GUILD_ID` | サーバーID | ✅ |
| `PROGRESS_CHANNEL_ID` | 全体報告チャンネルID | ✅ |
| `SOS_CHANNEL_ID` | コマンドチャンネルID | ✅ |
| `SPREADSHEET_ID` | Google スプレッドシートID | ✅ |
| `GOOGLE_CREDENTIALS_PATH` | サービスアカウントJSONパス | ✅ |
| `MEMBER_1_ID` ~ `MEMBER_5_ID` | メンバーのユーザーID | 後から設定可 |

> **IDの取得方法**: Discord → 設定 → 詳細設定 → 開発者モード ON → 対象を右クリック → 「IDをコピー」

### 4. Discord Developer Portal の設定

1. [Developer Portal](https://discord.com/developers/applications) → Bot → **Privileged Gateway Intents** で以下を ON:
   - ✅ SERVER MEMBERS INTENT
   - ✅ MESSAGE CONTENT INTENT

2. **OAuth2** → **URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Attach Files`, `Manage Roles`, `Manage Events`
   - 生成されたURLでBotをサーバーに追加

> ⚠️ **Manage Roles** は称号ロールの自動作成・付与に、**Manage Events** は日程調整のサーバーイベント自動作成に必要です。Botのロール順位を対象ロールより上に設定してください。

### 5. 起動

```bash
python bot.py
```

ログに `🤖 しんちょくん#XXXX としてログインしました！` と表示されれば成功です。

### 6. 初期セットアップ（初回のみ）

Bot起動後、Discordで以下のコマンドを実行:

```
/setup_hub       # コマンドセンターを設置
/sync_roles      # 全メンバーに称号ロールを一括付与
/sync_exp        # SheetsのEXPログからDB再計算（不整合がある場合）
```

## 🎮 管理者コマンド

全コマンドは管理者権限が必要です。メンバー向け機能はコマンドチャンネルのボタンで利用します。

| コマンド | 説明 |
|---|---|
| `/ping` | Botの応答確認 |
| `/setup_hub` | コマンドチャンネルに常設フォームを設置 |
| `/test_quest` | クエスト配信を手動実行 |
| `/test_summary` | 週次サマリーを手動投稿 |
| `/sync_exp` | SheetsのEXPログからDBのEXPを再計算 |
| `/sync_roles` | 全メンバーの称号ロールを一括付与 |
| `/help_bot` | 管理者用コマンド一覧 |

## 📁 プロジェクト構成

```
shinchokun/
├── bot.py                  # エントリーポイント（Bot起動・スケジューラ管理）
├── config.py               # 設定の一元管理
├── database.py             # SQLiteデータベース操作
├── sheets.py               # Google Sheets API連携
├── gamification.py         # EXP/称号ゲーミフィケーションシステム
├── rate_limiter.py         # Rate Limiter & リトライユーティリティ
├── cogs/
│   ├── hub.py              # 統合コマンドチャンネル（SOS・ミーティング・タスク申請等）
│   ├── quest.py            # クエスト自動配信 & 完了報告
│   ├── meeting.py          # 個別打診の応答View
│   ├── scheduler.py        # 日程調整（調整さん方式・投票・サーバーイベント）
│   ├── summary.py          # 週次サマリー
│   └── commands.py         # 管理者用スラッシュコマンド
├── credentials/
│   └── *.json              # Google認証（※Gitに含めない）
├── data/
│   └── progress.db         # データベース（自動生成）
├── .env                    # 環境変数（※Gitに含めない）
├── .env.example            # 環境変数テンプレート
├── requirements.txt        # 依存ライブラリ
└── Procfile                # デプロイ用
```

## ⏰ 自動スケジュール

| 曜日 | 時刻 | 動作 |
|---|---|---|
| **月曜** | **09:00** | クエスト自動配信（DM） |
| **日曜** | **21:00** | 週間サマリー自動投稿 |
| **毎時** | **:00** | 日程調整の期限切れ投票チェック |

## 🔧 カスタマイズ

### スケジュールの変更
`config.py` の以下の値を変更:
```python
QUEST_DAY = "mon"       # クエスト配信の曜日
QUEST_HOUR = 9          # クエスト配信の時刻（24時間制）
SUMMARY_DAY = "sun"     # サマリー投稿の曜日
SUMMARY_HOUR = 21       # サマリー投稿の時刻
```

### 今後の目標の更新
`config.py` の `MILESTONES` リストを編集。

### 職種の追加
Google スプレッドシートの「職種マスタ」シートに1行追加するだけ（Bot再起動で反映）。

### メンバー別メッセージの設定
1. 「メンバー」シートに `dm_tone` 列を追加（`formal` / `casual` / `cheerful`）
2. 「メンバー」シートに `nickname` 列を追加（Bot内の呼び名）
3. 任意: 「メッセージテンプレート」シートでカスタムメッセージを定義

未設定でも `formal` トーンのデフォルトメッセージで動作します。

### EXP獲得量の変更
`config.py` の `EXP_REWARDS` 辞書を編集。

## 📊 データベース構成

| テーブル | 用途 |
|---|---|
| `user_profiles` | ユーザーのEXP・レベル・称号（永続） |
| `exp_records` | EXP獲得履歴 |
| `quest_completions` | クエスト完了記録 |
| `sos_requests` | SOS送信履歴 |
| `meeting_requests` | 個別打診リクエスト |
| `peer_bonuses` | ピアボーナス送信履歴 |
| `schedule_polls` | 日程調整投票 |
| `schedule_votes` | 日程調整の個別回答 |
| `task_requests` | タスク追加申請 |
| `progress_reports` | 旧: 進捗レポート（過去データ参照用） |
| `schedule_rounds` | 旧: スケジュールラウンド（過去データ参照用） |

## 📝 ライセンス

このプロジェクトは学内卒業制作用に開発されました。
