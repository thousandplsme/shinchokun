"""
config.py — Bot設定の一元管理（v2 改訂版）

すべての設定値をここで管理します。
.env から読み込む値と、コード内で定義する値を分離しています。

【v2 での変更点】
- Google Sheets関連の環境変数を追加
- SOSチャンネルIDを追加
- 称号テンプレート・EXP設定を追加
- 水/土の進捗確認スケジュールを廃止（クエスト中心に移行）
- クエスト配信スケジュール（月曜 09:00）を追加
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ============================================
# Discord 設定（.env から読み込み）
# ============================================
# 【実務的な設計意図（セキュリティ）】
# トークンなどの機密情報をコードに直書きし、そのままGitHubなどの
# バージョン管理システム（Git）にコミットすると、第三者にBotを乗っ取られる
# 危険性（インシデント）があります。
# そのため、機密情報は必ず `.env` ファイルに分離し、Gitの管理外（.gitignore）
# にするのが実務上の絶対的なベストプラクティスです。
#
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
PROGRESS_CHANNEL_ID = int(os.getenv("PROGRESS_CHANNEL_ID", "0"))
SOS_CHANNEL_ID = int(os.getenv("SOS_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))


# ============================================
# Google Sheets 設定（.env から読み込み）
# ============================================
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/service_account.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")


# ============================================
# メンバー設定
# ============================================
# メンバーのDiscordユーザーIDを .env から読み込み
#
# 【学べること：内包表記】
# `for i in range(1, 6)` を使って、1〜5までのキーと値を動的に生成しています。
# メンバーが10人に増えた場合も、`range(1, 11)` に変えるだけで対応できます。
#
# 【実務的な設計意図】
# なぜソースコード内に直接ID（直書き/ハードコード）を書かないのか？
# → メンバーの入れ替え時にコードの修正・再デプロイが不要になるため。
#
MEMBER_IDS = {
    f"member_{i}": int(os.getenv(f"MEMBER_{i}_ID", "0"))
    for i in range(1, 6)
}

# リーダーのID
LEADER_ID = MEMBER_IDS.get("member_4", 0)

# 全メンバーIDのリスト（0は未設定なので除外）
def get_active_member_ids():
    """設定済みのメンバーIDだけを返す"""
    return [uid for uid in MEMBER_IDS.values() if uid != 0]


# ============================================
# スケジュール設定（v2 改訂版）
# ============================================

# --- クエスト配信（月曜 09:00）---
QUEST_DAY = "mon"
QUEST_HOUR = 9
QUEST_MINUTE = 0

# --- 週次サマリー（日曜 21:00）---
SUMMARY_DAY = "sun"
SUMMARY_HOUR = 21

# タイムゾーン
TIMEZONE = "Asia/Tokyo"


# ============================================
# ゲーミフィケーション設定
# ============================================

# --- EXP獲得量 ---
EXP_REWARDS = {
    "quest_complete": None,  # スプレッドシートの exp_reward 値を使用
    "sos_send": 20,          # SOS送信ボーナス（困りごとを共有した勇気）
    "streak_bonus": 100,     # 3週連続クエスト達成ボーナス
    "peer_bonus_send": 15,   # ピアボーナス送信者（感謝を伝えた行動に報酬）
    "peer_bonus_receive": 15, # ピアボーナス受信者
}

# ピアボーナス1日あたりの送信上限
PEER_BONUS_DAILY_LIMIT = 3

# --- 称号テンプレート ---
# キー: 必要レベル の下限
# 値: 称号テンプレート（{job} に職種名が入る）
#
# 【OrderedDict のように、キーの昇順で定義する】
# レベル判定時に「該当する最大のキー」を選ぶロジックで使用。
TITLE_TEMPLATES = {
    1:  "見習い {job}",
    5:  "駆け出し {job}",
    10: "{job} C級",
    20: "{job} B級",
    30: "{job} A級",
    50: "マスター {job}",
}

# --- レベル計算式 ---
# Level = floor(sqrt(total_exp / EXP_PER_LEVEL_UNIT))
# この値を変えるとレベルアップに必要なEXPが変わる
EXP_PER_LEVEL_UNIT = 10

# --- ロールカラー（称号ランクごとの色）---
# Discord のロール色は 16進数の整数で指定する
ROLE_COLORS = {
    1:  0x95A5A6,  # 見習い: グレー
    5:  0x2ECC71,  # 駆け出し: 緑
    10: 0x3498DB,  # C級: 青
    20: 0x9B59B6,  # B級: 紫
    30: 0xF39C12,  # A級: 金
    50: 0xE91E63,  # マスター: ピンク（虹色の代替）
}


# ============================================
# モチベーション絵文字マッピング
# ============================================
MOTIVATION_EMOJIS = {
    1: "💀",   # 最悪
    2: "😣",   # つらい
    3: "😐",   # 普通
    4: "😊",   # 良い
    5: "😤",   # 絶好調
}

MOTIVATION_LABELS = {
    1: "最悪",
    2: "つらい",
    3: "普通",
    4: "良い",
    5: "絶好調",
}


# ============================================
# マイルストーン定義
# ============================================
MILESTONES = [
    {
        "name": "企画確定・コンセプト固め",
        "deadline": "2026-04-30",
        "description": "コンセプトドキュメント完成、コアループ定義",
    },
    {
        "name": "技術検証（コアメカニクス）",
        "deadline": "2026-06-30",
        "description": "コアメカニクスの動作確認、30fps以上",
    },
    {
        "name": "プロトタイプ版（グレーボックス）",
        "deadline": "2026-08-31",
        "description": "遊べる最小単位の完成",
    },
    {
        "name": "プロトタイプ版レビュー・改善",
        "deadline": "2026-09-30",
        "description": "第三者テストプレイ、フィードバック反映",
    },
    {
        "name": "アルファ版制作",
        "deadline": "2026-11-30",
        "description": "全機能実装完了、致命的バグゼロ",
    },
    {
        "name": "ベータ版・バランス調整",
        "deadline": "2026-12-31",
        "description": "バランス調整、演出追加",
    },
    {
        "name": "最終ブラッシュアップ・発表準備",
        "deadline": "2027-01-31",
        "description": "発表用ビルドの安定動作確認",
    },
]


# ============================================
# Embed カラー
# ============================================
COLOR_PRIMARY = 0x5865F2     # Discord Blurple
COLOR_SUCCESS = 0x57F287     # 緑
COLOR_WARNING = 0xFEE75C     # 黄
COLOR_DANGER  = 0xED4245     # 赤
COLOR_INFO    = 0x5865F2     # 青
COLOR_GOLD    = 0xF1C40F     # 金（ランキング用）
COLOR_QUEST   = 0xE67E22     # オレンジ（クエスト用）
COLOR_SOS     = 0xE91E63     # ピンク（SOS用）
COLOR_PEER    = 0xF472B6     # ピンク明（ピアボーナス用）


# ============================================
# デフォルトメッセージテンプレート
# ============================================
# スプレッドシートの「メッセージテンプレート」シートが
# 未作成の場合にフォールバックとして使用される。
# メンバーの dm_tone に対応するキーで検索する。

DEFAULT_MESSAGES = {
    "formal": {
        "quest_greeting": "今週のクエストです 🗡️",
        "quest_complete": "お疲れさまでした！ ✨",
        "sos_thanks": "報告ありがとう🙏",
        "peer_bonus": "感謝のメッセージが届きました 💌",
    },
    "casual": {
        "quest_greeting": "おつかれ！今週のクエストだよ 🗡️",
        "quest_complete": "やるじゃん！さすが！ 🔥",
        "sos_thanks": "相談ありがとう！一緒に解決しよう 💪",
        "peer_bonus": "ありがとうが届いたよ！💌",
    },
    "cheerful": {
        "quest_greeting": "やっほー！クエストの時間だよ！✨",
        "quest_complete": "すごい！天才！ 🎉🎉",
        "sos_thanks": "声かけてくれて嬉しい！一緒にがんばろう！🌈",
        "peer_bonus": "スペシャルサンクスが届いたよ！💝",
    },
}

# デフォルトトーン（メンバーに dm_tone が未設定の場合）
DEFAULT_TONE = "formal"
