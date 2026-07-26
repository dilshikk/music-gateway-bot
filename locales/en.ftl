welcome = 👋 Hello, { $name } { $badge }!

 🎵 I'll help you find and download any music.

 Just type a song title or artist name.

help-text =
 How to use: 

 1. Type a track or artist name
 2. Pick the track from the list
 3. Get the audio file

 Commands: 
 /start — main menu
 /history — search history
 /favorites — favorite tracks
 /popular — popular queries
 /settings — settings
 /help — this help

search-placeholder = Type a track name...
search-too-short = ✏️ Please enter a track name (at least 2 characters).
search-processing = 🔍 Searching...
search-queue-position = ⏳ You are in queue: #{ $position } 
 Query: { $query } 
search-results-header = 🎵 Results for: { $query } 
 Found: { $total } tracks
search-not-found = 😔 Nothing found for { $query }.
search-error = ❌ Search error. Please try again later.
search-timeout = ⏰ Request timed out. Please try again.
search-queue-full = 🔴 Service overloaded. Try again in a minute.

download-processing = ⏳ Downloading...
download-caption = 🎵 { $artist } — { $title }
download-error = ❌ Could not get track. Try another one.
download-results-stale = ❌ Results expired. Please search again.

history-title = 📜 Search History: 
history-empty = 📭 Your search history is empty.
history-clear = 🗑 Clear history
history-cleared = 🗑 History cleared.

subscription-required =
 📢 To use the bot, please subscribe to:

 { $channels }
subscription-check = ✅ Check subscription
subscription-success =
 ✅ Great! You are subscribed to all channels.

 You can now use the bot.
subscription-fail = ❌ You are not yet subscribed to all channels:

 { $channels }

rate-limit-minute = ⏳ Too many requests.
 Please wait { $seconds } sec.
rate-limit-day = 📊 Daily limit reached.
 Resets in { $hours }h { $minutes }m.
banned = 🚫 You are banned.

settings-title = ⚙️ Settings 

 🌐 Language: { $language }
 🎵 Quality: { $quality }
 🔔 Notifications: { $notifications }
settings-language = 🌐 Language
settings-quality = 🎵 Audio quality
settings-notifications = 🔔 Notifications
settings-saved = ✅ Settings saved.

quality-any = Any
quality-128 = 128 kbps
quality-320 = 320 kbps
quality-lossless = Lossless

popular-title = 🔥 Popular Queries: 
popular-empty = 📭 No popular queries yet.

btn-close = ❌ Close
btn-back = 🔙 Back
btn-prev = ⬅️
btn-next = ➡️
btn-repeat = 🔁 Repeat
btn-favorite-add = ⭐ Add to favorites
btn-favorite-remove = 💔 Remove
btn-check-sub = ✅ Check subscription
btn-cancel = ❌ Cancel
btn-confirm = ✅ Confirm

favorites-title = ⭐ Favorite Tracks: 
favorites-empty = 📭 You have no favorite tracks.
favorites-added = ⭐ Added to favorites.
favorites-removed = 💔 Removed from favorites.
favorites-full = ⚠️ Favorites list is full (max 100 tracks).

# BUG FIX: missing keys used in inline.py
inline-hint-title = 🎵 Music Search
inline-hint-text = Type a track or artist name
