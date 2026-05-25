"""
cogs/summary.py — 週次サマリー機能（v2 強化版）

日曜夜に1週間の進捗サマリーを自動投稿する。
v2ではクエスト完了状況・EXPランキング・称号情報を追加。
メンバーの追加入力は不要（全データを自動集計）。

【v2 での変更点】
- 進捗報告ベース → クエスト完了ベースに変更
- EXPランキング（今週の獲得EXP順）を追加
- 各メンバーの称号・レベルを表示
- クエスト消化率をメンバー別に表示

【このファイルで学べること】
- 複数のEmbedを一度に送信するパターン (embeds=[...])
- プレゼンテーション層（表示ロジック）とデータ取得ロジックの分離

【実務的な設計意図（巨大関数の分割とプレゼンテーション層の分離）】
週次サマリーのように大量の情報をフォーマットする処理は、1つの関数にまとめると
数百行に肥大化し（ファットコントローラー）、保守性が著しく低下します。
これを防ぐため、各セクション（EXPランキング、クエスト消化率、ステータス）ごとに
Embedを構築するヘルパーメソッド（_build_xxx_embed）へ関数分割しています。
これにより、特定の表示部分を修正したい場合の影響範囲を局所化できます。
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
from database import now_jst
from sheets import SheetsClient
from utils.messaging import send_dm_safely

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


class SummaryCog(commands.Cog):
    """週次サマリーの自動生成・投稿を管理するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def post_weekly_summary(self):
        """
        週次サマリーを投稿する（スケジューラから日曜 21:00 に呼ばれる）。
        """
        channel = self.bot.get_channel(config.PROGRESS_CHANNEL_ID)
        if not channel:
            logger.error(f"チャンネルが見つかりません: {config.PROGRESS_CHANNEL_ID}")
            return

        today = now_jst()
        monday = today - timedelta(days=today.weekday())
        week_start = monday.strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        embeds = []
        embeds.append(await self._build_exp_ranking_embed(week_start, week_end))
        
        all_quests = []
        try:
            sheets: SheetsClient = self.bot.sheets
            all_quests = await sheets.get_weekly_quests(week_start)
            embeds.append(self._build_quest_progress_embed(all_quests))
        except Exception as e:
            logger.error(f"⚠️ クエスト消化率取得エラー: {e}")
            embed_error = discord.Embed(title="⚔️ クエスト消化率", color=config.COLOR_QUEST)
            embed_error.add_field(name="⚠️ エラー", value="クエストデータの取得に失敗しました。", inline=False)
            embeds.append(embed_error)

        embeds.append(await self._build_member_status_embed())

        await channel.send(embeds=embeds)
        logger.info(f"📊 週間サマリーを投稿しました ({week_start} 〜 {week_end})")

        if all_quests:
            await self._send_unfinished_quest_dms(all_quests, week_start)

    async def _build_exp_ranking_embed(self, week_start: str, week_end: str) -> discord.Embed:
        """
        EXPランキングのEmbedを構築する。
        データソースはスプレッドシートのEXPログ（正のデータ源）。
        """
        embed = discord.Embed(
            title="📊 週間サマリー",
            description=f"**{week_start}** 〜 **{week_end}**",
            color=config.COLOR_GOLD,
        )

        try:
            sheets = getattr(self.bot, "sheets", None)
            if sheets:
                all_logs = await sheets.get_all_exp_logs()

                # 今週の期間でフィルタリング
                week_end_dt = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)
                week_end_str = week_end_dt.strftime("%Y-%m-%d")

                weekly_totals: dict[int, int] = {}
                for log in all_logs:
                    ts = log.get("timestamp", "")
                    if ts >= week_start and ts < week_end_str:
                        uid = log["discord_id"]
                        weekly_totals[uid] = weekly_totals.get(uid, 0) + log["amount"]

                # 降順ソート
                sorted_ranking = sorted(weekly_totals.items(), key=lambda x: x[1], reverse=True)

                if sorted_ranking:
                    ranking_lines = []
                    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                    for i, (uid, exp) in enumerate(sorted_ranking):
                        user = self.bot.get_user(uid)
                        name = user.display_name if user else f"ID:{uid}"
                        medal = medals[i] if i < len(medals) else f"{i+1}."
                        ranking_lines.append(f"{medal} **{name}** — +{exp} EXP")
                    embed.add_field(name="🏆 今週のEXPランキング", value="\n".join(ranking_lines), inline=False)
                else:
                    embed.add_field(name="🏆 今週のEXPランキング", value="今週のEXPデータはありません。", inline=False)
            else:
                embed.add_field(name="🏆 今週のEXPランキング", value="Google Sheets が未接続です。", inline=False)
        except Exception as e:
            logger.error(f"⚠️ EXPランキング構築エラー: {e}")
            embed.add_field(name="🏆 今週のEXPランキング", value="EXPデータの取得に失敗しました。", inline=False)

        return embed

    def _build_quest_progress_embed(self, all_quests: list[dict]) -> discord.Embed:
        """クエスト消化率のEmbedを構築する"""
        embed = discord.Embed(title="⚔️ クエスト消化率", color=config.COLOR_QUEST)
        if all_quests:
            member_quest_map: dict[int, dict] = {}
            for q in all_quests:
                uid = q["discord_id"]
                if uid not in member_quest_map:
                    member_quest_map[uid] = {"total": 0, "completed": 0}
                
                st = q.get("status", "")
                if st == "辞退":
                    continue # 辞退は分母・分子ともに含めない
                
                member_quest_map[uid]["total"] += 1
                if st == "完了":
                    member_quest_map[uid]["completed"] += 1

            for uid, stats in member_quest_map.items():
                user = self.bot.get_user(uid)
                name = user.display_name if user else f"ID:{uid}"
                rate = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0
                filled = int(rate / 10)
                bar = "▓" * filled + "░" * (10 - filled)
                embed.add_field(
                    name=f"🔹 {name}",
                    value=(
                        f"[{bar}] **{rate:.0f}%**\n"
                        f"✅ 完了: {stats['completed']} / 全{stats['total']}件"
                    ),
                    inline=False,
                )
        else:
            embed.add_field(name="📝 クエスト", value="今週のクエストはありません。", inline=False)
        return embed

    async def _build_member_status_embed(self) -> discord.Embed:
        """メンバーステータス（称号・レベル）のEmbedを構築する"""
        embed = discord.Embed(title="🏅 メンバーステータス", color=config.COLOR_PRIMARY)
        profiles = await self.bot.db.get_all_user_profiles()
        if profiles:
            for profile in profiles:
                user = self.bot.get_user(profile["discord_id"])
                name = user.display_name if user else f"ID:{profile['discord_id']}"
                embed.add_field(
                    name=f"🔹 {name}",
                    value=f"🏅 {profile['current_title']}\n📊 Lv.{profile['level']} | 💰 {profile['total_exp']} EXP",
                    inline=True,
                )
        else:
            embed.add_field(name="📝 ステータス", value="プロフィールデータがありません。", inline=False)
        embed.set_footer(text="しんちょくん 🤖 | 週間サマリー")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _send_unfinished_quest_dms(self, all_quests: list[dict], week_start: str):
        """未完タスクがあるメンバーへ確認DMを送信する"""
        pending_map: dict[int, list[dict]] = {}
        for q in all_quests:
            st = q.get("status", "").strip()
            if st not in ["完了", "辞退"]:  # 空欄・未着手・進行中 → すべて未完了
                uid = q["discord_id"]
                if uid not in pending_map:
                    pending_map[uid] = []
                pending_map[uid].append(q)

        for uid, p_quests in pending_map.items():
            if user := self.bot.get_user(uid):
                view = UnfinishedQuestDMView(self.bot, p_quests, week_start)
                embed = discord.Embed(
                    title="⚠️ 未完了クエストの確認",
                    description=(
                        "今週のクエストで未完了のものが残っています。\n"
                        "月曜 09:00 までに下のメニューから「辞退」を選択してください。\n"
                        "※回答がない場合、自動的に来週へ持ち越されます。"
                    ),
                    color=discord.Color.orange()
                )
                await send_dm_safely(user, embed=embed, view=view)
                logger.info(f"📨 未完了クエスト確認DM送信: {user.display_name} ({len(p_quests)}件)")

    # ============================================
    # 土曜日の自動声掛け
    # ============================================

    async def send_saturday_reminder(self):
        """
        土曜 09:00 にスケジューラから呼ばれる。
        今週のクエストで「完了」が1件もないメンバーにDMで声掛けする。
        """
        return  # [Task 10] 通知過多を避けるため一時凍結
        
        sheets = getattr(self.bot, "sheets", None)
        if not sheets:
            logger.warning("⚠️ 土曜リマインダー: Google Sheets 未接続のためスキップ")
            return

        try:
            week_start = sheets.get_current_week_start()
            all_quests = await sheets.get_weekly_quests(week_start)
        except Exception as e:
            logger.error(f"❌ 土曜リマインダー: クエスト取得エラー: {e}")
            return

        if not all_quests:
            logger.info("📭 土曜リマインダー: 今週のクエストがないためスキップ")
            return

        # メンバーごとの完了状況を集計
        member_status: dict[int, dict] = {}
        for q in all_quests:
            uid = q["discord_id"]
            if uid not in member_status:
                member_status[uid] = {"total": 0, "completed": 0, "quests": []}
            member_status[uid]["total"] += 1
            if q.get("status") == "完了":
                member_status[uid]["completed"] += 1
            elif q.get("status") in ["未着手", "進行中", ""]:
                member_status[uid]["quests"].append(q["quest_title"])

        reminded_count = 0
        for uid, status in member_status.items():
            if status["completed"] == 0 and status["total"] > 0:
                user = self.bot.get_user(uid)
                if not user:
                    continue

                quest_list = "\n".join(f"・{t}" for t in status["quests"][:5])

                embed = discord.Embed(
                    title="📋 今週のクエスト状況のお知らせ",
                    description=(
                        "今週のクエストがまだ完了していないようです。\n"
                        "もし困っていることがあれば、クエストDMの「❓ もっと詳しく」ボタンから\n"
                        "リーダーに気軽に質問できます！\n\n"
                        "難しい場合は「⏸️ 辞退する」も選べますので、無理せずご活用ください 🙏"
                    ),
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="📝 未完了のクエスト",
                    value=quest_list or "（情報を取得できませんでした）",
                    inline=False,
                )
                embed.set_footer(text="しんちょくん 🤖 | 週末リマインダー")

                await send_dm_safely(user, embed=embed)
                reminded_count += 1
                logger.info(f"📨 土曜リマインダー送信: {user.display_name}")

        logger.info(f"✅ 土曜リマインダー完了: {reminded_count}人に送信")


