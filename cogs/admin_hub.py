"""
cogs/admin_hub.py — 管理者専用の統合コマンドハブ（v2.2 新規）

管理者向け機能を1つのメッセージに常設し、UIから直感的に操作できるようにします。
"""

import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from discord import app_commands

import config
from gamification import ExpManager, TitleSystem

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

# レア度定義 (色対応)
RARITY_COLORS = {
    "normal": {"name": "ノーマル", "color": 0x95A5A6, "label": "⚪ ノーマル (灰)"},
    "rare": {"name": "レア", "color": 0x3498DB, "label": "🔵 レア (青)"},
    "epic": {"name": "エピック", "color": 0x9B59B6, "label": "🟣 エピック (紫)"},
    "legendary": {"name": "レジェンド", "color": 0xF1C40F, "label": "🟡 レジェンド (金)"},
}

# ============================================
# 隠し称号付与 UIフロー
# ============================================

class HiddenTitleModal(discord.ui.Modal, title="🏅 隠し称号の付与"):
    title_name = discord.ui.TextInput(
        label="称号名",
        placeholder="例: 伝説のバグハンター",
        style=discord.TextStyle.short, required=True, max_length=50,
    )
    reason = discord.ui.TextInput(
        label="付与の理由（活躍内容）",
        placeholder="例: あの進行不能バグの原因を特定してくれました！",
        style=discord.TextStyle.paragraph, required=True, max_length=500,
    )
    exp_reward = discord.ui.TextInput(
        label="獲得EXP（数値のみ）",
        placeholder="例: 500",
        style=discord.TextStyle.short, required=True, max_length=10, default="100",
    )

    def __init__(self, bot: commands.Bot, target_user: discord.Member, rarity_key: str):
        super().__init__()
        self.bot = bot
        self.target_user = target_user
        self.rarity_key = rarity_key

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            exp_amount = int(self.exp_reward.value.strip())
        except ValueError:
            await interaction.followup.send("⚠️ 獲得EXPには数値を入力してください。", ephemeral=True)
            return

        rarity_info = RARITY_COLORS.get(self.rarity_key, RARITY_COLORS["normal"])
        role_name = f"称号: {self.title_name.value.strip()}"
        role_color = discord.Color(rarity_info["color"])

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("⚠️ サーバー内でのみ実行できます。", ephemeral=True)
            return

        # 1. ロールの作成または取得
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(
                    name=role_name, color=role_color, hoist=False, mentionable=False,
                    reason=f"しんちょくん: 隠し称号 '{self.title_name.value}'"
                )
                logger.info(f"✨ 隠し称号ロール作成: {role_name}")
            except Exception as e:
                logger.error(f"❌ ロール作成エラー: {e}")
                await interaction.followup.send("❌ ロールの作成に失敗しました。Manage Roles 権限を確認してください。", ephemeral=True)
                return

        # 2. ロールの付与
        try:
            if role not in self.target_user.roles:
                await self.target_user.add_roles(role, reason="しんちょくん: 隠し称号付与")
        except Exception as e:
            logger.error(f"❌ ロール付与エラー: {e}")
            await interaction.followup.send("❌ ロールの付与に失敗しました。", ephemeral=True)
            return

        # 3. EXPの付与とログ記録
        try:
            if self.bot.sheets:
                await self.bot.sheets.append_exp_log(
                    discord_id=self.target_user.id,
                    amount=exp_amount,
                    reason=f"隠し称号獲得: {self.title_name.value}"
                )
            
            exp_mgr = ExpManager(self.bot.db, self.bot.sheets)
            await exp_mgr.ensure_profile(self.target_user.id)
            profile = await self.bot.db.get_user_profile(self.target_user.id)
            if profile:
                new_exp = profile["total_exp"] + exp_amount
                # 隠し称号はメインの称号(Job/Level)は上書きせず、純粋にEXPだけ加算する
                await self.bot.db.update_exp(self.target_user.id, new_exp, profile["current_title"], profile["level"])
        except Exception as e:
            logger.error(f"❌ EXP付与エラー: {e}")
            # 継続してアナウンスは送る

        # 4. 全体アナウンス
        channel = guild.get_channel(config.PROGRESS_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🎉 隠し称号 実績解除！",
                description=f"{self.target_user.mention} さんが新たな実績を解除し、称号を獲得しました！",
                color=role_color,
            )
            embed.add_field(name="🏅 獲得称号", value=f"**『{self.title_name.value}』**\n(レア度: {rarity_info['name']})", inline=False)
            embed.add_field(name="📝 活躍内容", value=self.reason.value, inline=False)
            embed.add_field(name="🎁 ボーナス", value=f"**+{exp_amount} EXP**", inline=False)
            embed.set_thumbnail(url=self.target_user.display_avatar.url)
            embed.set_footer(text="しんちょくん 🤖 | 隠し称号システム")

            await channel.send(content=f"{self.target_user.mention} おめでとうございます！", embed=embed)

        await interaction.followup.send(f"✅ {self.target_user.display_name} さんに隠し称号を付与し、アナウンスを送信しました。", ephemeral=True)


class HiddenTitleSetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot
        self.selected_user: discord.Member | None = None
        self.selected_rarity: str | None = None

        # ユーザー選択
        self.user_select = discord.ui.UserSelect(
            placeholder="称号を付与するメンバーを選択",
            custom_id="hidden_title_user_select"
        )
        self.user_select.callback = self.user_callback
        self.add_item(self.user_select)

        # レア度選択
        options = [
            discord.SelectOption(label=r["label"], value=k)
            for k, r in RARITY_COLORS.items()
        ]
        self.rarity_select = discord.ui.Select(
            placeholder="称号のレア度を選択",
            options=options,
            custom_id="hidden_title_rarity_select"
        )
        self.rarity_select.callback = self.rarity_callback
        self.add_item(self.rarity_select)

        # 決定ボタン
        self.confirm_btn = discord.ui.Button(
            label="詳細を入力して付与",
            style=discord.ButtonStyle.primary,
            disabled=True,
            custom_id="hidden_title_confirm_btn"
        )
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def user_callback(self, interaction: discord.Interaction):
        self.selected_user = self.user_select.values[0]
        await self._update_view(interaction)

    async def rarity_callback(self, interaction: discord.Interaction):
        self.selected_rarity = self.rarity_select.values[0]
        await self._update_view(interaction)

    async def _update_view(self, interaction: discord.Interaction):
        if self.selected_user and self.selected_rarity:
            self.confirm_btn.disabled = False
        await interaction.response.edit_message(view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        if not self.selected_user or not self.selected_rarity:
            return
        modal = HiddenTitleModal(self.bot, self.selected_user, self.selected_rarity)
        await interaction.response.send_modal(modal)


# ============================================
# メンバー選択用 View (個別クエスト配信用)
# ============================================

class SendQuestSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot
        
        self.user_select = discord.ui.UserSelect(
            placeholder="クエストを追加配信するメンバーを選択",
            custom_id="send_quest_user_select"
        )
        self.user_select.callback = self.user_callback
        self.add_item(self.user_select)

    async def user_callback(self, interaction: discord.Interaction):
        target = self.user_select.values[0]
        commands_cog = self.bot.get_cog("CommandsCog")
        if commands_cog and hasattr(commands_cog, "send_quest"):
            # commands.py の send_quest コマンド処理を流用
            await commands_cog.send_quest.callback(commands_cog, interaction, target)
        else:
            await interaction.response.send_message("⚠️ CommandsCog が読み込まれていません。", ephemeral=True)

# ============================================
# Admin Hub View
# ============================================

class AdminHubView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        # 常設なのでタイムアウトなし
        super().__init__(timeout=None)
        self.bot = bot

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⚠️ この操作は管理者専用です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🏅 隠し称号の付与", style=discord.ButtonStyle.success, custom_id="admin_hub_title", row=0)
    async def hidden_title_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        view = HiddenTitleSetupView(self.bot)
        await interaction.response.send_message("👤 **隠し称号の付与設定**\n付与するメンバーとレア度を選択してください。", view=view, ephemeral=True)

    @discord.ui.button(label="⚔️ クエスト一斉配信", style=discord.ButtonStyle.primary, custom_id="admin_hub_test_quest", row=0)
    async def test_quest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        quest_cog = self.bot.get_cog("QuestCog")
        if quest_cog:
            await interaction.response.send_message("⏳ クエスト配信を開始します...", ephemeral=True)
            await quest_cog.deliver_weekly_quests()
            await interaction.edit_original_response(content="✅ クエスト配信を実行しました。")
        else:
            await interaction.response.send_message("⚠️ QuestCogが読み込まれていません。", ephemeral=True)

    @discord.ui.button(label="📩 個別クエスト配信", style=discord.ButtonStyle.secondary, custom_id="admin_hub_send_quest", row=0)
    async def send_quest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        view = SendQuestSelectView(self.bot)
        await interaction.response.send_message("👤 **追加配信**\nクエストを追加配信するメンバーを選んでください。", view=view, ephemeral=True)

    @discord.ui.button(label="📅 サマリー投稿", style=discord.ButtonStyle.primary, custom_id="admin_hub_test_summary", row=1)
    async def test_summary_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        summary_cog = self.bot.get_cog("SummaryCog")
        if summary_cog:
            await interaction.response.send_message("⏳ サマリーを投稿します...", ephemeral=True)
            await summary_cog.post_weekly_summary()
            await interaction.edit_original_response(content="✅ サマリーを投稿しました。")
        else:
            await interaction.response.send_message("⚠️ SummaryCogが読み込まれていません。", ephemeral=True)

    @discord.ui.button(label="🔄 データ同期", style=discord.ButtonStyle.danger, custom_id="admin_hub_sync", row=1)
    async def sync_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_admin(interaction): return
        commands_cog = self.bot.get_cog("CommandsCog")
        if commands_cog:
            await interaction.response.send_message("⏳ データ同期（EXP・ロール）を実行中...", ephemeral=True)
            # sync_exp の呼び出し（Interactionを直接渡すと重複応答エラーになるため、内部で処理を分けるかラップするべきですが、簡単のため直接コールバック呼び出し）
            # sync_exp / sync_roles は内部で followup/response.send を呼ぶため、一括ボタンにする場合は注意が必要。
            # ここでは別々に選べる View を返す方が安全です。
            
            view = AdminSyncSelectView(self.bot)
            await interaction.edit_original_response(content="🔄 同期する項目を選択してください", view=view)
        else:
            await interaction.response.send_message("⚠️ CommandsCogが読み込まれていません。", ephemeral=True)


class AdminSyncSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="EXP同期", style=discord.ButtonStyle.primary)
    async def sync_exp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        commands_cog = self.bot.get_cog("CommandsCog")
        if commands_cog:
            await commands_cog.sync_exp.callback(commands_cog, interaction)

    @discord.ui.button(label="ロール同期", style=discord.ButtonStyle.primary)
    async def sync_roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        commands_cog = self.bot.get_cog("CommandsCog")
        if commands_cog:
            await commands_cog.sync_roles.callback(commands_cog, interaction)


# ============================================
# Admin Hub Cog
# ============================================

class AdminHubCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup_admin_hub", description="管理者専用チャンネルに管理用ハブを設置します")
    @app_commands.default_permissions(administrator=True)
    async def setup_admin_hub(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="👑 しんちょくん 管理者センター",
            description=(
                "管理者向けの機能一覧です。ボタンを押して操作してください。\n"
                "*(このパネルは管理者のみ操作可能です)*\n\n"
                "🏅 **隠し称号の付与** — 特別な称号をメンバーに与えます\n"
                "⚔️ **クエスト一斉配信** — 今週のクエストを全員に配信します\n"
                "📩 **個別クエスト配信** — 指定メンバーにプール分を配信します\n"
                "📅 **サマリー投稿** — 週次サマリーを手動で投稿します\n"
                "🔄 **データ同期** — スプレッドシートとDBのEXP/称号を同期します"
            ),
            color=config.COLOR_DANGER,
        )
        embed.set_footer(text="しんちょくん 🤖 | 管理者センター")

        view = AdminHubView(self.bot)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ 管理者ハブを設置しました！", ephemeral=True)
        logger.info("✅ 管理者ハブ設置完了")


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminHubCog(bot))
