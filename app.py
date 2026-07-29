from flask import Flask, request, jsonify, render_template
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import logging
import warnings
from urllib3.exceptions import InsecureRequestWarning
import os
import threading
import time
from datetime import datetime, timedelta
import pytz

warnings.simplefilter('ignore', InsecureRequestWarning)

app = Flask(__name__, template_folder="templates")
app.logger.setLevel(logging.INFO)

# ================= ক্রেডিট ইনক্রিপশন =================

B_SITE = b'\x46\x46\x20\x4c\x69\x6b\x65\x20\x50\x72\x6f'
B_CRED = b'\x63\x72\x65\x61\x74\x65\x64\x20\x62\x79\x20\x52\x6f\x6d\x61\x6e\x20\x74\x67\x20\x40\x4d\x44\x5f\x52\x6f\x6d\x61\x6e\x5f\x41\x6d\x61\x64'
B_LINK = b'\x68\x74\x74\x70\x73\x3a\x2f\x2f\x74\x2e\x6d\x65\x2f\x4d\x44\x5f\x52\x6f\x6d\x61\x6e\x5f\x41\x6d\x61\x64'

SITE_NAME = B_SITE.decode('utf-8')
CREDIT_TEXT = B_CRED.decode('utf-8')
TG_LINK = B_LINK.decode('utf-8')

# ================= ফায়ারবেস কনফিগারেশন =================
FIREBASE_API_KEY = "AIzaSyCvMIbiMnhJ2gu7qXysEzQi_goy5lDLMkk"
FIREBASE_DB_URL = "https://ff-auto-like-default-rtdb.asia-southeast1.firebasedatabase.app"
ADMIN_EMAIL = "admin@gmail.com"
ADMIN_PASS = "axromjan"

firebase_id_token = None
token_expires_at = None

def get_firebase_token():
    global firebase_id_token, token_expires_at
    if firebase_id_token and token_expires_at and datetime.now() < token_expires_at:
        return firebase_id_token
        
    auth_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    try:
        res = requests.post(auth_url, json={"email": ADMIN_EMAIL, "password": ADMIN_PASS, "returnSecureToken": True}, timeout=10).json()
        if 'idToken' in res:
            firebase_id_token = res['idToken']
            token_expires_at = datetime.now() + timedelta(seconds=int(res['expiresIn']) - 300)
            return firebase_id_token
    except Exception: pass
    return None

def update_firebase_stats(likes_added):
    token = get_firebase_token()
    if not token: return
    try:
        url = f"{FIREBASE_DB_URL}/stats.json?auth={token}"
        current = requests.get(url, timeout=10).json() or {}
        requests.patch(url, json={
            "total_likes": int(current.get('total_likes', 0)) + likes_added,
            "today_likes": int(current.get('today_likes', 0)) + likes_added
        }, timeout=10)
    except Exception: pass

# ================= সুপার ফাস্ট টোকেন জেনারেটর =================
ACCOUNTS_FILE = "accounts.txt"
TOKEN_FILE_BD = "token_bd.json"
TOKEN_API_URL = "https://guest-uid-pass-token.vercel.app/guest"

last_token_update_time = None
token_generation_lock = threading.Lock()

async def fetch_token_api(session, uid, pwd):
    """Async API Request to fetch tokens instantly"""
    try:
        async with session.get(TOKEN_API_URL, params={"uid": uid, "pw": pwd}, timeout=10) as res:
            data = await res.json()
            tkn = data.get("MajorLogin", {}).get("token")
            if tkn: return {"uid": str(uid), "token": tkn, "region": "BD"}
    except Exception: pass
    return None

async def generate_tokens_concurrently(accounts):
    """সবগুলো একাউন্টের জন্য একসাথে রিকোয়েস্ট ফায়ার করবে"""
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_token_api(session, acc['uid'], acc['password']) for acc in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]

def ensure_tokens():
    """নিখুঁত ৭ ঘণ্টার লজিক এবং ইমার্জেন্সি এম্পটি ফাইল চেকার"""
    global last_token_update_time
    now = datetime.now()
    
    with token_generation_lock:
        needs_update = False
        
        # শর্ত ১: প্রথম রান অথবা ৭ ঘণ্টা পার হয়ে গেলে
        if last_token_update_time is None or (now - last_token_update_time) > timedelta(hours=7):
            needs_update = True
        
        # শর্ত ২: ৭ ঘণ্টার ভেতরে হলেও যদি টোকেন ফাইল না থাকে বা ফাঁকা থাকে
        if not needs_update:
            if not os.path.exists(TOKEN_FILE_BD):
                needs_update = True
            else:
                try:
                    with open(TOKEN_FILE_BD, 'r') as f:
                        data = json.load(f)
                        if not data: needs_update = True
                except Exception: needs_update = True
                
        if needs_update:
            app.logger.info("Initializing Super-Fast Token Generation...")
            accounts = []
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, "r") as f:
                    for line in f:
                        if ":" in line and not line.startswith("#"):
                            u, p = line.strip().split(":", 1)
                            accounts.append({"uid": u, "password": p})
            
            if accounts:
                successful = asyncio.run(generate_tokens_concurrently(accounts))
                if successful:
                    with open(TOKEN_FILE_BD, "w") as f:
                        json.dump(successful, f, indent=2)
                    app.logger.info(f"Generated {len(successful)} tokens instantly!")
                    last_token_update_time = datetime.now()

