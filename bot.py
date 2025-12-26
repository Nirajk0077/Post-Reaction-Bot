import logging
import os
import sys
import threading
import pickle
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from collections import defaultdict, deque

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Define the 4 reaction emojis
REACTIONS = ["👍", "👎", "🔥", "❤️"]

# Track processed media groups to prevent duplicate buttons on albums
processed_media_groups = deque(maxlen=1000)

INFO_TEXT = """Hɪɴᴅɪ:-
Is Pᴏsᴛ Kᴇ Bᴀᴀʀᴇ Mᴇɪɴ Aᴀᴘᴋᴀ Kʏᴀ Kʜᴀʏᴀʟ Hᴀɪ? Nᴇᴇᴄʜᴇ Rᴇᴀᴄᴛɪᴏɴ Dᴇɪɴ! 👇

Eɴɢʟɪsʜ:-
Wʜᴀᴛ ᴅᴏ ʏᴏᴜ ᴛʜɪɴᴋ ᴏғ ᴛʜɪs ᴘᴏsᴛ? Lᴇᴀᴠᴇ ʏᴏᴜʀ ʀᴇᴀᴄᴛɪᴏɴ ʙᴇʟᴏᴡ! 👇"""


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Override to suppress default logging to stderr, or redirect to logger
        logger.info(f"Health check request: {self.client_address[0]}")


def start_health_server():
    try:
        port = int(os.environ.get("PORT", 8080))
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"Starting health check server on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.critical(f"Health check server failed to start or crashed: {e}")
        # Forcefully exit the process so the platform knows something is wrong
        os._exit(1)


def get_keyboard(reactions_data, share_url=None, comment_url=None):
    """
    Generates the inline keyboard based on current reaction counts.
    reactions_data: dict of {emoji: set(user_ids)}
    share_url: Optional URL to be used in the Share button.
    comment_url: Optional URL to be used in the Comment button.
    
    Layout:
    [ R1, R2, R3, R4 ] (Top)
    [ Share ] (Middle, if available)
    [ Support Group, Join Channel ] (Bottom)
    """
    
    # 1. Reaction Buttons (Top Row)
    reaction_buttons = []
    for emoji in REACTIONS:
        count = len(reactions_data.get(emoji, []))
        text = f"{emoji} {count}" if count > 0 else emoji
        reaction_buttons.append(InlineKeyboardButton(text, callback_data=f"reaction|{emoji}"))
    
    # 2. Top Row (Info + Share)
    top_row = [InlineKeyboardButton("Info ℹ️", callback_data="info")]
    if share_url:
        top_row.append(InlineKeyboardButton("Share ⤴️", url=f"https://t.me/share/url?url={share_url}"))

    # 3. Comment Button
    comment_row = []
    # Use provided comment_url, or fallback to share_url + ?comment=1 if available
    final_comment_url = comment_url if comment_url else (f"{share_url}?comment=1" if share_url else None)

    if final_comment_url:
        comment_row.append(InlineKeyboardButton("Comment 💬", url=final_comment_url))

    # 4. Link Buttons (Bottom Row)
    support_group_url = os.environ.get("SUPPORT_GROUP_URL", "tg://resolve?domain=OOSSupport")
    channel_url = os.environ.get("CHANNEL_URL", "tg://resolve?domain=OOSHub")
    
    link_buttons = [
        InlineKeyboardButton("🔄 Support Chat", url=support_group_url),
        InlineKeyboardButton("💠 Join Channel", url=channel_url)
    ]
    
    keyboard = []
    keyboard.append(top_row)
    keyboard.append(reaction_buttons)
    if comment_row:
        keyboard.append(comment_row)
    keyboard.append(link_buttons)

    return InlineKeyboardMarkup(keyboard)


