import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict

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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define the 4 reaction emojis
REACTIONS = ["👍", "👎", "🔥", "❤️"]


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Starting health check server on port {port}")
    server.serve_forever()


def get_keyboard(reactions_data, share_url=None):
    """
    Generates the inline keyboard based on current reaction counts.
    reactions_data: dict of {emoji: set(user_ids)}
    share_url: Optional URL to be used in the Share button.
    
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
    
    # 2. Share Button (Middle Row)
    middle_row = []
    if share_url:
        share_button = InlineKeyboardButton("Share ⤴️", url=f"https://t.me/share/url?url={share_url}")
        middle_row.append(share_button)
    
    # 3. Link Buttons (Bottom Row)
    support_group_url = os.environ.get("SUPPORT_GROUP_URL", "https://t.me/OOSCommunityy")
    channel_url = os.environ.get("CHANNEL_URL", "https://t.me/OOSHub")
    
    link_buttons = [
        InlineKeyboardButton("🔄 Support Chat", url=support_group_url),
        InlineKeyboardButton("☸️ Channel", url=channel_url)
    ]
    
    keyboard = []
    keyboard.append(reaction_buttons)
    if middle_row:
        keyboard.append(middle_row)
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

    chat_id = message.chat_id
    
    # Construct the share URL (post link)
    post_link = message.link
    
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
        # In groups, we reply to the message.
        try:
            target_message = await message.reply_text(
                "React:",
                reply_markup=get_keyboard({}, share_url=post_link)
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

    # For channels, we perform the edit now that data is initialized
    if is_channel:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=target_message_id,
                reply_markup=get_keyboard(context.bot_data["post_reactions"][key], share_url=post_link)
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
    
    # Handle Reaction Buttons
    if not data.startswith("reaction|"):
        await query.answer()
        return

    selected_emoji = data.split("|")[1]
    
    message = query.message
    chat_id = message.chat_id
    message_id = message.message_id
    key = f"{chat_id}_{message_id}"
    
    # Get the original post link if possible to keep the Share button working
    post_link = message.link
    
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
    new_markup = get_keyboard(reactions_map, share_url=post_link)
    
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to update reactions for message {message_id} in chat {chat_id}: {e}")


def main():
    """Start the bot."""
    # Get the token from environment variable or ask user to input it
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    
    # Start health check server in background thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    persistence = PicklePersistence(filepath="bot_data.pickle")
    
    application = Application.builder().token(token).persistence(persistence).build()

    # 1. Handler for Channel Posts
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.UpdateType.CHANNEL_POST, add_reaction_buttons))
    
    # 2. Handler for Group Messages
    # We use reply_text approach now
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND) & filters.UpdateType.MESSAGE, add_reaction_buttons))
    
    # 3. Callback Query Handler
    application.add_handler(CallbackQueryHandler(handle_callback))

    print("Bot is starting... Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