# ================= স্মার্ট লাইক ডেলিভারি ইঞ্জিন =================
def encrypt_message(plaintext):
    try:
        cipher = AES.new(b'Yg&tc%DEuh6%Zc^8', AES.MODE_CBC, b'6oyZDr22E3ychjM%')
        padded_message = pad(plaintext, AES.block_size)
        return binascii.hexlify(cipher.encrypt(padded_message)).decode('utf-8')
    except Exception: return None

FF_HEADERS = {
    'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
    'Connection': "Keep-Alive",
    'Accept-Encoding': "gzip",
    'Content-Type': "application/x-www-form-urlencoded",
    'Expect': "100-continue",
    'X-Unity-Version': "2018.4.11f1",
    'X-GA': "v1 1",
    'ReleaseVersion': "OB54"
}

# গ্লোবাল ভেরিয়েবল যাতে প্রতিবার প্রথম থেকে টোকেন চেক করতে না হয়
last_working_idx = 0 

def get_profile_and_working_token(uid, tokens_list):
    """স্মার্ট প্রোফাইল চেকার (আগে কাজ করা টোকেন দিয়ে শুরু করবে)"""
    global last_working_idx
    try:
        msg = uid_generator_pb2.uid_generator()
        msg.saturn_ = int(uid)
        msg.garena = 1
        enc_uid = encrypt_message(msg.SerializeToString())
        if not enc_uid: return None, None

        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"
        
        # লিস্ট রিস্ট্রাকচার করা হলো যাতে আগে কাজ করা ইন্ডেক্স থেকে খোঁজা শুরু করে
        start_idx = last_working_idx % len(tokens_list)
        ordered_tokens = tokens_list[start_idx:] + tokens_list[:start_idx]
        
        for i, item in enumerate(ordered_tokens):
            token = item['token']
            headers = FF_HEADERS.copy()
            headers['Authorization'] = f"Bearer {token}"
            try:
                res = requests.post(url, data=bytes.fromhex(enc_uid), headers=headers, verify=False, timeout=10)
                if res.status_code == 200 and res.content:
                    items = like_count_pb2.Info()
                    items.ParseFromString(res.content)
                    data_json = json.loads(MessageToJson(items))
                    likes = int(data_json.get('AccountInfo', {}).get('Likes', 0))
                    name = str(data_json.get('AccountInfo', {}).get('PlayerNickname', 'Unknown'))
                    
                    last_working_idx = (start_idx + i) % len(tokens_list) # সেভ করা হলো
                    return {"name": name, "likes": likes}, token
            except Exception: continue
        return None, None
    except Exception: return None, None

async def send_single_like(encrypted_uid, token, url, session):
    headers = FF_HEADERS.copy()
    headers['Authorization'] = f"Bearer {token}"
    try:
        async with session.post(url, data=bytes.fromhex(encrypted_uid), headers=headers, timeout=15) as res:
            return await res.text()
    except Exception: return None

def process_like_system(uid, like_count):
    ensure_tokens()
    try:
        if not os.path.exists(TOKEN_FILE_BD): return {"status": "error", "message": "No token file"}
        with open(TOKEN_FILE_BD, "r") as f: tokens = json.load(f)
        if not tokens: return {"status": "error", "message": "Token file is empty"}

        profile_before, working_token = get_profile_and_working_token(uid, tokens)
        if not profile_before:
            return {"status": "error", "message": "গেম সার্ভার থেকে ডাটা পাওয়া যায়নি। UID সঠিক কিনা চেক করুন।"}
            
        player_name = profile_before["name"]
        before_likes = profile_before["likes"]
        
        msg = like_pb2.like()
        msg.uid = int(uid)
        msg.region = "BD"
        enc_uid_like = encrypt_message(msg.SerializeToString())
        url_like = "https://clientbp.ggpolarbear.com/LikeProfile"
        
        for attempt in range(2):
            tasks = []
            async def run_batch():
                async with aiohttp.ClientSession() as session:
                    for i in range(int(like_count)):
                        tkn = tokens[i % len(tokens)]["token"]
                        tasks.append(send_single_like(enc_uid_like, tkn, url_like, session))
                    await asyncio.gather(*tasks, return_exceptions=True)
            
            asyncio.run(run_batch())
            time.sleep(2)
            
            profile_after, _ = get_profile_and_working_token(uid, [{"token": working_token}])
            after_likes = profile_after["likes"] if profile_after else before_likes
            likes_given = after_likes - before_likes
            
            if likes_given > 0:
                update_firebase_stats(likes_given)
                return {
                    "status": "success", "player_name": player_name, "uid": uid,
                    "before_like": before_likes, "after_like": after_likes, "likes_given": likes_given,
                    "message": f"Success: {likes_given} Likes sent.",
                    "credit": CREDIT_TEXT
                }
            else:
                if attempt == 0: time.sleep(10)
                else:
                    return {
                        "status": "failed", "player_name": player_name, "uid": uid,
                        "before_like": before_likes, "after_like": after_likes, "likes_given": 0,
                        "message": "লাইক লিমিট শেষ বা রিকোয়েস্ট ফেইল।",
                        "credit": CREDIT_TEXT
                    }
    except Exception as e: return {"status": "error", "message": str(e)}

