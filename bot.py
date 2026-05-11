import os
import discord
from discord import app_commands
import google.generativeai as genai
import asyncio
import json
import mimetypes
from pathlib import Path

# --- 1. Render用ダミーサーバー ---
from flask import Flask
from threading import Thread

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

# モデル定義
MODELS = {
    "gemini-3-flash-preview": {
        "display_name": "Gemini 3 Flash",
        "supports": ["text", "image", "video", "audio", "pdf"],
        "features": ["thinking", "grounding", "context_caching", "code_execution", "file_search"]
    },
    "gemini-3.1-pro-preview": {
        "display_name": "Gemini 3.1 Pro",
        "supports": ["text", "image", "video", "audio", "pdf"],
        "features": ["thinking", "grounding", "context_caching", "code_execution", "file_search"]
    }
}

THINKING_LEVELS = {
    "ミニマル": "minimal",
    "低": "low",
    "中": "medium",
    "高": "high"
}

# 設定ファイル
SETTINGS_FILE = "settings.json"

def load_settings():
    if Path(SETTINGS_FILE).exists():
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_settings(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

# 履歴ファイル
def get_history_file(channel_id):
    return f"history_{channel_id}.json"

def load_history(channel_id):
    history_file = get_history_file(channel_id)
    if Path(history_file).exists():
        with open(history_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_history(channel_id, history):
    history_file = get_history_file(channel_id)
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = MyClient()

# --- 3. スラッシュコマンドの実装 ---

# 設定変更コマンド
@client.tree.command(name="settings", description="Geminiの挙動を設定します")
@app_commands.choices(model=[
    app_commands.Choice(name="Gemini 3 Flash", value="gemini-3-flash-preview"),
    app_commands.Choice(name="Gemini 3.1 Pro", value="gemini-3.1-pro-preview"),
])
@app_commands.choices(thinking_level=[
    app_commands.Choice(name="ミニマル", value="ミニマル"),
    app_commands.Choice(name="低", value="低"),
    app_commands.Choice(name="中", value="中"),
    app_commands.Choice(name="高", value="高"),
])
@app_commands.describe(temperature="0.0(厳格)〜2.0(創造的)", thinking_level="Thinking Level")
async def settings(interaction: discord.Interaction, model: str = "gemini-3-flash-preview", temperature: float = 0.7, thinking_level: str = "中"):
    settings = load_settings()
    channel_id = str(interaction.channel_id)
    settings[channel_id] = {
        "model": model,
        "temperature": max(0.0, min(2.0, temperature)),
        "thinking_level": thinking_level,
        "thinking_mode": True  # デフォルトオン、必要に応じて変更
    }
    save_settings(settings)
    await interaction.response.send_message(
        f"設定を更新したよ！\nモデル: {MODELS[model]['display_name']}\nTemperature: {temperature}\nThinking Level: {thinking_level}",
        ephemeral=True
    )

# 履歴削除コマンド
@client.tree.command(name="clear", description="このチャンネルの履歴を削除します")
async def clear(interaction: discord.Interaction):
    channel_id = str(interaction.channel_id)
    history_file = get_history_file(channel_id)
    if Path(history_file).exists():
        Path(history_file).unlink()
    await interaction.response.send_message("履歴を削除したよ！", ephemeral=True)

# --- 4. メッセージ受信処理 ---

@client.event
async def on_message(message):
    if message.author == client.user: return

    channel_id = str(message.channel_id)
    settings = load_settings().get(channel_id, {
        "model": "gemini-3-flash-preview",
        "temperature": 0.7,
        "thinking_level": "中",
        "thinking_mode": True
    })

    model_name = settings["model"]
    model_info = MODELS[model_name]

    # 履歴読み込み
    history = load_history(channel_id)

    prompt = message.content.strip()

    # 添付ファイルの処理
    content_parts = []
    unsupported_files = []
    if message.attachments:
        for attachment in message.attachments:
            mime_type, _ = mimetypes.guess_type(attachment.filename)
            if mime_type:
                if any(mime_type.startswith(support) for support in ["image/", "video/", "audio/", "application/pdf"] if support in model_info["supports"]):
                    file_data = await attachment.read()
                    content_parts.append({
                        "mime_type": mime_type,
                        "data": file_data
                    })
                else:
                    unsupported_files.append(attachment.filename)
            else:
                unsupported_files.append(attachment.filename)

    if unsupported_files:
        prompt += f"\n\n非対応ファイル: {', '.join(unsupported_files)} は対応していません。"

    # 履歴をGemini形式に変換
    conversation = []
    for h in history[-10:]:  # 最新10件
        conversation.append({"role": "user", "parts": [h["user"]]})
        conversation.append({"role": "model", "parts": [h["bot"]]})

    conversation.append({"role": "user", "parts": content_parts if content_parts else [prompt]})

    # モデル設定
    generation_config = {
        "temperature": settings["temperature"],
    }
    if settings["thinking_mode"] and "thinking" in model_info["features"]:
        generation_config["thinking_config"] = {"thinking_budget": THINKING_LEVELS[settings["thinking_level"]]}

    tools = []
    if "grounding" in model_info["features"]:
        tools.append({"google_search_retrieval": {}})
    if "code_execution" in model_info["features"]:
        tools.append({"code_execution": {}})
    # 他のツールも追加可能

    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config=generation_config,
        tools=tools
    )

    chat = model.start_chat(history=conversation[:-1])  # 履歴を渡す

    async with message.channel.typing():
        try:
            # ストリーミングで送信
            response = await asyncio.to_thread(chat.send_message, conversation[-1]["parts"], stream=True)
            
            full_response = ""
            thinking_message = None
            thinking_count = 0
            
            async for chunk in response:
                if chunk.candidates:
                    for candidate in chunk.candidates:
                        if hasattr(candidate, 'content') and candidate.content.parts:
                            for part in candidate.content.parts:
                                if hasattr(part, 'text') and part.text:
                                    full_response += part.text
                                elif hasattr(part, 'function_call'):
                                    # 関数呼び出し処理
                                    pass
                                elif hasattr(part, 'thought') and settings["thinking_mode"]:
                                    # Thinking Modeのリアルタイム表示
                                    thinking_count += 1
                                    if thinking_message is None:
                                        thinking_message = await message.channel.send(f"思考中... {thinking_count}s")
                                    else:
                                        await thinking_message.edit(content=f"思考中... {thinking_count}s")
                                    await asyncio.sleep(1)  # 1秒待つ
            
            # Thinkingメッセージ削除
            if thinking_message:
                await thinking_message.delete()
            
            # 最終レスポンス送信
            await message.channel.send(full_response)
            
            # 履歴保存
            history.append({"user": prompt, "bot": full_response})
            save_history(channel_id, history)
        except Exception as e:
            await message.channel.send(f"エラー: {e}")

client.run(DISCORD_TOKEN)
