"""
One-Click Content Planner
Kunde verbindet Instagram/Facebook/TikTok per OAuth → lädt Videos hoch → plant 30 Tage
"""

import os, uuid, sqlite3, json, time, threading, requests, urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, redirect, jsonify, render_template, session, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import cloudinary, cloudinary.api, cloudinary.uploader
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "oneclick-secret-2026")
BERLIN = pytz.timezone("Europe/Berlin")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BASE_URL          = os.environ.get("BASE_URL", "https://oneclick.up.railway.app")
META_APP_ID       = os.environ.get("META_APP_ID", "2063582944215113")
META_APP_SECRET   = os.environ.get("META_APP_SECRET", "6de7e70cfe1585e840c71a5e956b3dd7")
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "awqonr7yi3ykhomu")
META_CONFIG_ID    = os.environ.get("META_CONFIG_ID", "818172838011332")
TIKTOK_SECRET     = os.environ.get("TIKTOK_CLIENT_SECRET", "UPrtHs9QQ6j5rQPlX91SmyQvjhYUe878")
CLOUDINARY_CLOUD  = os.environ.get("CLOUDINARY_CLOUD_NAME", "dlv8ebddq")
CLOUDINARY_KEY    = os.environ.get("CLOUDINARY_API_KEY", "837591974475139")
CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "1wHQz08D45SYbFg7vuecfVMaOac")
ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH           = os.environ.get("DB_PATH", "/app/data/oneclick.db")
# ──────────────────────────────────────────────────────────────────────────────

cloudinary.config(cloud_name=CLOUDINARY_CLOUD, api_key=CLOUDINARY_KEY, api_secret=CLOUDINARY_SECRET)

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ig_user_id TEXT, ig_token TEXT, ig_username TEXT,
            fb_page_id TEXT, fb_page_token TEXT, fb_page_name TEXT,
            tiktok_token TEXT, tiktok_username TEXT,
            post_time TEXT DEFAULT '19:00',
            caption_mode TEXT DEFAULT 'fixed',
            caption_text TEXT DEFAULT '',
            platforms TEXT DEFAULT 'ig,fb'
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            cloudinary_url TEXT,
            public_id TEXT,
            post_order INTEGER,
            posted INTEGER DEFAULT 0,
            posted_at TEXT
        )""")
        db.commit()

def get_user(user_id):
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

def update_user(user_id, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    with get_db() as db:
        db.execute(f"UPDATE users SET {fields} WHERE id=?", values)
        db.commit()

# ─── AI CAPTION ───────────────────────────────────────────────────────────────
def generate_caption(user_name: str) -> str:
    if not ANTHROPIC_KEY:
        return "Amazing content created with AI! Comment 'AI' to learn how. Link in bio."
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""Write a short Instagram caption for {user_name}.
        Must include: 1) Hook first line 2) Comment 'AI' CTA 3) 5 relevant hashtags.
        Max 150 words. Return only the caption text."""}]
    )
    return msg.content[0].text

# ─── POSTING ──────────────────────────────────────────────────────────────────
def post_instagram(ig_user_id, ig_token, video_url, caption):
    base = f"https://graph.instagram.com/v21.0/{ig_user_id}"
    r = requests.post(f"{base}/media", data={
        "video_url": video_url, "media_type": "REELS",
        "caption": caption, "share_to_feed": "true", "access_token": ig_token,
    })
    if r.status_code != 200:
        print(f"IG Container error: {r.text}")
        return False
    container_id = r.json().get("id")
    for _ in range(20):
        time.sleep(15)
        s = requests.get(f"https://graph.instagram.com/v21.0/{container_id}",
            params={"fields": "status_code", "access_token": ig_token}).json().get("status_code", "")
        if s == "FINISHED":
            break
        if s == "ERROR":
            return False
    pub = requests.post(f"{base}/media_publish",
        data={"creation_id": container_id, "access_token": ig_token})
    return pub.status_code == 200

def post_facebook(fb_page_id, fb_page_token, video_url, caption):
    r = requests.post(
        f"https://graph-video.facebook.com/v21.0/{fb_page_id}/videos",
        data={"file_url": video_url, "description": caption,
              "published": "true", "access_token": fb_page_token})
    return r.status_code == 200