# ================= অটো লাইক শিডিউলার =================
def auto_like_scheduler():
    tz = pytz.timezone('Asia/Dhaka')
    while True:
        now = datetime.now(tz)
        current_time = now.strftime("%H:%M")
        
        if current_time == "04:30":
            ensure_tokens()
            time.sleep(60)
            
        elif current_time == "05:00":
            token = get_firebase_token()
            if token:
                try:
                    url = f"{FIREBASE_DB_URL}/auto_likes.json?auth={token}"
                    auto_likes = requests.get(url, timeout=15).json() or {}
                    
                    for key, data in auto_likes.items():
                        if int(data.get('days_left', 0)) > 0:
                            result = process_like_system(data['uid'], int(data['daily_likes']))
                            patch_url = f"{FIREBASE_DB_URL}/auto_likes/{key}.json?auth={token}"
                            
                            if result.get('status') == 'success' and result.get('likes_given', 0) > 0:
                                new_days = int(data['days_left']) - 1
                                new_tot = int(data.get('total_given', 0)) + result['likes_given']
                                status_msg = f"Success (+{result['likes_given']}) at {now.strftime('%I:%M %p')}"
                                if new_days <= 0: requests.delete(patch_url.replace('.json', ''))
                                else: requests.patch(patch_url, json={"days_left": new_days, "total_given": new_tot, "last_response": status_msg})
                            else:
                                requests.patch(patch_url, json={"last_response": "Failed (0 Likes)"})
                            time.sleep(40)
                except Exception: pass
            time.sleep(60)
        time.sleep(30)

# ================= ওয়েব ও API রাউটস =================
# রেন্ডারে HTML ফাইল পাঠানোর সময় ক্রেডিট ভেরিয়েবলগুলো পাস করা হচ্ছে
@app.route('/')
def home(): 
    return render_template('index.html', site_name=SITE_NAME, credit=CREDIT_TEXT, tg_link=TG_LINK)

@app.route('/admin')
def fake_admin(): 
    return render_template('fake_admin.html')

@app.route('/admin200')
def admin_login(): 
    return render_template('admin_login.html', site_name=SITE_NAME)

@app.route('/dashboard')
def dashboard(): 
    return render_template('dashboard.html', site_name=SITE_NAME, credit=CREDIT_TEXT, tg_link=TG_LINK)

@app.route('/api/admin/send_instant')
def api_admin_send_instant():
    uid = request.args.get('uid')
    count = request.args.get('count')
    if not uid or not count: return jsonify({"status": "error", "message": "Data Missing"}), 400
    return jsonify(process_like_system(uid, count))

# প্রফেশনাল শর্ট API (e.g. /like?key=RXABCX&uid=12345)
@app.route('/like')
def api_user_like():
    api_key = request.args.get('key')
    uid = request.args.get('uid')
    
    if not api_key or not uid:
        return jsonify({"status": "error", "message": "Missing key or uid parameter", "credit": CREDIT_TEXT}), 400
        
    token = get_firebase_token()
    url = f"{FIREBASE_DB_URL}/api_keys/{api_key}.json?auth={token}"
    api_data = requests.get(url, timeout=10).json()
    
    if not api_data: return jsonify({"status": "error", "message": "Invalid API Key", "credit": CREDIT_TEXT}), 401
        
    used = int(api_data.get('used_requests', 0))
    limit = int(api_data.get('limit', 0))
    
    if used >= limit: return jsonify({"status": "error", "message": "API Limit Reached", "credit": CREDIT_TEXT}), 403
        
    likes_to_send = int(api_data.get('likes_per_req', 10))
    result = process_like_system(uid, likes_to_send)
    
    if result.get('status') == 'success' and result.get('likes_given', 0) > 0:
        requests.patch(url, json={"used_requests": used + 1})
        
    return jsonify(result)

@app.route('/ping')
def keep_alive():
    return jsonify({"status": "alive"})

if __name__ == '__main__':
    threading.Thread(target=auto_like_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
