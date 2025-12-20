import logging
import os
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
REACTIONS = ["👍", "❤️", "🔥", "👏"]


def get_keyboard(reactions_data):
    """
    Generates the inline keyboard based on current reaction counts.
    reactions_data: dict of {emoji: set(user_ids)}
    """
    buttons = []
    for emoji in REACTIONS:
        count = len(reactions_data.get(emoji, []))
        text = f"{emoji} {count}" if count > 0 else emoji
        # Callback data format: "reaction|{emoji}"
        buttons.append(InlineKeyboardButton(text, callback_data=f"reaction|{emoji}"))

    return InlineKeyboardMarkup([buttons])


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
    
    # We need to decide where to attach the buttons.
    # For channels: The bot (as admin) can edit the channel post to add buttons.
    # For groups: The bot CANNOT edit a user's message. It must reply with a new message containing the buttons.
    
    target_message = None
    
    if is_channel:
        # In channels, we try to edit the message itself.
        target_message = message
        # We need to ensure we can edit it. If it's a new post, we should be able to.
    else:
        # In groups, we reply to the message.
        # But we don't want to reply to every single message in a busy group unless necessary.
        # For this task, we assume we want to add reactions to every 'post' (message).
        try:
            target_message = await message.reply_text(
                "React:",
                reply_markup=get_keyboard({})
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
    
    # Initialize if not present (it shouldn't be, as it's new)
    if key not in context.bot_data["post_reactions"]:
        context.bot_data["post_reactions"][key] = {emoji: set() for emoji in REACTIONS}

    # For channels, we perform the edit now that data is initialized
    if is_channel:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=target_message_id,
                reply_markup=get_keyboard(context.bot_data["post_reactions"][key])
            )
        except Exception as e:
            logger.error(f"Failed to add buttons to channel post {target_message_id} in chat {chat_id}: {e}")


async def handle_reaction_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the button click, toggles the reaction for the user, and updates the buttons.
    """
    query = update.callback_query
    user = query.from_user
    
    data = query.data
    if not data.startswith("reaction|"):
        await query.answer()
        return

    selected_emoji = data.split("|")[1]
    
    message = query.message
    chat_id = message.chat_id
    message_id = message.message_id
    key = f"{chat_id}_{message_id}"
    
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
    new_markup = get_keyboard(reactions_map)
    
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to update reactions for message {message_id} in chat {chat_id}: {e}")


def main():
    """Start the bot."""
    # Get the token from environment variable or ask user to input it
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    
    persistence = PicklePersistence(filepath="bot_data.pickle")
    
    application = Application.builder().token(token).persistence(persistence).build()

    # 1. Handler for Channel Posts
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & (~filters.UpdateType.EDITED_MESSAGE) & (~filters.UpdateType.EDITED_CHANNEL_POST), add_reaction_buttons))
    
    # 2. Handler for Group Messages
    # We use reply_text approach now
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND) & (~filters.UpdateType.EDITED_MESSAGE), add_reaction_buttons))
    
    # 3. Callback Query Handler
    application.add_handler(CallbackQueryHandler(handle_reaction_click))

    print("Bot is starting... Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