def post_tiktok(tiktok_token, video_url, caption):
    r = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": f"Bearer {tiktok_token}", "Content-Type": "application/json; charset=UTF-8"},
        json={"post_info": {"title": caption[:150], "privacy_level": "PUBLIC_TO_EVERYONE",
                            "disable_duet": False, "disable_comment": False, "disable_stitch": False},
              "source_info": {"source": "PULL_FROM_URL", "video_url": video_url}})
    return r.status_code == 200

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def run_posts_for_user(user):
    user_id  = user["id"]
    caption  = generate_caption(user["name"]) if user["caption_mode"] == "ai" else user["caption_text"]
    platforms= (user["platforms"] or "").split(",")

    with get_db() as db:
        video = db.execute(
            "SELECT * FROM queue WHERE user_id=? AND posted=0 ORDER BY post_order LIMIT 1",
            (user_id,)).fetchone()
    if not video:
        print(f"[{user['name']}] Queue leer")
        return

    video_url = video["cloudinary_url"]
    ig_ok = fb_ok = tt_ok = False

    if "ig" in platforms and user["ig_token"]:
        ig_ok = post_instagram(user["ig_user_id"], user["ig_token"], video_url, caption)
    if "fb" in platforms and user["fb_page_token"]:
        fb_ok = post_facebook(user["fb_page_id"], user["fb_page_token"], video_url, caption)
        time.sleep(3)
    if "tt" in platforms and user["tiktok_token"]:
        tt_ok = post_tiktok(user["tiktok_token"], video_url, caption)

    print(f"[{user['name']}] IG={'✓' if ig_ok else '✗'} FB={'✓' if fb_ok else '✗'} TT={'✓' if tt_ok else '✗'}")

    if ig_ok or fb_ok or tt_ok:
        with get_db() as db:
            db.execute("UPDATE queue SET posted=1, posted_at=? WHERE id=?",
                      (datetime.now().isoformat(), video["id"]))
            db.commit()

def daily_scheduler():
    now_hour = datetime.now(BERLIN).strftime("%H:%M")
    with get_db() as db:
        users = db.execute("SELECT * FROM users WHERE post_time=?", (now_hour,)).fetchall()
    for user in users:
        threading.Thread(target=run_posts_for_user, args=(user,), daemon=True).start()

# ─── ROUTES ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/new")
def new_account():
    user_id = str(uuid.uuid4())[:8]
    with get_db() as db:
        db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (user_id, f"User {user_id}"))
        db.commit()
    return redirect(f"/dashboard/{user_id}")

@app.route("/dashboard/<user_id>")
def dashboard(user_id):
    user = get_user(user_id)
    if not user:
        return redirect("/")
    with get_db() as db:
        total   = db.execute("SELECT COUNT(*) FROM queue WHERE user_id=?", (user_id,)).fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM queue WHERE user_id=? AND posted=0", (user_id,)).fetchone()[0]
        posted  = total - pending
    return render_template("dashboard.html", user=dict(user),
                           total=total, pending=pending, posted=posted)

# ─── INSTAGRAM OAUTH ──────────────────────────────────────────────────────────
@app.route("/instagram/auth/<user_id>")
def instagram_auth(user_id):
    params = {
        "client_id": META_APP_ID,
        "redirect_uri": f"{BASE_URL}/instagram/callback",
        "config_id": META_CONFIG_ID,
        "response_type": "code",
        "state": user_id,
    }
    return redirect("https://www.facebook.com/v21.0/dialog/oauth?" + urllib.parse.urlencode(params))

@app.route("/instagram/callback")
def instagram_callback():
    code    = request.args.get("code")
    user_id = request.args.get("state")
    if not code or not user_id:
        return "Fehler: Kein Code erhalten", 400

    # Short-lived token
    r = requests.get("https://graph.facebook.com/v21.0/oauth/access_token", params={
        "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
        "code": code, "redirect_uri": f"{BASE_URL}/instagram/callback"
    })
    short_token = r.json().get("access_token")
    if not short_token:
        return f"Token-Fehler: {r.text}", 400

    # Long-lived token (60 Tage)
    r2 = requests.get("https://graph.facebook.com/v21.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET, "fb_exchange_token": short_token
    })
    long_token = r2.json().get("access_token", short_token)

    # Instagram Business Account holen
    pages = requests.get("https://graph.facebook.com/v21.0/me/accounts",
        params={"access_token": long_token}).json().get("data", [])

    ig_user_id = ig_username = None
    for page in pages:
        page_token = page.get("access_token")
        ig_data = requests.get(f"https://graph.facebook.com/v21.0/{page['id']}",
            params={"fields": "instagram_business_account", "access_token": page_token}).json()
        ig_id = ig_data.get("instagram_business_account", {}).get("id")
        if ig_id:
            me = requests.get(f"https://graph.instagram.com/v21.0/{ig_id}",
                params={"fields": "username", "access_token": long_token}).json()
            ig_user_id  = ig_id
            ig_username = me.get("username", ig_id)
            break

    if not ig_user_id:
        return "Kein Instagram Business Account gefunden. Stelle sicher dass dein Instagram mit einer Facebook-Seite verbunden ist.", 400

    update_user(user_id, ig_user_id=ig_user_id, ig_token=long_token, ig_username=ig_username)
    return redirect(f"/dashboard/{user_id}?connected=instagram")

# ─── FACEBOOK OAUTH ───────────────────────────────────────────────────────────
@app.route("/facebook/auth/<user_id>")
def facebook_auth(user_id):
    params = {
        "client_id": META_APP_ID,
        "redirect_uri": f"{BASE_URL}/facebook/callback",
        "config_id": META_CONFIG_ID,
        "response_type": "code",
        "state": user_id,
    }
    return redirect("https://www.facebook.com/v21.0/dialog/oauth?" + urllib.parse.urlencode(params))

