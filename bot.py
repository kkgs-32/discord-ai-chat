import os
import discord
from discord import app_commands
import google.genai as genai
from google.genai import types
from google.genai.types import Tool
import asyncio
import json
import mimetypes
from pathlib import Path
import logging
from datetime import datetime

# --- 0. ログ設定 ---
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('api_log.txt', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    print(f"DISCORD_TOKEN set: {bool(DISCORD_TOKEN)}")
    print(f"GEMINI_API_KEY set: {bool(GEMINI_API_KEY)}")
    print("Environment variables DISCORD_TOKEN and GEMINI_API_KEY must be set")
    exit(1)

client_genai = genai.Client(api_key=GEMINI_API_KEY)

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
    channel_id = str(interaction.channel.id)
    settings[channel_id] = {
        "model": model,
        "temperature": max(0.0, min(2.0, temperature)),
        "thinking_level": thinking_level,
        "thinking_mode": False  # デフォルトオフ（トークン節約）
    }
    save_settings(settings)
    await interaction.response.send_message(
        f"設定を更新したよ！\nモデル: {MODELS[model]['display_name']}\nTemperature: {temperature}\nThinking Level: {thinking_level}",
        ephemeral=True
    )

# 履歴削除コマンド
@client.tree.command(name="clear", description="このチャンネルの履歴を削除します")
async def clear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    channel_id = str(interaction.channel.id)
    history_file = get_history_file(channel_id)
    deleted = False
    if Path(history_file).exists():
        Path(history_file).unlink()
        deleted = True

    purge_result = 0
    purge_error = None
    if hasattr(interaction.channel, 'purge'):
        try:
            def check(message):
                return message.author == client.user or message.author == interaction.user

            deleted_messages = await interaction.channel.purge(limit=1000, check=check)
            purge_result = len(deleted_messages)
        except discord.Forbidden:
            purge_error = '権限がありません。メッセージは削除できませんでした。'
        except Exception as e:
            purge_error = f'メッセージ削除中にエラーが発生しました: {e}'
            logger.error(f"clear command purge error: {e}")
    else:
        purge_error = 'このチャンネルでメッセージ削除がサポートされていません。'

    if purge_error:
        await interaction.followup.send(
            f"履歴ファイルを{'削除しました' if deleted else '見つけませんでした'}。{purge_error}",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"履歴ファイルを{'削除しました' if deleted else '見つけませんでした'}。チャットメッセージを {purge_result} 件削除しました。",
            ephemeral=True
        )

# --- 4. メッセージ受信処理 ---

@client.event
async def on_message(message):
    if message.author == client.user: return

    channel_id = str(message.channel.id)
    settings = load_settings().get(channel_id, {
        "model": "gemini-3-flash-preview",
        "temperature": 0.7,
        "thinking_level": "中",
        "thinking_mode": False
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

    # 履歴読み込み
    history = load_history(channel_id)

    # 履歴をGemini形式に変換（最新10件のみ使用してトークン節約）
    gemini_history = []
    for h in history[-10:]:  # 最新10件のみ
        gemini_history.append(types.Content(role="user", parts=[types.Part(text=h["user"])]))
        gemini_history.append(types.Content(role="model", parts=[types.Part(text=h["bot"])]))

    # user_partsの作成
    if content_parts:
        user_parts = [types.Part(inline_data=types.Blob(mime_type=part["mime_type"], data=part["data"])) for part in content_parts]
        if prompt:
            user_parts.insert(0, types.Part(text=prompt))
    else:
        user_parts = [types.Part(text=prompt)]

    # モデル設定
    config = types.GenerateContentConfig(
        temperature=settings["temperature"],
        thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_level=THINKING_LEVELS[settings["thinking_level"]]) if settings["thinking_mode"] and "thinking" in model_info["features"] else None,
    )

    tools = []
    # if "grounding" in model_info["features"]:
    #     tools.append(Tool(google_search=types.GoogleSearch()))
    # if tools:
    #     config.tools = tools

    # チャット作成
    chat = client_genai.chats.create(model=model_name, config=config, history=gemini_history)

    async with message.channel.typing():
        try:
            # リクエストログ
            logger.info(f"=== API Request ===")
            logger.info(f"Channel ID: {channel_id}")
            logger.info(f"Model: {model_name}")
            logger.info(f"Temperature: {settings['temperature']}")
            logger.info(f"Thinking Mode: {settings['thinking_mode']}")
            logger.info(f"User Message: {prompt[:200]}...")  # 最初の200文字のみ
            logger.info(f"Content Parts Count: {len(content_parts)}")
            if content_parts:
                for i, part in enumerate(content_parts):
                    logger.info(f"  Part {i}: mime_type={part['mime_type']}, size={len(part['data'])} bytes")
            
            # ストリーミングで送信
            response_stream = chat.send_message_stream(message=user_parts)
            
            full_response = ""
            thinking_message = None
            thinking_count = 0
            
            for chunk in response_stream:
                if chunk.text:
                    full_response += chunk.text
                # Thinkingの処理（chunkにthoughtがあれば）
                if hasattr(chunk, 'thought') and chunk.thought and settings["thinking_mode"]:
                    thinking_count += 1
                    if thinking_message is None:
                        thinking_message = await message.channel.send(f"思考中... {thinking_count}s")
                    else:
                        await thinking_message.edit(content=f"思考中... {thinking_count}s")
                    await asyncio.sleep(1)
            
            # レスポンスログ
            logger.info(f"=== API Response ===")
            logger.info(f"Status: Success")
            logger.info(f"Response Length: {len(full_response)} characters")
            logger.info(f"Response Preview: {full_response[:200]}...")  # 最初の200文字のみ
            logger.info(f"Thinking Count: {thinking_count}")
            
            # Thinkingメッセージ削除
            if thinking_message:
                await thinking_message.delete()
            
            # 最終レスポンス送信
            await message.channel.send(full_response)
            
            # 履歴保存
            history.append({"user": prompt, "bot": full_response})
            save_history(channel_id, history)
        except Exception as e:
            logger.error(f"=== API Error ===")
            logger.error(f"Error Type: {type(e).__name__}")
            logger.error(f"Error Message: {str(e)}")
            logger.error(f"Channel ID: {channel_id}")
            logger.error(f"Model: {model_name}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            await message.channel.send(f"エラー: {e}")

client.run(DISCORD_TOKEN)