# ============================================
# 日曜用 未完タスク確認UI
# ============================================


class SummaryQuestDeclineModal(discord.ui.Modal, title="⏸️ クエストを辞退する"):
    reason = discord.ui.TextInput(
        label="辞退の理由",
        placeholder="例: キャパオーバーのため、タスクから外してほしいです。",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, quest_titles: list[str], week_start: str):
        super().__init__()
        self.bot = bot
        self.quest_titles = quest_titles
        self.week_start = week_start

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            for title in self.quest_titles:
                await self.bot.sheets.update_quest_status(
                    self.week_start, interaction.user.id, title, "辞退"
                )
        except Exception as e:
            logger.error(f"❌ 辞退ステータス更新エラー: {e}")
            await interaction.followup.send("⚠️ スプレッドシートの更新に失敗しました。", ephemeral=True)
            return

        titles_str = "\n".join(f"・{t}" for t in self.quest_titles)
        logger.info(f"⏸️ クエスト辞退: {interaction.user.display_name} が以下のクエストを辞退しました。\n{titles_str}\n理由: {self.reason.value}")

        await interaction.followup.send(f"✅ {len(self.quest_titles)}件のクエストを辞退しました。", ephemeral=True)


class UnfinishedQuestDMView(discord.ui.View):
    def __init__(self, bot: commands.Bot, quests: list[dict], week_start: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.quests = quests
        self.week_start = week_start
        self.selected_quest_titles = []

        options = [
            discord.SelectOption(label=q["quest_title"][:100], value=q["quest_title"])
            for q in quests
        ]
        
        # Select Menu with multiple selection
        select = discord.ui.Select(
            placeholder="辞退するクエストを選択（複数可）...", 
            options=options, 
            custom_id="unfin_quest_select",
            min_values=1,
            max_values=len(options)
        )
        select.callback = self.select_callback
        self.add_item(select)

        # Extend ALL Button (always enabled)
        self.extend_btn = discord.ui.Button(label="▶️ 全て来週に延長", style=discord.ButtonStyle.primary, custom_id="unfin_extend_btn")
        self.extend_btn.callback = self.extend_callback
        self.add_item(self.extend_btn)

        # Decline Button (Disabled initially)
        self.decline_btn = discord.ui.Button(label="⏸️ 選択したクエストを辞退", style=discord.ButtonStyle.danger, disabled=True, custom_id="unfin_decline_btn")
        self.decline_btn.callback = self.decline_callback
        self.add_item(self.decline_btn)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_quest_titles = interaction.data["values"]
        self.decline_btn.disabled = False
        await interaction.response.edit_message(view=self)

    async def extend_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        titles_str = "\n".join(f"・{q['quest_title']}" for q in self.quests)
        logger.info(f"▶️ クエスト延長: {interaction.user.display_name} が以下のクエストを延長しました。\n{titles_str}")

        await interaction.followup.send(
            "✅ 延長の意向を記録しました。\n月曜日にクエストが自動的に来週へ持ち越されます。",
            ephemeral=True
        )

    async def decline_callback(self, interaction: discord.Interaction):
        if not self.selected_quest_titles:
            return
        modal = SummaryQuestDeclineModal(self.bot, self.selected_quest_titles, self.week_start)
        await interaction.response.send_modal(modal)

async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))
