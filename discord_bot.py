import discord
from discord.ext import commands
import yt_dlp
import asyncio
import time
import re
import os  # [新增] 導入 os 模組
from dotenv import load_dotenv # [新增] 從 dotenv 導入 load_dotenv

# [新增] 載入 .env 檔案中的環境變數
load_dotenv()

# --- 設定 ---
INTENTS = discord.Intents.default()
INTENTS.message_content = True
BOT_PREFIX = '!'
# [修改] 從環境變數讀取 TOKEN
TOKEN = os.getenv('TOKEN')

# 檢查 TOKEN 是否成功載入
if TOKEN is None:
    print("錯誤：找不到環境變數 'TOKEN'。請確定你的 .env 檔案已建立且包含 TOKEN。")
    exit() # 如果沒有 Token，直接結束程式

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'skip_download': True,
    'default_search': 'ytsearch1',
    'source_address': '0.0.0.0',
    # [新增] 強制使用 android 客戶端以避免 Web 端被限流 (Signature extraction failed)
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'ios']
        }
    }
}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -af "dynaudnorm=f=150:g=15"',
}

# --- 按鈕控制視圖 ---
# ... (MusicControlsView 類別完全不變) ...
class MusicControlsView(discord.ui.View):
    def __init__(self, music_engine, *, timeout=None):
        super().__init__(timeout=timeout)
        self.music_engine = music_engine
        self.update_buttons()

    def update_buttons(self):
        if self.music_engine.voice_client and self.music_engine.voice_client.is_paused():
            self.pause_resume_button.label = "播放"
            self.pause_resume_button.emoji = "▶️"
            self.pause_resume_button.style = discord.ButtonStyle.green
        else:
            self.pause_resume_button.label = "暫停"
            self.pause_resume_button.emoji = "⏸️"
            self.pause_resume_button.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or interaction.user.voice.channel != self.music_engine.voice_client.channel:
            await interaction.response.send_message("你必須跟機器人在同一個語音頻道才能使用按鈕！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="暫停", style=discord.ButtonStyle.secondary, emoji="⏸️")
    async def pause_resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.music_engine.voice_client and self.music_engine.voice_client.is_playing():
            self.music_engine.pause_playback()
            await interaction.followup.send("⏸️ 音樂已暫停。", ephemeral=True)
        elif self.music_engine.voice_client and self.music_engine.voice_client.is_paused():
            self.music_engine.resume_playback()
            await interaction.followup.send("▶️ 音樂已恢復播放。", ephemeral=True)
        self.update_buttons()
        await interaction.message.edit(view=self)
        
    @discord.ui.button(label="跳過", style=discord.ButtonStyle.primary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.music_engine.voice_client and (self.music_engine.voice_client.is_playing() or self.music_engine.voice_client.is_paused()):
            song_title = self.music_engine.current_song['title'] if self.music_engine.current_song else "目前歌曲"
            self.music_engine.voice_client.stop()
            await interaction.followup.send(f"⏭️ **{interaction.user.mention}** 已跳過 **{song_title}**。")

    @discord.ui.button(label="離開", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("掰掰，下次再找我！", ephemeral=True)
        await self.music_engine.stop_and_cleanup()
    
    @discord.ui.button(label="播放清單", style=discord.ButtonStyle.primary, emoji="🎶", row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        embed = self.music_engine.create_queue_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

# --- 音樂引擎核心 ---
class MusicEngine:
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = asyncio.Queue()
        self.current_song = None
        self.voice_client = None
        self.last_ctx = None
        self.now_playing_message = None
        self.idle_timeout = 180
        self.playback_start_time = 0
        self.time_played_before_pause = 0
        self.progress_updater_task = None
        # [新增] 用於智慧型更新
        self.last_progress_bar = None
        self.player_task = self.bot.loop.create_task(self.player_loop())

    def format_time(self, seconds):
        if seconds is None: return "00:00"
        seconds = int(seconds)
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def create_progress_bar(self, current_time):
        total_time = self.current_song.get('duration', 0)
        if total_time == 0: return "`直播中，無進度條`"
        percentage = (current_time / total_time) if total_time > 0 else 0
        bar_length = 20
        # 使用 round 讓進度條更貼近視覺上的比例 (例如 99% 時會進位到滿格)
        filled_length = round(bar_length * percentage)
        # 確保不會超過長度
        filled_length = min(max(filled_length, 0), bar_length)
        bar = '▬' * filled_length + '🔘' + '─' * (bar_length - filled_length)
        formatted_current = self.format_time(current_time)
        formatted_total = self.format_time(total_time)
        return f"`{formatted_current}` {bar} `{formatted_total}`"

    # [主要修改處] 智慧型更新邏輯
    async def progress_updater(self):
        try:
            while self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
                if self.voice_client.is_playing() and self.now_playing_message:
                    current_time = self.get_current_playback_time()
                    new_progress_bar = self.create_progress_bar(current_time)

                    # 只有在進度條的視覺呈現有變化時才更新
                    if new_progress_bar != self.last_progress_bar:
                        embed = self.now_playing_message.embeds[0]
                        embed.set_field_at(0, name="進度", value=new_progress_bar, inline=False)
                        try:
                            await self.now_playing_message.edit(embed=embed)
                            self.last_progress_bar = new_progress_bar # 更新最後的進度條
                        except (discord.NotFound, discord.HTTPException):
                            break
                
                # 每 1 秒計算一次，讓進度條看起來更順暢
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"進度條更新時發生錯誤: {e}")

    # ... (其餘 MusicEngine 方法不變) ...
    def get_current_playback_time(self):
        if self.voice_client and self.voice_client.is_paused():
            return self.time_played_before_pause
        if self.playback_start_time == 0:
            return 0
        return self.time_played_before_pause + (time.time() - self.playback_start_time)
    
    def pause_playback(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self.time_played_before_pause += (time.time() - self.playback_start_time)

    def resume_playback(self):
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self.playback_start_time = time.time()
            
    async def player_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            if self.progress_updater_task and not self.progress_updater_task.done():
                self.progress_updater_task.cancel()
            if self.now_playing_message:
                try: await self.now_playing_message.edit(view=None)
                except (discord.NotFound, discord.HTTPException): pass
                self.now_playing_message = None

            try:
                next_song_request = await asyncio.wait_for(self.queue.get(), timeout=self.idle_timeout)
            except asyncio.TimeoutError:
                await self.stop_and_cleanup(is_idle=True)
                return

            self.last_ctx = next_song_request['ctx']
            
            loop = self.bot.loop
            song_info = await loop.run_in_executor(None, self.get_song_info, next_song_request['search'])

            if song_info is None:
                await self.last_ctx.send(f"❌ 抱歉，找不到 `{next_song_request['search']}` 這首歌。")
                continue

            if self.voice_client is None or not self.voice_client.is_connected():
                try: self.voice_client = await next_song_request['channel'].connect()
                except Exception as e:
                    await self.last_ctx.send("我無法連接到你的語音頻道，請檢查我的權限！")
                    continue
            elif self.voice_client.channel != next_song_request['channel']:
                await self.voice_client.move_to(next_song_request['channel'])

            self.current_song = song_info
            self.current_song['requester'] = next_song_request['requester']
            
            source = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTIONS)
            
            self.time_played_before_pause = 0
            self.playback_start_time = time.time()
            
            finished = asyncio.Event()
            self.voice_client.play(source, after=lambda e: self.bot.loop.call_soon_threadsafe(finished.set))

            await self.send_now_playing_message(self.current_song)
            self.progress_updater_task = self.bot.loop.create_task(self.progress_updater())
            
            await finished.wait()
            self.current_song = None

    def get_video_basic_info(self, search_query: str):
        """
        Uses yt_dlp to quickly fetch basic info (title, url) without processing full stream.
        """
        try:
            # Check if it's a URL or search query
            is_url = re.match(r'^https?://', search_query)

            opts = {
                'quiet': True,
                'skip_download': True,
                'noplaylist': True,
            }

            if not is_url:
                search_query = f"ytsearch:{search_query}"
                opts['extract_flat'] = 'in_playlist' # Faster for search results

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_query, download=False, process=not is_url)
                
                if 'entries' in info:
                    if not info['entries']:
                        return None
                    entry = info['entries'][0]
                    return {
                        'title': entry.get('title', search_query),
                        'url': entry.get('url', search_query)
                    }
                else:
                    return {
                        'title': info.get('title', 'Unknown Title'),
                        'url': info.get('webpage_url', search_query)
                    }

        except Exception as e:
            print(f"Error fetching basic info: {e}")
            return None

    def get_song_info(self, search_query: str):
        """
        Uses yt_dlp to extract the stream info.
        Supports both direct URLs and search queries.
        """
        video_url = search_query

        # Check if it looks like a URL, otherwise treat as search
        if not re.match(r'^https?://', search_query):
             video_url = f"ytsearch:{search_query}"

        try:
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                info = ydl.extract_info(video_url, download=False)

                # ytsearch returns entries
                if 'entries' in info:
                    if not info['entries']:
                        return None
                    info = info['entries'][0]
                
                return {
                    'source': info['url'], 
                    'title': info.get('title', 'Unknown Title'), 
                    'duration': info.get('duration', 0),
                    'webpage_url': info.get('webpage_url', search_query),
                    'thumbnail': info.get('thumbnail')
                }
        except Exception as e:
            print(f"yt_dlp failed to extract info for '{video_url}': {e}")
            return None
    
    async def send_now_playing_message(self, song_info):
        embed = discord.Embed(title="▶️ 正在播放", description=f"**{song_info['title']}**", color=discord.Color.green())
        
        # [修改] 重置 last_progress_bar
        self.last_progress_bar = self.create_progress_bar(0)
        embed.add_field(name="進度", value=self.last_progress_bar, inline=False)
        
        controls = MusicControlsView(self)
        self.now_playing_message = await self.last_ctx.send(embed=embed, view=controls)

    def create_queue_embed(self):
        queue_items = list(self.queue._queue)
        if not queue_items and self.current_song is None:
            return discord.Embed(description="目前播放清單是空的。", color=discord.Color.blue())
        embed = discord.Embed(title="🎶 播放清單", color=discord.Color.blue())
        if self.current_song:
            embed.add_field(name="正在播放", value=f"**{self.current_song['title']}**\n(點播者: {self.current_song['requester'].mention})", inline=False)
        if queue_items:
            queue_list = ""
            for i, item in enumerate(queue_items[:10]):
                search_term = item['song_title']
                requester = item['requester'].mention
                queue_list += f"`{i+1}.` {search_term} (點播者: {requester})\n"
            embed.add_field(name="待播清單", value=queue_list, inline=False)
            if len(queue_items) > 10:
                embed.set_footer(text=f"...還有 {len(queue_items) - 10} 首歌")
        return embed

    async def stop_and_cleanup(self, is_idle=False):
        if self.progress_updater_task and not self.progress_updater_task.done():
            self.progress_updater_task.cancel()
        while not self.queue.empty():
            try: self.queue.get_nowait()
            except asyncio.QueueEmpty: continue
        if self.voice_client:
            self.voice_client.stop()
            await self.voice_client.disconnect()
            self.voice_client = None
        if self.now_playing_message:
            try: await self.now_playing_message.edit(view=None)
            except (discord.NotFound, discord.HTTPException): pass
            self.now_playing_message = None
        if is_idle and self.last_ctx:
            await self.last_ctx.send(f"閒置超過 {self.idle_timeout // 60} 分鐘了，張牛先下班囉！😴(你想跑哪?!?)")
        music_engines.pop(self.guild_id, None)
        print(f"伺服器 {self.guild_id} 的 MusicEngine 已清理完畢。")
        if not self.player_task.done():
            self.player_task.cancel()


# --- Bot 初始化與指令 ---
# ... (所有指令完全不變) ...
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS)
music_engines = {}
def get_music_engine(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_engines:
        print(f"為伺服器 {guild_id} 建立新的 MusicEngine")
        music_engines[guild_id] = MusicEngine(bot, guild_id)
    return music_engines[guild_id]

@bot.event
async def on_ready():
    print(f'{bot.user.name} 已經上線！')

@bot.command(name='play', help='播放一首 YouTube 歌曲')
async def play(ctx, *, search: str = None):
    if search is None:
        await ctx.send("請在 `!play` 後面加上歌曲名稱或 YouTube 網址！")
        return

    music_engine = get_music_engine(ctx)
    if not ctx.author.voice:
        await ctx.send("你必須在一個語音頻道中才能使用此指令！")
        return
    
    # 使用 run_in_executor 避免阻塞，獲取基本資訊
    basic_info = await bot.loop.run_in_executor(None, music_engine.get_video_basic_info, search)

    song_title = search
    search_query = search

    if basic_info:
        song_title = basic_info.get('title', search)

    request = { 
        'ctx': ctx, 
        'channel': ctx.author.voice.channel, 
        'search': search_query,
        'song_title': song_title,
        'requester': ctx.author
    }
    
    await music_engine.queue.put(request)
    await ctx.send(f"✅ 已將 `{song_title}` 加入到待播清單！")

@bot.command(name='skip', help='跳過目前的歌曲')
async def skip(ctx):
    music_engine = get_music_engine(ctx)
    if music_engine.voice_client and (music_engine.voice_client.is_playing() or music_engine.voice_client.is_paused()):
        music_engine.voice_client.stop()
        await ctx.send("⏭️ 已跳過歌曲。")
    else:
        await ctx.send("目前沒有歌曲在播放。")

@bot.command(name='pause', help='暫停目前播放的音樂')
async def pause(ctx):
    music_engine = get_music_engine(ctx)
    if music_engine.voice_client and music_engine.voice_client.is_playing():
        music_engine.pause_playback()
        await ctx.send("⏸️ 音樂已暫停。")

@bot.command(name='resume', help='恢復播放音樂')
async def resume(ctx):
    music_engine = get_music_engine(ctx)
    if music_engine.voice_client and music_engine.voice_client.is_paused():
        music_engine.resume_playback()
        await ctx.send("⏯️ 音樂已恢復播放。")

@bot.command(name='leave', help='讓機器人離開語音頻道並清空待播清單')
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_engines:
        music_engine = music_engines[guild_id]
        await ctx.send("掰掰，下次再找我！")
        await music_engine.stop_and_cleanup()
    else:
        await ctx.send("我目前不在任何語音頻道中。")

@bot.command(name='queue', help='顯示待播清單')
async def queue_command(ctx):
    music_engine = get_music_engine(ctx)
    embed = music_engine.create_queue_embed()
    await ctx.send(embed=embed)

@bot.command(name='np', aliases=['nowplaying'], help='顯示目前播放的歌曲與進度')
async def now_playing(ctx):
    music_engine = get_music_engine(ctx)
    if not music_engine.current_song:
        await ctx.send("目前沒有歌曲正在播放。", ephemeral=True)
        return

    song = music_engine.current_song
    current_time = music_engine.get_current_playback_time()
    progress_bar = music_engine.create_progress_bar(current_time)

    embed = discord.Embed(
        title="▶️ 正在播放",
        description=f"**[{song['title']}]({song['webpage_url']})**",
        color=discord.Color.green()
    )
    if song.get('thumbnail'):
        embed.set_thumbnail(url=song['thumbnail'])
    
    embed.add_field(name="進度", value=progress_bar, inline=False)
    embed.add_field(name="點播者", value=song['requester'].mention, inline=False)

    controls = MusicControlsView(music_engine)

    await ctx.send(embed=embed, view=controls, ephemeral=True)

# --- 啟動 Bot ---
bot.run(TOKEN)