async def add_reaction_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Adds reaction buttons to new posts in channels or groups.
    """
    # Determine if it's a channel post or a group message
    is_channel = bool(update.channel_post)
    message = update.channel_post if is_channel else update.message

    if not message:
        return

    # Filter out private chats if any slip through
    if message.chat.type == "private":
        return

    # Filter out posts that are not text, photo, video, or document
    if not (message.text or message.caption or message.photo or message.video or message.document):
        return

    # Deduplicate media groups (albums)
    if message.media_group_id:
        if message.media_group_id in processed_media_groups:
            return
        processed_media_groups.append(message.media_group_id)

    chat_id = message.chat_id
    
    # Construct the share URL (post link)
    if is_channel:
        post_link = message.link
    else:
        # In groups, if it's a forward from a channel, try to link to the original post
        if message.forward_from_chat and message.forward_from_chat.type == "channel":
            origin_chat = message.forward_from_chat
            origin_msg_id = message.forward_from_message_id
            if origin_chat.username:
                post_link = f"https://t.me/{origin_chat.username}/{origin_msg_id}"
            else:
                # Private channel link format: https://t.me/c/ID/MSG_ID
                # ID usually starts with -100, we need to strip it
                chat_id_str = str(origin_chat.id).replace("-100", "")
                post_link = f"https://t.me/c/{chat_id_str}/{origin_msg_id}"
        else:
            post_link = message.link
    
    # Determine the Comment URL
    # For channels, we rely on the fallback (share_url + ?comment=1) which works fine.
    # For groups, linking to the channel post with ?comment=1 causes "removed from discussion group" error.
    # So we link to the message thread in the group itself.
    comment_url = None
    if not is_channel and message.link:
        comment_url = f"{message.link}?thread={message.message_id}"

    # We need to decide where to attach the buttons.
    # For channels: The bot (as admin) can edit the channel post to add buttons.
    # For groups: The bot CANNOT edit a user's message. It must reply with a new message containing the buttons.
    
    target_message = None
    
    if is_channel:
        # In channels, we try to edit the message itself.
        target_message = message
        
        # NOTE: To make "Join Channel" persist on forward, we append it to the text.
        channel_url = os.environ.get("CHANNEL_URL", "https://t.me/telegram")
        try:
            original_text = message.text or message.caption or ""
            if channel_url not in original_text:
                new_text = f"{original_text}"
                if message.text:
                    await message.edit_text(new_text)
                elif message.caption:
                    await message.edit_caption(new_text)
        except Exception as e:
            logger.warning(f"Failed to append link to text in {chat_id}: {e}")

    else:
        # In groups, we ONLY reply if the message is forwarded from a channel.
        # Check for forward_from_chat type being 'channel'
        is_channel_forward = False
        if message.forward_from_chat and message.forward_from_chat.type == "channel":
            is_channel_forward = True
        
        if not is_channel_forward:
            return

        try:
            target_message = await message.reply_text(
                "Rate this post 👇",
                reply_markup=get_keyboard({}, share_url=post_link, comment_url=comment_url)
            )
        except Exception as e:
            logger.error(f"Failed to send reply in group {chat_id}: {e}")
            return

    # Now we initialize the reaction data for the TARGET message (the one holding the buttons)
    # If it's a channel, it's the original message.
    # If it's a group, it's the reply message sent by the bot.
    
    target_message_id = target_message.message_id
    key = f"{chat_id}_{target_message_id}"
    
    if "post_reactions" not in context.bot_data:
        context.bot_data["post_reactions"] = {}
    
    # Initialize if not present
    if key not in context.bot_data["post_reactions"]:
        context.bot_data["post_reactions"][key] = {emoji: set() for emoji in REACTIONS}

    # Save metadata (share_url and comment_url) for persistence
    if "post_meta" not in context.bot_data:
        context.bot_data["post_meta"] = {}
    context.bot_data["post_meta"][key] = {"share_url": post_link, "comment_url": comment_url}

    # For channels, we perform the edit now that data is initialized
    if is_channel:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=target_message_id,
                reply_markup=get_keyboard(context.bot_data["post_reactions"][key], share_url=post_link, comment_url=comment_url)
            )
        except Exception as e:
            logger.error(f"Failed to add buttons to channel post {target_message_id} in chat {chat_id}: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles button clicks (Reactions).
    """
    query = update.callback_query
    user = query.from_user
    data = query.data
    
    # Handle Info Button
    if data == "info":
        await query.answer(text=INFO_TEXT, show_alert=True)
        return

    # Handle Reaction Buttons
    if not data.startswith("reaction|"):
        await query.answer()
        return

    selected_emoji = data.split("|")[1]
    
    message = query.message
    chat_id = message.chat_id
    message_id = message.message_id
    key = f"{chat_id}_{message_id}"
    
    # Retrieve the original post link from metadata if available
    if "post_meta" not in context.bot_data:
        context.bot_data["post_meta"] = {}

    meta = context.bot_data["post_meta"].get(key, {})
    share_url = meta.get("share_url")
    comment_url = meta.get("comment_url")

    # Fallback if metadata is missing (e.g. old posts)
    if not share_url:
        share_url = message.link

    # Try to reconstruct comment_url for groups if missing
    if not comment_url and message.chat.type != "channel":
        # In groups, the bot's message (message) is a reply to the original post (reply_to_message)
        original_msg = message.reply_to_message
        if original_msg and original_msg.link:
            comment_url = f"{original_msg.link}?thread={original_msg.message_id}"
    
    if "post_reactions" not in context.bot_data:
        context.bot_data["post_reactions"] = {}
    
    if key not in context.bot_data["post_reactions"]:
        # Fallback if data is missing
        context.bot_data["post_reactions"][key] = {emoji: set() for emoji in REACTIONS}

    reactions_map = context.bot_data["post_reactions"][key]
    user_id = user.id
    
    # Single reaction logic: Remove user from ALL other reactions first
    for emoji, user_set in reactions_map.items():
        if emoji != selected_emoji and user_id in user_set:
            user_set.remove(user_id)
    
    # Toggle logic for the selected emoji
    if user_id in reactions_map[selected_emoji]:
        reactions_map[selected_emoji].remove(user_id)
        notification_text = f"Removed {selected_emoji}"
    else:
        reactions_map[selected_emoji].add(user_id)
        notification_text = f"Added {selected_emoji}"
        
    await query.answer(text=notification_text)
    
    # Update the keyboard
    new_markup = get_keyboard(reactions_map, share_url=share_url, comment_url=comment_url)
    
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to update reactions for message {message_id} in chat {chat_id}: {e}")


