import os
import discord
from discord import app_commands
import google.generativeai as genai
import asyncio
from flask import Flask
from threading import Thread

# --- 1. Render用ダミーサーバー ---
app = Flask(__name__)
@app.route('/')
def home(): return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

Thread(target=run_web).start()

# --- 2. ボットとGeminiの設定 ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

# ユーザーごとの設定を保存する辞書
user_configs = {}

class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync() # スラッシュコマンドを同期

client = MyClient()

# --- 3. スラッシュコマンドの実装 ---

# 設定変更コマンド
@client.tree.command(name="config", description="Geminiの挙動を設定します")
@app_commands.choices(use_search=[
    app_commands.Choice(name="有効", value=1),
    app_commands.Choice(name="無効", value=0),
])
@app_commands.describe(temperature="0.0(厳格)〜1.0(創造的)", use_search="Google検索機能を使うか")
async def config(interaction: discord.Interaction, temperature: float = 0.7, use_search: int = 0):
    user_configs[interaction.user.id] = {
        "temperature": max(0.0, min(1.0, temperature)),
        "use_search": bool(use_search)
    }
    await interaction.response.send_message(
        f"設定を更新したよ！\nTemperature: {temperature}\nGoogle検索: {'有効' if use_search else '無効'}", 
        ephemeral=True
    )

# 履歴削除コマンド
@client.tree.command(name="clear", description="このチャンネルのメッセージを掃除します")
async def clear(interaction: discord.Interaction, amount: int = 10):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"{len(deleted)} 件のメッセージを削除したよ！", ephemeral=True)

# --- 4. メッセージ受信処理 (ファイル対応) ---

@client.event
async def on_message(message):
    if message.author == client.user: return
    if not client.user.mentioned_in(message): return

    # ユーザー設定の取得
    config = user_configs.get(message.author.id, {"temperature": 0.7, "use_search": False})
    
    # ツール設定（Google検索）
    tools = [{"google_search_retrieval": {}}] if config["use_search"] else []
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config={"temperature": config["temperature"]},
        tools=tools
    )

    prompt = message.content.replace(f'<@!{client.user.id}>', '').replace(f'<@{client.user.id}>', '').strip()
    
    # 添付ファイルの処理
    content_parts = [prompt if prompt else "このファイルを解析して"]
    if message.attachments:
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'pdf', 'txt']):
                file_data = await attachment.read()
                content_parts.append({
                    "mime_type": attachment.content_type,
                    "data": file_data
                })

    async with message.channel.typing():
        try:
            # Geminiに送信
            response = await asyncio.to_thread(model.generate_content, content_parts)
            # 5分後に自動消去
            await message.reply(response.text, delete_after=300)
        except Exception as e:
            await message.reply(f"エラー: {e}")

client.run(DISCORD_TOKEN)