@app.route("/facebook/callback")
def facebook_callback():
    code    = request.args.get("code")
    user_id = request.args.get("state")
    if not code or not user_id:
        return "Fehler", 400

    r = requests.get("https://graph.facebook.com/v21.0/oauth/access_token", params={
        "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
        "code": code, "redirect_uri": f"{BASE_URL}/facebook/callback"
    })
    short_token = r.json().get("access_token")

    # Long-lived
    r2 = requests.get("https://graph.facebook.com/v21.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET, "fb_exchange_token": short_token
    })
    long_token = r2.json().get("access_token", short_token)

    # Page token holen
    pages = requests.get("https://graph.facebook.com/v21.0/me/accounts",
        params={"access_token": long_token}).json().get("data", [])
    if not pages:
        return "Keine Facebook-Seite gefunden", 400

    page = pages[0]
    update_user(user_id, fb_page_id=page["id"], fb_page_token=page["access_token"],
                fb_page_name=page["name"])
    return redirect(f"/dashboard/{user_id}?connected=facebook")

# ─── TIKTOK OAUTH ─────────────────────────────────────────────────────────────
@app.route("/tiktok/auth/<user_id>")
def tiktok_auth(user_id):
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": "user.info.basic,video.publish,video.upload",
        "response_type": "code",
        "redirect_uri": f"{BASE_URL}/tiktok/callback",
        "state": user_id,
    }
    return redirect("https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params))

@app.route("/tiktok/callback")
def tiktok_callback():
    code    = request.args.get("code")
    user_id = request.args.get("state")
    r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key": TIKTOK_CLIENT_KEY, "client_secret": TIKTOK_SECRET,
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": f"{BASE_URL}/tiktok/callback"
    })
    token = r.json().get("access_token", "")
    if token:
        update_user(user_id, tiktok_token=token)
    return redirect(f"/dashboard/{user_id}?connected=tiktok")

# ─── VIDEO UPLOAD ─────────────────────────────────────────────────────────────
@app.route("/upload/<user_id>", methods=["POST"])
def upload_videos(user_id):
    user = get_user(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    files   = request.files.getlist("videos")
    results = []

    with get_db() as db:
        current_max = db.execute(
            "SELECT COALESCE(MAX(post_order), 0) FROM queue WHERE user_id=?",
            (user_id,)).fetchone()[0]

    for i, file in enumerate(files):
        order = current_max + i + 1
        try:
            result = cloudinary.uploader.upload(
                file, resource_type="video",
                folder=f"oneclick/{user_id}",
                public_id=f"video_{order:03d}_{file.filename.rsplit('.', 1)[0]}",
                context=f"ig_posted=false|post_order={order}",
                tags=[f"user_{user_id}"]
            )
            with get_db() as db:
                db.execute(
                    "INSERT INTO queue (user_id, cloudinary_url, public_id, post_order) VALUES (?,?,?,?)",
                    (user_id, result["secure_url"], result["public_id"], order))
                db.commit()
            results.append({"file": file.filename, "status": "ok", "order": order})
        except Exception as e:
            results.append({"file": file.filename, "status": "error", "error": str(e)})

    return jsonify({"uploaded": len([r for r in results if r["status"] == "ok"]),
                    "total": len(files), "results": results})

# ─── SETTINGS ─────────────────────────────────────────────────────────────────
@app.route("/settings/<user_id>", methods=["POST"])
def save_settings(user_id):
    data = request.json or request.form
    update_user(user_id,
        name          = data.get("name", "My Account"),
        post_time     = data.get("post_time", "19:00"),
        caption_mode  = data.get("caption_mode", "fixed"),
        caption_text  = data.get("caption_text", ""),
        platforms     = data.get("platforms", "ig,fb"),
    )
    return jsonify({"status": "saved"})

# ─── MANUAL TRIGGER ───────────────────────────────────────────────────────────
@app.route("/post_now/<user_id>")
def post_now(user_id):
    user = get_user(user_id)
    if not user:
        return jsonify({"error": "not found"}), 404
    threading.Thread(target=run_posts_for_user, args=(dict(user),), daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/queue/<user_id>")
def queue_status(user_id):
    with get_db() as db:
        total   = db.execute("SELECT COUNT(*) FROM queue WHERE user_id=?", (user_id,)).fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM queue WHERE user_id=? AND posted=0", (user_id,)).fetchone()[0]
    return jsonify({"total": total, "pending": pending, "posted": total - pending})

@app.route("/disconnect/<user_id>/<platform>")
def disconnect(user_id, platform):
    fields = {"instagram": {"ig_token": None, "ig_user_id": None, "ig_username": None},
              "facebook":  {"fb_page_token": None, "fb_page_id": None, "fb_page_name": None},
              "tiktok":    {"tiktok_token": None, "tiktok_username": None}}
    if platform in fields:
        update_user(user_id, **fields[platform])
    return redirect(f"/dashboard/{user_id}")

# ─── START ────────────────────────────────────────────────────────────────────
init_db()
scheduler = BackgroundScheduler(timezone=BERLIN)
scheduler.add_job(daily_scheduler, CronTrigger(minute=0, timezone=BERLIN))  # every hour, checks post_time
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