def prune_bot_data(filepath="bot_data.pickle"):
    """
    Prunes old post data from bot_data.pickle to prevent memory bloat and OOM crashes.
    Keeps only the last 50 posts per chat.
    """
    if not os.path.exists(filepath):
        return

    try:
        logger.info("Checking bot data for pruning...")
        with open(filepath, "rb") as f:
            data = pickle.load(f)

        # persistence stores data under 'bot_data' key
        bot_data = data.get("bot_data", {})
        if not bot_data:
            return

        reactions = bot_data.get("post_reactions", {})
        meta = bot_data.get("post_meta", {})

        initial_count = len(reactions)

        # Group keys by chat_id
        chat_posts = defaultdict(list)
        for key in list(reactions.keys()):
            try:
                # Key format: "{chat_id}_{message_id}"
                parts = key.rsplit("_", 1)
                if len(parts) != 2:
                    continue
                chat_id_str, msg_id_str = parts
                chat_id = int(chat_id_str)
                msg_id = int(msg_id_str)

                chat_posts[chat_id].append((msg_id, key))
            except ValueError:
                continue

        keys_to_remove = []
        MAX_POSTS_PER_CHAT = 50

        for chat_id, posts in chat_posts.items():
            # Sort by message_id descending (newest first)
            posts.sort(key=lambda x: x[0], reverse=True)

            # Identify old posts
            if len(posts) > MAX_POSTS_PER_CHAT:
                for _, key in posts[MAX_POSTS_PER_CHAT:]:
                    keys_to_remove.append(key)

        if keys_to_remove:
            logger.info(f"Pruning: Removing {len(keys_to_remove)} old posts from bot_data.")
            for key in keys_to_remove:
                reactions.pop(key, None)
                meta.pop(key, None)

            # Save back to file
            with open(filepath, "wb") as f:
                pickle.dump(data, f)
        else:
            logger.info("No pruning needed.")

    except Exception as e:
        logger.warning(f"Failed to prune bot data: {e}")


def main():
    """Start the bot."""
    # Get the token from environment variable or ask user to input it
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

    if token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("Bot token not found. Set TELEGRAM_BOT_TOKEN environment variable.")
        sys.exit(1)
    
    # Prune old data before loading to prevent OOM
    prune_bot_data()

    # Start health check server in background thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    try:
        persistence = PicklePersistence(filepath="bot_data.pickle")
        application = Application.builder().token(token).persistence(persistence).build()

        # 1. Handler for Channel Posts
        application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.UpdateType.CHANNEL_POST, add_reaction_buttons))

        # 2. Handler for Group Messages
        # We use reply_text approach now. We also filter for FORWARDED messages only,
        # and further check specifically for channel forwards inside the handler.
        application.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND) & filters.UpdateType.MESSAGE & filters.FORWARDED, add_reaction_buttons))

        # 3. Callback Query Handler
        application.add_handler(CallbackQueryHandler(handle_callback))

        print("Bot is starting... Press Ctrl+C to stop.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.critical(f"Critical error in main loop: {e}", exc_info=True)
        # Ensure the process exits so the container can restart
        os._exit(1)


if __name__ == "__main__":
    main()

