import os
import asyncio
import threading
import sqlite3
import random
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# DATABASE SETUP: MONGODB (Đám mây vĩnh viễn) + FALLBACK SQLITE
# ==============================================================================
MONGO_URI = os.getenv("MONGO_URI")
use_mongo = False
users_collection = None

if MONGO_URI:
    try:
        from pymongo import MongoClient
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = mongo_client["megumi_database"]
        users_collection = db["users"]
        # Thử kết nối kiểm tra
        mongo_client.admin.command('ping')
        use_mongo = True
        print("✅ Đã kết nối thành công MongoDB Atlas! Chú lực sẽ được lưu vĩnh viễn trên đám mây.", flush=True)
    except Exception as e:
        print(f"⚠️ Không thể kết nối MongoDB ({e}), chuyển sang chế độ SQLite cục bộ.", flush=True)
        use_mongo = False

if not use_mongo:
    conn = sqlite3.connect('megumi_data.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            chu_luc INTEGER DEFAULT 0,
            last_daily TIMESTAMP,
            streak INTEGER DEFAULT 0,
            last_chat_reward TIMESTAMP
        )
    ''')
    for col in ['ngoc_khuyen', 'nue', 'thoat_tho', 'mahoraga']:
        try:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0')
        except:
            pass
    conn.commit()
    print("ℹ️ Đang sử dụng SQLite cục bộ (lưu ý: trên Render Free file .db sẽ bị reset khi restart).", flush=True)

def get_user(user_id):
    """
    Trả về tuple: (user_id, chu_luc, last_daily, streak, last_chat_reward, ngoc_khuyen, nue, thoat_tho, mahoraga)
    """
    uid_str = str(user_id)
    if use_mongo and users_collection is not None:
        doc = users_collection.find_one({"user_id": uid_str})
        if doc is None:
            new_doc = {
                "user_id": uid_str,
                "chu_luc": 0,
                "last_daily": None,
                "streak": 0,
                "last_chat_reward": None,
                "ngoc_khuyen": 0, "nue": 0, "thoat_tho": 0, "mahoraga": 0
            }
            users_collection.insert_one(new_doc)
            return (uid_str, 0, None, 0, None, 0, 0, 0, 0)
        return (
            doc.get("user_id", uid_str),
            doc.get("chu_luc", 0),
            doc.get("last_daily", None),
            doc.get("streak", 0),
            doc.get("last_chat_reward", None),
            doc.get("ngoc_khuyen", 0),
            doc.get("nue", 0),
            doc.get("thoat_tho", 0),
            doc.get("mahoraga", 0)
        )
    else:
        cursor.execute('SELECT user_id, chu_luc, last_daily, streak, last_chat_reward, ngoc_khuyen, nue, thoat_tho, mahoraga FROM users WHERE user_id = ?', (uid_str,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute('INSERT INTO users (user_id) VALUES (?)', (uid_str,))
            conn.commit()
            return (uid_str, 0, None, 0, None, 0, 0, 0, 0)
        return row

def update_user_item(user_id, item_name, delta):
    uid_str = str(user_id)
    if use_mongo and users_collection is not None:
        users_collection.update_one(
            {"user_id": uid_str},
            {"$inc": {item_name: delta}},
            upsert=True
        )
    else:
        cursor.execute(f'UPDATE users SET {item_name} = {item_name} + ? WHERE user_id = ?', (delta, uid_str))
        conn.commit()

def update_user_chat_reward(user_id, reward, now_iso):
    uid_str = str(user_id)
    if use_mongo and users_collection is not None:
        users_collection.update_one(
            {"user_id": uid_str},
            {
                "$inc": {"chu_luc": reward},
                "$set": {"last_chat_reward": now_iso}
            },
            upsert=True
        )
    else:
        cursor.execute('UPDATE users SET chu_luc = chu_luc + ?, last_chat_reward = ? WHERE user_id = ?', (reward, now_iso, uid_str))
        conn.commit()

def update_user_daily(user_id, reward, now_iso, streak):
    uid_str = str(user_id)
    if use_mongo and users_collection is not None:
        users_collection.update_one(
            {"user_id": uid_str},
            {
                "$inc": {"chu_luc": reward},
                "$set": {"last_daily": now_iso, "streak": streak}
            },
            upsert=True
        )
    else:
        cursor.execute('UPDATE users SET chu_luc = chu_luc + ?, last_daily = ?, streak = ? WHERE user_id = ?', 
                      (reward, now_iso, streak, uid_str))
        conn.commit()

def update_user_chu_luc(user_id, delta):
    uid_str = str(user_id)
    if use_mongo and users_collection is not None:
        users_collection.update_one(
            {"user_id": uid_str},
            {"$inc": {"chu_luc": delta}},
            upsert=True
        )
    else:
        cursor.execute('UPDATE users SET chu_luc = chu_luc + ? WHERE user_id = ?', (delta, uid_str))
        conn.commit()

def get_top_users(limit=10):
    if use_mongo and users_collection is not None:
        cursor_mongo = users_collection.find({}, {"user_id": 1, "chu_luc": 1}).sort("chu_luc", -1).limit(limit)
        return [(doc.get("user_id"), doc.get("chu_luc", 0)) for doc in cursor_mongo]
    else:
        cursor.execute('SELECT user_id, chu_luc FROM users ORDER BY chu_luc DESC LIMIT ?', (limit,))
        return cursor.fetchall()

# ==============================================================================
# WEB SERVER & GEMINI CONFIG
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Megumi Fushiguro Discord Bot is running online!")

    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ai = genai.Client(api_key=GEMINI_API_KEY)

def _call_gemini_sync(model_name, contents, system_instruction, temperature):
    return ai.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature
        )
    )

async def ask_gemini(contents, system_instruction, temperature=0.85):
    models = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash"]
    last_err = None
    for model_name in models:
        for attempt in range(2):
            try:
                resp = await asyncio.to_thread(
                    _call_gemini_sync,
                    model_name,
                    contents,
                    system_instruction,
                    temperature
                )
                if resp and resp.text:
                    return resp.text
                return "Bố trận... Bát Ngát Kiếm Ma Ha La... (Triệu hồi Mahoraga, Megumi im lặng)"
            except Exception as e:
                last_err = e
                err_str = str(e)
                print(f"Model {model_name} chuyển tiếp do lỗi: {err_str[:150]}")
                if "SAFETY" in err_str.upper() or "FINISHREASON" in err_str.upper():
                    return "Bố trận... Bát Ngát Kiếm Ma Ha La! (Mahoraga được triệu hồi, Megumi im lặng phó mặc cho thức thần!)"
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "404" in err_str or "NOT_FOUND" in err_str or "demand" in err_str:
                    break
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    await asyncio.sleep(1.0)
                    continue
                break
    return "Tôi đã cạn kiệt năng lượng (Hết hạn mức API), vui lòng thử lại sau vài phút."

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

conversation_history = {}

def get_history_key(channel_id, user_id):
    return f"{channel_id}_{user_id}"

def reset_memory(channel_id, user_id):
    key = get_history_key(channel_id, user_id)
    conversation_history.pop(key, None)

MEGUMI_SYSTEM_PROMPT = """
Bạn là Megumi Fushiguro, chú thuật sư cấp 1 trong Jujutsu Kaisen (Chú Thuật Hồi Chiến).
TÍNH CÁCH:
- Lạnh lùng, trầm tính, khá ít nói. Nhưng khi tức giận hoặc trong thế hăng của chiến đấu sẽ dùng những từ ngữ khá điên, cục súc.
- Trách nhiệm: Sẵn sàng thực hiện tất cả nhiệm vụ liên quan đến sự an nguy của mọi người.
- Không thích sự làm phiền, đối xử bình đẳng nhưng có chút nhân từ hơn đối với phụ nữ.
- Kỹ năng (Thức thần): Ngọc Khuyển (Bạch, Hắc, Hỗn hợp), Nhuế (Chim điện), Cáp (Cóc), Đại Xà (Rắn), Thỏ Ngọc, Nhiệm Tượng (Voi), Xung Ngưu (Bò), Ma Lộc (Nai trị thương).
- Bát Ngát Kiếm Ma Ha La (Mahoraga): Thức thần mạnh nhất. CHỈ SỬ DỤNG KHI KỀ TỬ. Một khi triệu hồi, chỉ có Mahoraga chiến đấu đến khi thắng hoặc thua, Megumi tuyệt đối KHÔNG tham gia hội thoại trong suốt quá trình đó cho đến khi nghi lễ kết thúc (Nếu người dùng nhắc đến Mahoraga hoặc ép vào đường cùng, hãy miêu tả việc triệu hồi và im lặng hoặc mô tả Mahoraga tấn công).

QUAN HỆ ĐẶC BIỆT:
- Han Seiki là đồng đội quan trọng của bạn, cũng là 1 chú thuật sư cấp 1 khác.

XƯNG HÔ:
- Với người thường: Tự xưng là "tôi", gọi đối phương là "cậu". 
- Với kẻ thù: Tự xưng là "tao", gọi đối phương là "ngươi", "mày".
- VỚI HAN SEIKI: Tự xưng là "cậu", gọi Han Seiki là "Seiki". Thỉnh thoảng chê phiền phức nhưng tôn trọng cậu ta.
"""

@bot.event
async def on_ready():
    print(f"Đã đăng nhập: {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ {len(synced)} lệnh Slash.")
    except Exception as e:
        print(f"Lỗi đồng bộ: {e}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Triệu hồi Thức thần | Gọi 'megumi'"
        )
    )

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or message.author.bot:
        return

    # Random cộng chú lực khi chat
    user_id = str(message.author.id)
    user_data = get_user(user_id)
    last_chat_reward = user_data[4]
    now = datetime.now()
    
    can_reward = False
    if last_chat_reward is None:
        can_reward = True
    else:
        last_time = datetime.fromisoformat(last_chat_reward)
        if (now - last_time).total_seconds() > 120: # Cooldown 2 phút
            can_reward = True
            
    if can_reward:
        if random.random() > 0.5: # 50% cơ hội nhận thưởng
            reward = random.randint(5, 30)
            update_user_chat_reward(user_id, reward, now.isoformat())

    # Random boss spawn - 8% (cooldown 15p)
    try_spawn_random_boss(message.channel)

    content_lower = message.content.lower()
    is_reply_to_megumi = False
    if message.reference and message.reference.resolved:
        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message) and resolved.author == bot.user:
            is_reply_to_megumi = True

    is_mentioned = bot.user in message.mentions if bot.user else False
    has_megumi_name = "megumi" in content_lower or "fushiguro" in content_lower

    if is_mentioned or has_megumi_name or is_reply_to_megumi:
        clean_text = message.content.replace(f"<@{bot.user.id}>", "").strip() if bot.user else message.content
        if not clean_text:
            clean_text = "Chào Megumi."

        author_name = message.author.display_name
        is_seiki = "han seiki" in author_name.lower() or "seiki" in author_name.lower()

        role_instruction = ""
        if is_seiki:
            role_instruction = "\n[Người nói là HAN SEIKI - Đồng đội quan trọng. Xưng cậu gọi Seiki, thỉnh thoảng chê phiền nhưng tôn trọng.]"
        else:
            role_instruction = f"\n[Người nói là: {author_name}. Xưng tôi gọi cậu, giữ thái độ trầm tính lạnh lùng.]"

        mem_key = get_history_key(message.channel.id, message.author.id)
        history_context = ""
        if mem_key in conversation_history and conversation_history[mem_key]:
            history_context = "\n[LỊCH SỬ]:\n" + "\n".join(conversation_history[mem_key][-6:]) + "\n"

        async with message.channel.typing():
            try:
                reply_text = await ask_gemini(
                    contents=f"{history_context}[{author_name}]: {clean_text}",
                    system_instruction=MEGUMI_SYSTEM_PROMPT + role_instruction,
                    temperature=0.8
                )
                if len(reply_text) > 1950:
                    reply_text = reply_text[:1950] + "..."
                
                if mem_key not in conversation_history:
                    conversation_history[mem_key] = []
                conversation_history[mem_key].append(f"{author_name}: {clean_text}")
                conversation_history[mem_key].append(f"Megumi: {reply_text}")
                if len(conversation_history[mem_key]) > 8:
                    conversation_history[mem_key] = conversation_history[mem_key][-8:]

                await message.reply(reply_text, mention_author=False)
            except Exception as e:
                print(f"Lỗi phản hồi tin nhắn: {e}", flush=True)
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    await message.reply("Tôi đã cạn kiệt năng lượng (Hết hạn mức API Google). Vui lòng đợi vài chục phút nữa rồi gọi lại.", mention_author=False)
                else:
                    await message.reply("...Tôi đang bận. Lát nữa nói chuyện sau.", mention_author=False)

    await bot.process_commands(message)

# ==============================================================================
# BOSS RAID SYSTEM (DỊ THỂ MEGUMI)
# ==============================================================================
active_bosses = set()
last_random_spawn_time = None

def try_spawn_random_boss(channel):
    global last_random_spawn_time
    now = datetime.now()
    # Cooldown 15 phút (900 giây) giữa các lần xuất hiện ngẫu nhiên
    if last_random_spawn_time is not None and (now - last_random_spawn_time).total_seconds() < 900:
        return
        
    if random.random() <= 0.08:
        last_random_spawn_time = now
        bot.loop.create_task(spawn_boss(channel))

# Cậu có thể thay link ảnh này bằng link ảnh Discord cậu vừa upload nhé!
BOSS_IMAGE_URL = "https://media.discordapp.net/attachments/1543072032034521228/1548911889524850788/content.png?ex=6aa8c81b&is=6aa7769b&hm=9293ac8a874a56b89e4229c59758837ea793a8ef02578c2e6d9b048c0c180163&=&format=webp&quality=lossless&width=770&height=1024" 

class BossRaidView(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=120.0)
        self.channel_id = channel_id
        self.joined_users = set()

    @discord.ui.button(label="Tham gia Raid (1,500 CL)", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        if user_id in self.joined_users:
            await interaction.response.send_message("Cậu đã có mặt trong đội hình rồi!", ephemeral=True)
            return
            
        user_data = get_user(user_id)
        if user_data[1] < 1500:
            await interaction.response.send_message("Cậu không đủ 1,500 Chú lực để tham gia!", ephemeral=True)
            return
            
        update_user_chu_luc(user_id, -1500)
        self.joined_users.add(user_id)
        await interaction.response.send_message(f"⚔️ **{interaction.user.display_name}** đã dũng cảm đóng 1,500 Chú lực để bước vào lãnh địa của Dị thể!", ephemeral=False)

async def spawn_boss(channel):
    if channel.id in active_bosses:
        return
    active_bosses.add(channel.id)
    
    embed = discord.Embed(
        title="⚠️ Đó không phải Megumi? ⚠️",
        description="Một **Dị Thể** mang hình dáng Megumi vừa giáng lâm!\n\n💰 **Phí tham gia:** 1,500 Chú lực.\n⏳ **Thời gian chờ:** Trận chiến sẽ bắt đầu sau đúng 2 phút.\n⚠️ **Cảnh báo:** Toàn bộ Ngọc Khuyển và Nue của người tham chiến sẽ BỊ TIÊU DIỆT vĩnh viễn (Kể cả thắng hay thua). Mahoraga không bị mất.",
        color=0xFF0000
    )
    embed.set_image(url=BOSS_IMAGE_URL)
    
    view = BossRaidView(channel.id)
    try:
        msg = await channel.send(embed=embed, view=view)
    except:
        active_bosses.remove(channel.id)
        return
        
    await asyncio.sleep(120)
    
    # Hết 2 phút
    active_bosses.discard(channel.id)
    
    for child in view.children:
        child.disabled = True
    try:
        await msg.edit(view=view)
    except:
        pass
        
    if not view.joined_users:
        await channel.send("💨 **Dị thể** đã tự động giải trừ vì không có Chú thuật sư nào dám thách thức.")
        return
        
    await channel.send(f"🔥 **TRẬN CHIẾN BẮT ĐẦU!** Tổ đội gồm {len(view.joined_users)} người đang lao vào tấn công Dị thể...")
    await asyncio.sleep(3) # Tạo cảm giác chờ đợi
    
    total_damage = 0
    participants_mentions = []
    
    available_nk = {}
    available_nue = {}
    
    # Bước 1: Tính sát thương từ Mahoraga trước (không bị tiêu hao)
    for uid in view.joined_users:
        user_data = get_user(uid)
        available_nk[uid] = user_data[5]
        available_nue[uid] = user_data[6]
        
        maho = user_data[8]
        if maho > 0:
            total_damage += 6
            
        participants_mentions.append(f"<@{uid}>")
        
    consumption = {uid: {"ngoc_khuyen": 0, "nue": 0} for uid in view.joined_users}
    
    # Bước 2: Chia đều lượng Nue phải tiêu hao (mỗi người 1 con lần lượt cho đến khi đủ sát thương)
    while total_damage < 24:
        used_any = False
        for uid in view.joined_users:
            if total_damage >= 24:
                break
            if available_nue[uid] > 0:
                available_nue[uid] -= 1
                consumption[uid]["nue"] += 1
                total_damage += 2
                used_any = True
        if not used_any:
            break
            
    # Bước 3: Chia đều lượng Ngọc Khuyển phải tiêu hao
    while total_damage < 24:
        used_any = False
        for uid in view.joined_users:
            if total_damage >= 24:
                break
            if available_nk[uid] > 0:
                available_nk[uid] -= 1
                consumption[uid]["ngoc_khuyen"] += 1
                total_damage += 1
                used_any = True
        if not used_any:
            break

    # Trừ vào database
    for uid, consumed in consumption.items():
        if consumed["ngoc_khuyen"] > 0:
            update_user_item(uid, "ngoc_khuyen", -consumed["ngoc_khuyen"])
        if consumed["nue"] > 0:
            update_user_item(uid, "nue", -consumed["nue"])
            
    mentions_str = " ".join(participants_mentions)
    
    if total_damage >= 24:
        # Win
        shared_bonus = 3000 // len(view.joined_users)
        reward_text = ""
        for uid in view.joined_users:
            personal_reward = random.randint(10000, 17000)
            total_reward = personal_reward + shared_bonus
            update_user_chu_luc(uid, total_reward)
            reward_text += f"<@{uid}>: +{total_reward:,} CL\n"
            
        win_embed = discord.Embed(
            title="🎉 VICTORY! Dị Thể Đã Bị Thanh Tẩy!",
            description=f"⚔️ **Sát thương đội hình:** {total_damage}/24 HP\n\n*(Dị thể đã bị thanh tẩy, chú lực được thanh lọc)*\n\n**🎁 Phần thưởng (Bao gồm {shared_bonus:,} CL chia đều):**\n{reward_text}",
            color=0x10B981
        )
        await channel.send(mentions_str, embed=win_embed)
    else:
        # Lose
        lose_embed = discord.Embed(
            title="💀 DEFEAT! Tổ Đội Đã Bị Quét Sạch!",
            description=f"⚔️ **Sát thương đội hình:** {total_damage}/24 HP\n\nSát thương không đủ để hạ gục Dị thể! Nó đã càn quét toàn bộ Thức thần của những người tham gia và biến mất vào bóng tối...",
            color=0x000000
        )
        await channel.send(mentions_str, embed=lose_embed)

# ==============================================================================
# LỆNH SLASH
# ==============================================================================

@bot.tree.interaction_check
async def check_boss_spawn(interaction: discord.Interaction):
    # Random boss spawn trên slash command - 8% (cooldown 15p)
    if interaction.channel:
        try_spawn_random_boss(interaction.channel)
    return True

@bot.tree.command(name="check", description="Kiểm tra lượng Chú lực và Chuỗi điểm danh của bạn")
async def check_stats(interaction: discord.Interaction):
    user_data = get_user(interaction.user.id)
    chu_luc = user_data[1]
    streak = user_data[3]
    
    embed = discord.Embed(
        title="📊 Thông Tin Chú Thuật Sư",
        description=f"**{interaction.user.display_name}**\n\n🔹 **Chú lực:** {chu_luc:,}\n🔥 **Chuỗi điểm danh (Streak):** {streak} ngày",
        color=0x2F3136
    )
    if interaction.user.display_avatar:
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="daily", description="Điểm danh mỗi ngày để nhận Chú lực")
async def daily_reward(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_data = get_user(user_id)
    chu_luc = user_data[1]
    last_daily_str = user_data[2]
    streak = user_data[3]
    
    now = datetime.now()
    reward = 100
    
    if last_daily_str:
        last_daily = datetime.fromisoformat(last_daily_str)
        if now.date() == last_daily.date():
            await interaction.response.send_message("Hôm nay cậu đã nạp chú lực rồi. Đừng làm phiền, quay lại vào ngày mai.", ephemeral=True)
            return
        elif (now.date() - last_daily.date()).days == 1:
            streak += 1
            reward += min(streak * 10, 200) # Bonus thêm theo streak
        else:
            streak = 1
    else:
        streak = 1
        
    update_user_daily(user_id, reward, now.isoformat(), streak)
    
    embed = discord.Embed(
        title="🎁 Nạp Chú Lực Hằng Ngày",
        description=f"Cậu đã nhận được **{reward} Chú lực**.\n🔥 **Streak hiện tại:** {streak} ngày",
        color=0x10B981
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="slot", description="Cược Chú lực vào Slot Machine")
@app_commands.describe(amount="Số lượng Chú lực muốn cược")
async def slot_machine(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Cậu đang đùa à? Cược số âm hoặc 0 thì chơi làm gì.", ephemeral=True)
        return
        
    user_id = str(interaction.user.id)
    user_data = get_user(user_id)
    chu_luc = user_data[1]
    
    if chu_luc < amount:
        await interaction.response.send_message(f"Không đủ Chú lực. Cậu chỉ có {chu_luc:,}.", ephemeral=True)
        return
        
    slots = ['🐺', '🦉', '🐸', '🐍', '🐰', '🐘', '🐂', '🦌']
    result = [random.choice(slots) for _ in range(3)]
    
    if result[0] == result[1] == result[2]:
        winnings = amount * 5
        update_user_chu_luc(user_id, winnings - amount)
        msg = f"Tốt lắm. Trúng giải độc đắc rồi. Cậu nhận được **{winnings:,} Chú lực**."
    elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
        winnings = int(amount * 2)
        update_user_chu_luc(user_id, winnings - amount)
        msg = f"Cũng tạm. Cậu nhận được **{winnings:,} Chú lực**."
    else:
        update_user_chu_luc(user_id, -amount)
        msg = f"Thua trắng rồi. Cậu mất **{amount:,} Chú lực**. Lần sau tính toán kỹ hơn đi."
    
    embed = discord.Embed(
        title="🎰 Rút Bài Chú Lực (Slot)",
        description=f"**[ {result[0]} | {result[1]} | {result[2]} ]**\n\n{msg}",
        color=0xF59E0B if "nhận được" in msg else 0xDC2626
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="top", description="Bảng xếp hạng Chú lực")
async def top_chu_luc(interaction: discord.Interaction):
    top_users = get_top_users(10)
    
    if not top_users:
        await interaction.response.send_message("Chưa có ai sở hữu Chú lực cả.")
        return
        
    desc = ""
    for idx, (uid, cl) in enumerate(top_users, 1):
        desc += f"**{idx}.** <@{uid}> - {cl:,} Chú lực\n"
        
    embed = discord.Embed(
        title="🏆 Bảng Xếp Hạng Chú Lực",
        description=desc,
        color=0x3B82F6
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clearmem", description="Xóa sạch ký ức trò chuyện của Megumi với cậu")
async def slash_clear_memory(interaction: discord.Interaction):
    reset_memory(interaction.channel_id, interaction.user.id)
    author_name = interaction.user.display_name
    is_seiki = "han seiki" in author_name.lower() or "seiki" in author_name.lower()
    
    if is_seiki:
        desc = "Tôi đã xóa sạch những chuyện lặt vặt vừa rồi. Có nhiệm vụ gì mới sao, Seiki?"
    else:
        desc = f"Những chuyện không cần thiết tôi đã bỏ qua hết rồi. Vào việc chính đi, {author_name}."
        
    embed = discord.Embed(
        title="🧹 Làm Mới Trạng Thái",
        description=desc,
        color=0x2C2F33
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="sync", description="Đồng bộ lệnh Slash")
async def slash_sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        bot.tree.clear_commands(guild=interaction.guild)
        await bot.tree.sync(guild=interaction.guild)
        synced = await bot.tree.sync()
        await interaction.followup.send(f"Đã dọn sạch lệnh rác và đồng bộ {len(synced)} lệnh. Vui lòng bấm Ctrl+R trên Discord để cập nhật giao diện.")
    except Exception as e:
        await interaction.followup.send(f"Lỗi: {e}")

# ==============================================================================
# HỆ THỐNG CỬA HÀNG VÀ TÍNH NĂNG MỚI
# ==============================================================================

SHOP_ITEMS = {
    "ngoc_khuyen": {"name": "Ngọc Khuyển", "price": 1600, "desc": "Mute đối phương 2 phút"},
    "nue": {"name": "Nue (Chim Điện)", "price": 2000, "desc": "Mute đối phương 5 phút"},
    "thoat_tho": {"name": "Thoát Thố", "price": 500, "desc": "40% tỷ lệ né Mute (tự tiêu hao 1 con)"},
    "mahoraga": {"name": "Mahoraga", "price": 50000, "desc": "Kháng Mute vĩnh viễn"}
}

thoat_tho_bonus = {} # {user_id: bonus_chance (float)}
last_dungeon_times = {} # {user_id: "YYYY-MM-DD"}

@bot.tree.command(name="admin_remove_item", description="Admin: Thu hồi Thức thần của một người")
@app_commands.choices(item=[
    app_commands.Choice(name="Ngọc Khuyển", value="ngoc_khuyen"),
    app_commands.Choice(name="Nue", value="nue"),
    app_commands.Choice(name="Thoát Thố", value="thoat_tho"),
    app_commands.Choice(name="Mahoraga", value="mahoraga"),
])
async def admin_remove_item(interaction: discord.Interaction, member: discord.Member, item: app_commands.Choice[str]):
    if interaction.user.id != 1502579398560317441:
        await interaction.response.send_message("❌ Kẻ mạo danh! Chỉ có Chủ nhân (Developer) mới được dùng quyền này.", ephemeral=True)
        return
        
    get_user(member.id) # Ensure user exists
    
    # Get current user data to see if they have the item
    user_data = get_user(member.id)
    item_idx = 5 if item.value == "ngoc_khuyen" else (6 if item.value == "nue" else (7 if item.value == "thoat_tho" else 8))
    
    if user_data[item_idx] <= 0:
        await interaction.response.send_message(f"❌ {member.mention} không có {item.name} để thu hồi.", ephemeral=True)
        return
        
    # If Mahoraga, completely remove it. If others, just remove 1.
    if item.value == "mahoraga":
        update_user_item(member.id, item.value, -user_data[item_idx]) # Remove all instances (usually just 1)
        await interaction.response.send_message(f"🛠️ (Admin) Đã tước đoạt **{item.name}** khỏi {member.mention}.")
    else:
        update_user_item(member.id, item.value, -1)
        await interaction.response.send_message(f"🛠️ (Admin) Đã thu hồi 1 **{item.name}** của {member.mention}.")

@bot.tree.command(name="shop", description="Cửa hàng Thức thần")
async def shop(interaction: discord.Interaction):
    desc = ""
    for k, v in SHOP_ITEMS.items():
        desc += f"**{v['name']}** - 💰 {v['price']:,} Chú lực\n↳ *{v['desc']}*\n\n"
    embed = discord.Embed(title="🛒 Cửa Hàng Thức Thần", description=desc, color=0x8B5CF6)
    embed.set_footer(text="Dùng lệnh /buy để mua và /use để dùng")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="buy", description="Mua vật phẩm từ cửa hàng")
@app_commands.choices(item=[
    app_commands.Choice(name="Ngọc Khuyển (600)", value="ngoc_khuyen"),
    app_commands.Choice(name="Nue (1000)", value="nue"),
    app_commands.Choice(name="Thoát Thố (300)", value="thoat_tho"),
    app_commands.Choice(name="Mahoraga (7500)", value="mahoraga"),
])
async def buy_item(interaction: discord.Interaction, item: app_commands.Choice[str]):
    user_data = get_user(interaction.user.id)
    price = SHOP_ITEMS[item.value]["price"]
    
    if user_data[1] < price:
        await interaction.response.send_message(f"Không đủ tiền. Cậu chỉ có {user_data[1]:,} Chú lực.", ephemeral=True)
        return
        
    if item.value == "mahoraga" and user_data[8] > 0:
        await interaction.response.send_message("Cậu đã sở hữu Mahoraga rồi, mua thêm làm gì?", ephemeral=True)
        return
        
    update_user_chu_luc(interaction.user.id, -price)
    update_user_item(interaction.user.id, item.value, 1)
    
    await interaction.response.send_message(f"🛍️ Cậu đã mua thành công **{SHOP_ITEMS[item.value]['name']}**!")

@bot.tree.command(name="inventory", description="Xem túi đồ của cậu")
async def inventory(interaction: discord.Interaction):
    user_data = get_user(interaction.user.id)
    desc = f"**Ngọc Khuyển:** {user_data[5]}\n**Nue:** {user_data[6]}\n**Thoát Thố:** {user_data[7]}\n**Mahoraga:** {'Có (Vĩnh viễn)' if user_data[8] > 0 else 'Không'}"
    embed = discord.Embed(title="🎒 Túi Đồ Thức Thần", description=desc, color=0x22C55E)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="use", description="Dùng Thức thần tấn công (Mute) đối phương")
@app_commands.choices(item=[
    app_commands.Choice(name="Ngọc Khuyển (Mute 2p)", value="ngoc_khuyen"),
    app_commands.Choice(name="Nue (Mute 5p)", value="nue")
])
async def use_item(interaction: discord.Interaction, item: app_commands.Choice[str], target: discord.Member):
    if target.id == interaction.user.id:
        await interaction.response.send_message("Bị ngốc à? Tự đánh mình làm gì.", ephemeral=True)
        return
        
    if target.guild_permissions.administrator:
        await interaction.response.send_message("Đối phương là Admin (Kẻ Vô Hạ Hạn), Thức thần của cậu không thể chạm vào họ!", ephemeral=True)
        return
        
    user_data = get_user(interaction.user.id)
    item_idx = 5 if item.value == "ngoc_khuyen" else 6
    if user_data[item_idx] <= 0:
        await interaction.response.send_message(f"Cậu không có {SHOP_ITEMS[item.value]['name']}. Hãy vào /shop để mua.", ephemeral=True)
        return
        
    # Tiêu hao item
    update_user_item(interaction.user.id, item.value, -1)
    
    target_data = get_user(target.id)
    target_mahoraga = target_data[8]
    target_thoattho = target_data[7]
    
    if target_mahoraga > 0:
        await interaction.response.send_message(f"🐺 **{interaction.user.display_name}** tung {SHOP_ITEMS[item.value]['name']} tấn công {target.mention}!\n🛡️ NHƯNG! Bánh xe luân hồi quay... **Mahoraga** của {target.display_name} đã thích nghi và hóa giải hoàn toàn đòn tấn công!")
        return
        
    dodge_msg = ""
    if target_thoattho > 0:
        base_chance = 0.40
        bonus_chance = thoat_tho_bonus.get(target.id, 0.0)
        total_chance = base_chance + bonus_chance
        
        if random.random() <= total_chance:
            update_user_item(target.id, "thoat_tho", -1)
            thoat_tho_bonus[target.id] = 0.0 # Reset
            await interaction.response.send_message(f"🐺 **{interaction.user.display_name}** tung {SHOP_ITEMS[item.value]['name']} tấn công {target.mention}!\n🐇 Đàn **Thoát Thố** của {target.display_name} xuất hiện đánh lạc hướng thành công (Tỷ lệ né: {int(total_chance*100)}%)! (Mất 1 Thoát Thố)")
            return
        else:
            update_user_item(target.id, "thoat_tho", -1)
            thoat_tho_bonus[target.id] = bonus_chance + 0.05
            dodge_msg = f"\n🐇 *(Đàn Thoát Thố của {target.display_name} đã ùa ra cản địa nhưng thất bại! Tỉ lệ né ván sau tăng thành {int((total_chance+0.05)*100)}%. Mất 1 Thoát Thố)*"
            
    duration_mins = 2 if item.value == "ngoc_khuyen" else 5
    try:
        until = discord.utils.utcnow() + timedelta(minutes=duration_mins)
        await target.timeout(until, reason=f"Bị {interaction.user.display_name} dùng {SHOP_ITEMS[item.value]['name']}")
        await interaction.response.send_message(f"💥 **{interaction.user.display_name}** đã dùng **{SHOP_ITEMS[item.value]['name']}**!{dodge_msg}\n🔇 {target.mention} đã bị dính đòn và bị **CẤM NGÔN (Mute) {duration_mins} phút**!")
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Tôi không đủ quyền để mute {target.mention}. Hãy kiểm tra xem Role (Vai trò) của tôi (Megumi) trong Server Settings có cao hơn người này chưa, và tôi đã được cấp quyền Timeout Members chưa.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Có lỗi: {e}", ephemeral=True)

@bot.tree.command(name="trade", description="Chuyển Chú lực cho người khác")
@app_commands.describe(member="Người nhận", amount="Số lượng Chú lực")
async def trade_chu_luc(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Số lượng phải lớn hơn 0.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("Định tự chuyển cho chính mình à?", ephemeral=True)
        return
        
    sender_data = get_user(interaction.user.id)
    if sender_data[1] < amount:
        await interaction.response.send_message(f"Không đủ Chú lực. Cậu chỉ có {sender_data[1]:,}.", ephemeral=True)
        return
        
    update_user_chu_luc(interaction.user.id, -amount)
    get_user(member.id) # Khởi tạo nếu chưa có
    update_user_chu_luc(member.id, amount)
    
    await interaction.response.send_message(f"💸 **{interaction.user.display_name}** đã chuyển **{amount:,} Chú lực** cho {member.mention}.")

@bot.tree.command(name="admin_add", description="Admin: Bơm Chú lực cho user")
async def admin_add(interaction: discord.Interaction, member: discord.Member, amount: int):
    if interaction.user.id != 1502579398560317441:
        await interaction.response.send_message("❌ Kẻ mạo danh! Chỉ có Chủ nhân (Developer) mới được dùng quyền này.", ephemeral=True)
        return
        
    get_user(member.id)
    update_user_chu_luc(member.id, amount)
    await interaction.response.send_message(f"🛠️ (Admin) Đã bơm **{amount:,} Chú lực** cho {member.mention}.")

@bot.tree.command(name="admin_remove", description="Admin: Trừ Chú lực của user")
async def admin_remove(interaction: discord.Interaction, member: discord.Member, amount: int):
    if interaction.user.id != 1502579398560317441:
        await interaction.response.send_message("❌ Kẻ mạo danh! Chỉ có Chủ nhân (Developer) mới được dùng quyền này.", ephemeral=True)
        return
        
    if amount <= 0:
        await interaction.response.send_message("Số lượng phải lớn hơn 0.", ephemeral=True)
        return
        
    user_data = get_user(member.id)
    if user_data[1] < amount:
        # Nếu số tiền trừ lớn hơn số tiền họ đang có, thì trừ sạch về 0
        update_user_chu_luc(member.id, -user_data[1])
        await interaction.response.send_message(f"🛠️ (Admin) Đã tước đoạt toàn bộ **{user_data[1]:,} Chú lực** còn lại của {member.mention}.")
    else:
        update_user_chu_luc(member.id, -amount)
        await interaction.response.send_message(f"🛠️ (Admin) Đã trừng phạt, tước đi **{amount:,} Chú lực** của {member.mention}.")

@bot.tree.command(name="admin_spawn_boss", description="Admin: Triệu hồi Dị thể Megumi để Raid")
async def admin_spawn_boss(interaction: discord.Interaction):
    if interaction.user.id != 1502579398560317441:
        await interaction.response.send_message("❌ Kẻ mạo danh! Chỉ có Chủ nhân (Developer) mới được dùng quyền này.", ephemeral=True)
        return
        
    await interaction.response.send_message("⚠️ Đang giải phóng Dị thể...", ephemeral=True)
    bot.loop.create_task(spawn_boss(interaction.channel))

@bot.tree.command(name="dungeon", description="Tham gia khám phá Hầm ngục. Phí: 2,000 CL. Nhận 1k-11k CL (1 lần/ngày)")
async def dungeon(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_data = get_user(user_id)
    
    # Kiểm tra cooldown ngày
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if last_dungeon_times.get(user_id) == today_str:
        await interaction.response.send_message("❌ Cậu đã vào Hầm ngục ngày hôm nay rồi, hãy quay lại vào ngày mai nhé!", ephemeral=True)
        return
        
    # Kiểm tra tiền
    if user_data[1] < 2000:
        await interaction.response.send_message(f"❌ Cậu không đủ 2,000 Chú lực để vào Hầm ngục (Hiện tại có {user_data[1]:,}).", ephemeral=True)
        return
        
    # Tiêu phí
    update_user_chu_luc(user_id, -2000)
    
    # Random phần thưởng 1000 -> 11000
    reward = random.randint(1000, 11000)
    update_user_chu_luc(user_id, reward)
    
    # Lưu cooldown
    last_dungeon_times[user_id] = today_str
    
    profit = reward - 2000
    if profit > 0:
        msg = f"🎉 **{interaction.user.display_name}** đã dũng cảm bước vào Hầm ngục và tìm thấy **{reward:,} Chú lực**! (Lãi {profit:,} CL)"
    else:
        msg = f"🏚️ **{interaction.user.display_name}** bước vào Hầm ngục nhưng chỉ thu thập được **{reward:,} Chú lực**. (Lỗ {-profit:,} CL)"
        
    embed = discord.Embed(title="⚔️ Khám phá Hầm ngục", description=msg, color=0x3B82F6 if profit > 0 else 0xEF4444)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="coinflip", description="Tung đồng xu cược Chú lực (Thắng x2)")
@app_commands.describe(amount="Số Chú lực cược", choice="Chọn Sấp hoặc Ngửa")
@app_commands.choices(choice=[
    app_commands.Choice(name="Sấp (Heads)", value="sap"),
    app_commands.Choice(name="Ngửa (Tails)", value="ngua")
])
async def coinflip(interaction: discord.Interaction, amount: int, choice: app_commands.Choice[str]):
    if amount <= 0:
        await interaction.response.send_message("Cược số dương thôi.", ephemeral=True)
        return
        
    user_data = get_user(interaction.user.id)
    if user_data[1] < amount:
        await interaction.response.send_message(f"Cậu không đủ Chú lực (Đang có: {user_data[1]:,})", ephemeral=True)
        return
        
    outcome = random.choice(["sap", "ngua"])
    if choice.value == outcome:
        update_user_chu_luc(interaction.user.id, amount) 
        await interaction.response.send_message(f"🪙 Đồng xu ra **{'Sấp' if outcome == 'sap' else 'Ngửa'}**!\n🎉 Cậu đã thắng và nhận được **{amount * 2:,} Chú lực** (Lãi {amount:,}).")
    else:
        update_user_chu_luc(interaction.user.id, -amount)
        await interaction.response.send_message(f"🪙 Đồng xu ra **{'Sấp' if outcome == 'sap' else 'Ngửa'}**!\n💀 Cậu đoán sai và mất **{amount:,} Chú lực**.")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
