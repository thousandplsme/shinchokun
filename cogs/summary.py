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
from datetime import timedelta

import discord
from discord.ext import commands

import config
from database import now_jst
from sheets import SheetsClient
from utils.messaging import send_dm_safely

logger = logging.getLogger(__name__)


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
        """EXPランキングのEmbedを構築する"""
        embed = discord.Embed(
            title="📊 週間サマリー",
            description=f"**{week_start}** 〜 **{week_end}**",
            color=config.COLOR_GOLD,
        )
        weekly_exp_data = await self.bot.db.get_all_weekly_exp(week_start)
        if weekly_exp_data:
            ranking_lines = []
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            for i, data in enumerate(weekly_exp_data):
                user = self.bot.get_user(data["discord_id"])
                name = user.display_name if user else f"ID:{data['discord_id']}"
                medal = medals[i] if i < len(medals) else f"{i+1}."
                ranking_lines.append(f"{medal} **{name}** — +{data['weekly_exp']} EXP")
            embed.add_field(name="🏆 今週のEXPランキング", value="\n".join(ranking_lines), inline=False)
        else:
            embed.add_field(name="🏆 今週のEXPランキング", value="今週のEXPデータはありません。", inline=False)
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
                member_quest_map[uid]["total"] += 1
                if q.get("status", "") == "完了":
                    member_quest_map[uid]["completed"] += 1

            for uid, stats in member_quest_map.items():
                user = self.bot.get_user(uid)
                name = user.display_name if user else f"ID:{uid}"
                rate = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0
                filled = int(rate / 10)
                bar = "▓" * filled + "░" * (10 - filled)
                embed.add_field(
                    name=f"🔹 {name}",
                    value=f"[{bar}] **{rate:.0f}%**\n達成: {stats['completed']}/{stats['total']} クエスト",
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
            if q.get("status") in ["未着手", "進行中"]:
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
                        "月曜 09:00 までに下のメニューから「延長（持ち越し）」または「辞退」を選択してください。\n"
                        "※回答がない場合、自動的に「延長」として処理されます。"
                    ),
                    color=discord.Color.orange()
                )
                await send_dm_safely(user, embed=embed, view=view)


# ============================================
# 日曜用 未完タスク確認UI
# ============================================

class QuestExtendModal(discord.ui.Modal, title="▶️ クエストを来週に延長する"):
    reason = discord.ui.TextInput(
        label="延長の理由",
        placeholder="例: 他のタスクが重なり、来週に持ち越したいです。",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, quest_title: str, week_start: str):
        super().__init__()
        self.bot = bot
        self.quest_title = quest_title
        self.week_start = week_start

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 今週のステータスを「延長」に
            await self.bot.sheets.update_quest_status(
                self.week_start, interaction.user.id, self.quest_title, "延長"
            )
            # 来週に複製
            next_week = self.bot.sheets.get_next_week_start(self.week_start)
            await self.bot.sheets.duplicate_quest_to_next_week(
                self.week_start, interaction.user.id, self.quest_title, next_week
            )
        except Exception as e:
            logger.error(f"❌ 延長処理エラー: {e}")
            await interaction.followup.send("⚠️ スプレッドシートの更新に失敗しました。", ephemeral=True)
            return

            if leader := self.bot.get_user(int(config.LEADER_ID)) if config.LEADER_ID else None:
                embed = discord.Embed(
                    title="▶️ クエスト延長の通知",
                    description=f"**{interaction.user.display_name}** さんがクエストを来週に延長しました。",
                    color=discord.Color.orange()
                )
                embed.add_field(name="クエスト名", value=self.quest_title, inline=False)
                embed.add_field(name="理由", value=self.reason.value, inline=False)
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                await send_dm_safely(leader, embed=embed)

        await interaction.followup.send("✅ クエストを来週に延長し、リーダーに通知しました。", ephemeral=True)


class SummaryQuestDeclineModal(discord.ui.Modal, title="⏸️ クエストを辞退する"):
    reason = discord.ui.TextInput(
        label="辞退の理由",
        placeholder="例: キャパオーバーのため、タスクから外してほしいです。",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )

    def __init__(self, bot: commands.Bot, quest_title: str, week_start: str):
        super().__init__()
        self.bot = bot
        self.quest_title = quest_title
        self.week_start = week_start

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self.bot.sheets.update_quest_status(
                self.week_start, interaction.user.id, self.quest_title, "辞退"
            )
        except Exception as e:
            logger.error(f"❌ 辞退ステータス更新エラー: {e}")
            await interaction.followup.send("⚠️ スプレッドシートの更新に失敗しました。", ephemeral=True)
            return

            if leader := self.bot.get_user(int(config.LEADER_ID)) if config.LEADER_ID else None:
                embed = discord.Embed(
                    title="⏸️ クエスト辞退の通知",
                    description=f"**{interaction.user.display_name}** さんがクエストを辞退しました。",
                    color=discord.Color.red()
                )
                embed.add_field(name="クエスト名", value=self.quest_title, inline=False)
                embed.add_field(name="理由", value=self.reason.value, inline=False)
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                await send_dm_safely(leader, embed=embed)

        await interaction.followup.send("✅ クエストを辞退し、リーダーに通知しました。", ephemeral=True)


class UnfinishedQuestDMView(discord.ui.View):
    def __init__(self, bot: commands.Bot, quests: list[dict], week_start: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.week_start = week_start
        self.selected_quest_title = None

        options = [
            discord.SelectOption(label=q["quest_title"][:100], value=q["quest_title"])
            for q in quests
        ]
        
        # Select Menu
        select = discord.ui.Select(placeholder="対応する未完クエストを選択...", options=options, custom_id="unfin_quest_select")
        select.callback = self.select_callback
        self.add_item(select)

        # Buttons (Disabled initially)
        self.extend_btn = discord.ui.Button(label="▶️ 来週に延長する", style=discord.ButtonStyle.primary, disabled=True, custom_id="unfin_extend_btn")
        self.extend_btn.callback = self.extend_callback
        self.add_item(self.extend_btn)

        self.decline_btn = discord.ui.Button(label="⏸️ 辞退する", style=discord.ButtonStyle.danger, disabled=True, custom_id="unfin_decline_btn")
        self.decline_btn.callback = self.decline_callback
        self.add_item(self.decline_btn)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_quest_title = interaction.data["values"][0]
        self.extend_btn.disabled = False
        self.decline_btn.disabled = False
        await interaction.response.edit_message(view=self)

    async def extend_callback(self, interaction: discord.Interaction):
        if not self.selected_quest_title:
            return
        modal = QuestExtendModal(self.bot, self.selected_quest_title, self.week_start)
        await interaction.response.send_modal(modal)

    async def decline_callback(self, interaction: discord.Interaction):
        if not self.selected_quest_title:
            return
        modal = SummaryQuestDeclineModal(self.bot, self.selected_quest_title, self.week_start)
        await interaction.response.send_modal(modal)

async def setup(bot: commands.Bot):
    await bot.add_cog(SummaryCog(bot))
